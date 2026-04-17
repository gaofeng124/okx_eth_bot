"""
会话交易追踪器（Session Tracker）
===================================

职责
----
1. 按通道追踪本次会话的胜率、平均 PnL、费用消耗
2. 计算实际 USDT 盈亏（扣除手续费后）
3. 提供"通道是否健康"判断，供策略动态降权
4. 跨会话持久化核心统计到文件（重启不丢学习结果）

设计原则
--------
- 只读取传入的数据，不持有仓位/订单状态
- 统计结果作为 runtime 字典的一部分传回给策略
- 文件 I/O 异常不能影响主交易循环

P&L 计算
---------
实际 USDT PnL = price_move_pct × notional_usdt × leverage - round_trip_fee_usdt
其中:
  notional_usdt = contracts × ct_val × mid_price
  round_trip_fee_usdt = notional_usdt × roundtrip_fee_bps / 10000

通道健康判断
-----------
连续亏损次数 ≥ 阈值 → 标记为不健康
最近 N 笔胜率 < 阈值 → 标记为不健康
不健康通道不会被策略入场（由 ScalpProStrategy 检查 is_channel_healthy()）
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from quant.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class ChannelStats:
    """单个通道的统计数据。"""
    channel: str
    trades: int = 0
    wins: int = 0
    total_pnl_pct: float = 0.0     # 累计价格移动 PnL（未含杠杆费用）
    total_pnl_usdt: float = 0.0    # 累计实际 USDT PnL（含杠杆扣费）
    total_fees_usdt: float = 0.0   # 累计手续费 USDT
    consec_loss: int = 0           # 当前连续亏损次数
    last_trade_ts: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0

    @property
    def avg_pnl_usdt(self) -> float:
        return self.total_pnl_usdt / self.trades if self.trades > 0 else 0.0

    @property
    def fee_efficiency(self) -> float:
        """总毛利 / 总手续费（>1 才盈利，< 0 = 亏损连手续费都补不上）"""
        gross = self.total_pnl_usdt + self.total_fees_usdt  # 还原手续费前
        if self.total_fees_usdt <= 0:
            return 0.0
        return gross / self.total_fees_usdt


class SessionTracker:
    """
    会话级交易追踪器。

    使用方法：
        tracker = SessionTracker(data_dir="./data", leverage=5.0, ct_val=0.01)
        tracker.load()   # 启动时加载上次会话数据

        # 每次出场时调用
        tracker.record(channel, pnl_pct, mid_price, contracts, exit_reason)

        # 查询通道健康状态
        if not tracker.is_channel_healthy("meanrev"):
            # 跳过 MR 入场

        tracker.save()   # 定期保存（或每笔后保存）
    """

    # 通道不健康的判断阈值
    _UNHEALTHY_CONSEC_LOSS = 4       # 连续亏 4 次 → 不健康
    _UNHEALTHY_WIN_RATE = 0.25       # 最近 10 笔胜率 < 25% → 不健康
    _UNHEALTHY_MIN_TRADES = 5        # 至少 5 笔才判断健康状态
    _UNHEALTHY_FEE_EFF = -0.5        # 费用效率 < -0.5 → 完全在亏手续费

    def __init__(
        self,
        data_dir: str = "./data",
        leverage: float = 5.0,
        ct_val: float = 0.01,
        roundtrip_fee_bps: float = 7.0,
        persist_file: str = "scalp_session.json",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._leverage = leverage
        self._ct_val = ct_val
        self._fee_bps = roundtrip_fee_bps
        self._persist_path = self._data_dir / persist_file

        # 通道统计
        self._channels: dict[str, ChannelStats] = {
            ch: ChannelStats(channel=ch)
            for ch in ("pullback", "momentum", "meanrev")
        }

        # 会话全局统计
        self._session_trades: int = 0
        self._session_wins: int = 0
        self._session_pnl_usdt: float = 0.0
        self._session_fees_usdt: float = 0.0
        self._session_start_ts: float = time.time()

        # 滚动最近 10 笔（用于实时胜率计算）
        self._rolling_pnl: deque[tuple[str, float]] = deque(maxlen=10)

        # 持久化：从上次会话继承的长期统计
        self._lifetime_trades: int = 0
        self._lifetime_wins: int = 0
        self._lifetime_pnl_usdt: float = 0.0

    # ── 数据持久化 ───────────────────────────────────────────

    def load(self) -> None:
        """启动时加载上次会话的持久化数据。"""
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, "r") as f:
                data = json.load(f)

            # 检查文件是否过期（超过 12 小时不加载通道状态，但保留长期统计）
            saved_ts = data.get("saved_ts", 0)
            age_hours = (time.time() - saved_ts) / 3600.0

            if age_hours < 12.0:
                # 恢复通道统计
                for ch, stats_dict in data.get("channels", {}).items():
                    if ch in self._channels:
                        s = self._channels[ch]
                        s.trades = stats_dict.get("trades", 0)
                        s.wins = stats_dict.get("wins", 0)
                        s.total_pnl_pct = stats_dict.get("total_pnl_pct", 0.0)
                        s.total_pnl_usdt = stats_dict.get("total_pnl_usdt", 0.0)
                        s.total_fees_usdt = stats_dict.get("total_fees_usdt", 0.0)
                        s.consec_loss = stats_dict.get("consec_loss", 0)
                log.info(
                    "[tracker] 已加载上次会话数据（%.1f小时前）：channels=%s",
                    age_hours,
                    {ch: f"n={s.trades} wr={s.win_rate:.0%}" for ch, s in self._channels.items() if s.trades > 0}
                )

            # 长期统计总是加载（不受12小时限制）
            self._lifetime_trades = data.get("lifetime_trades", 0)
            self._lifetime_wins = data.get("lifetime_wins", 0)
            self._lifetime_pnl_usdt = data.get("lifetime_pnl_usdt", 0.0)

        except Exception as e:
            log.warning("[tracker] 加载持久化文件失败，忽略: %s", e)

    def save(self) -> None:
        """将当前统计写入文件（每笔交易后调用）。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "saved_ts": time.time(),
                "channels": {
                    ch: {
                        "trades": s.trades,
                        "wins": s.wins,
                        "total_pnl_pct": s.total_pnl_pct,
                        "total_pnl_usdt": s.total_pnl_usdt,
                        "total_fees_usdt": s.total_fees_usdt,
                        "consec_loss": s.consec_loss,
                        "win_rate": round(s.win_rate, 4),
                        "avg_pnl_usdt": round(s.avg_pnl_usdt, 4),
                        "fee_efficiency": round(s.fee_efficiency, 4),
                    }
                    for ch, s in self._channels.items()
                },
                "session": {
                    "trades": self._session_trades,
                    "wins": self._session_wins,
                    "pnl_usdt": round(self._session_pnl_usdt, 4),
                    "fees_usdt": round(self._session_fees_usdt, 4),
                },
                "lifetime_trades": self._lifetime_trades + self._session_trades,
                "lifetime_wins": self._lifetime_wins + self._session_wins,
                "lifetime_pnl_usdt": round(
                    self._lifetime_pnl_usdt + self._session_pnl_usdt, 4
                ),
            }
            with open(self._persist_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning("[tracker] 保存持久化文件失败，忽略: %s", e)

    # ── 交易记录 ─────────────────────────────────────────────

    def record(
        self,
        channel: str,
        pnl_pct: float,
        mid_price: float,
        contracts: float,
        exit_reason: str = "",
    ) -> None:
        """
        记录一笔完成的交易。

        参数:
            channel:    通道名（"pullback" / "momentum" / "meanrev"）
            pnl_pct:    价格移动百分比（正=盈利，负=亏损），不含杠杆
            mid_price:  成交时中间价（用于计算名义价值）
            contracts:  合约数量
            exit_reason: 出场原因（用于诊断）
        """
        # 计算实际 USDT PnL
        notional = contracts * self._ct_val * mid_price
        gross_usdt = pnl_pct * notional * self._leverage
        fee_usdt = notional * self._fee_bps / 10000.0
        net_usdt = gross_usdt - fee_usdt

        # 更新通道统计
        s = self._channels.get(channel)
        if s is None:
            s = ChannelStats(channel=channel)
            self._channels[channel] = s

        s.trades += 1
        s.total_pnl_pct += pnl_pct
        s.total_pnl_usdt += net_usdt
        s.total_fees_usdt += fee_usdt
        s.last_trade_ts = time.time()

        if net_usdt > 0:
            s.wins += 1
            s.consec_loss = 0
        else:
            s.consec_loss += 1

        # 更新滚动记录
        self._rolling_pnl.append((channel, net_usdt))

        # 更新会话统计
        self._session_trades += 1
        self._session_pnl_usdt += net_usdt
        self._session_fees_usdt += fee_usdt
        if net_usdt > 0:
            self._session_wins += 1

        log.info(
            "[tracker] ch=%s exit=%s | gross=%.4f USDT fee=%.4f USDT net=%.4f USDT "
            "| ch_wr=%.0f%% ch_consec_loss=%d | session_net=%.4f USDT",
            channel, exit_reason,
            gross_usdt, fee_usdt, net_usdt,
            s.win_rate * 100, s.consec_loss,
            self._session_pnl_usdt,
        )

        self.save()

    # ── 健康状态查询 ──────────────────────────────────────────

    def is_channel_healthy(self, channel: str) -> bool:
        """
        判断通道是否处于健康状态。

        返回 False 的条件（任一）：
        - 连续亏损 ≥ 4 次
        - 最近 10 笔该通道胜率 < 25%（且至少有 5 笔）
        - 费用效率 < -0.5（完全被手续费侵蚀）
        """
        s = self._channels.get(channel)
        if s is None or s.trades < self._UNHEALTHY_MIN_TRADES:
            return True  # 数据不足，默认健康

        if s.consec_loss >= self._UNHEALTHY_CONSEC_LOSS:
            log.info(
                "[tracker] 通道 %s 不健康：连续亏损 %d 次",
                channel, s.consec_loss,
            )
            return False

        if s.win_rate < self._UNHEALTHY_WIN_RATE:
            log.info(
                "[tracker] 通道 %s 不健康：胜率 %.0f%% < 25%%（%d笔）",
                channel, s.win_rate * 100, s.trades,
            )
            return False

        if s.fee_efficiency < self._UNHEALTHY_FEE_EFF:
            log.info(
                "[tracker] 通道 %s 不健康：费用效率 %.2f < -0.5",
                channel, s.fee_efficiency,
            )
            return False

        return True

    def channel_health_summary(self) -> dict[str, Any]:
        """返回所有通道的健康摘要，供日志/诊断使用。"""
        return {
            ch: {
                "healthy": self.is_channel_healthy(ch),
                "trades": s.trades,
                "win_rate": round(s.win_rate, 3),
                "avg_pnl_usdt": round(s.avg_pnl_usdt, 4),
                "consec_loss": s.consec_loss,
                "fee_efficiency": round(s.fee_efficiency, 3),
            }
            for ch, s in self._channels.items()
        }

    def session_summary(self) -> dict[str, Any]:
        """当前会话统计摘要。"""
        elapsed_h = (time.time() - self._session_start_ts) / 3600.0
        return {
            "elapsed_hours": round(elapsed_h, 2),
            "trades": self._session_trades,
            "win_rate": round(self._session_wins / max(1, self._session_trades), 3),
            "net_pnl_usdt": round(self._session_pnl_usdt, 4),
            "total_fees_usdt": round(self._session_fees_usdt, 4),
            "pnl_per_hour": round(
                self._session_pnl_usdt / max(0.1, elapsed_h), 4
            ),
        }
