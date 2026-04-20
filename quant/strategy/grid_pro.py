"""
GridPro v2 — 专业智能网格策略
==============================
10x 杠杆 + 波动率自适应网格 + 多层趋势过滤 + 全覆盖风控

架构分层
--------
1. 市场感知层  → 点差 / 价格速度 / 资金费率 / 报价时效
2. 波动率引擎  → 三窗口 ATR + 波动率状态分级 + 动态格数
3. 趋势过滤层  → RegimeDetector + 速度门控 + 上行偏置
4. 订单管理层  → 精度对齐 / 失败分类 / 指数退避 / 部分成交
5. 风控层      → 整体止损 / 日亏损 / 日收益目标 / 峰值回撤 /
                 杠杆安全 / 连续止损延长冷静 / 爆仓监控
6. 启动对账    → 撤残留挂单 / 同步已有持仓 / 仪器规格获取
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from quant.analysis import StreamingEMA
from quant.exchange import OKXRestClient
from quant.logging_config import get_logger
from quant.models import OrderIntent
from quant.strategy.base import TickStrategy
from quant.strategy.regime import RegimeDetector, Regime
from quant.strategy.session_tracker import SessionTracker

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# 常量 & 数据结构
# ══════════════════════════════════════════════════════════════════════════════

# OKX 错误码（下单失败时区分原因）
_ERR_POST_ONLY   = "51000"   # post_only 会穿越盘口，拒绝 → 调整价格重试
_ERR_LOT_SIZE    = "51008"   # 张数精度错误 → 修正 sz 重试
_ERR_PRICE_BAND  = "51020"   # 价格超出涨跌停 → 跳过
_ERR_NO_MARGIN   = "51011"   # 保证金不足 → 不重试，报警
_ERR_REDUCE_ONLY = "51023"   # 无持仓可减 → 正常（可能已平）
_RETRYABLE_SCODES = {_ERR_POST_ONLY, _ERR_LOT_SIZE}

# 波动率状态
class VolRegime:
    DEAD     = "dead"      # ATR<8bps：市场冻结，不开格
    CALM     = "calm"      # 8~15bps：安静，开2档
    NORMAL   = "normal"    # 15~30bps：正常，开4档
    ELEVATED = "elevated"  # 30~50bps：偏高，开2档（宽格）
    EXTREME  = "extreme"   # >50bps：极端，暂停

# 槽位状态
class _S:
    EMPTY      = "empty"
    ENTRY_LIVE = "entry_live"
    HOLDING    = "holding"


@dataclass
class GridSlot:
    level: int
    target_price: float = 0.0
    entry_order_id: str = ""
    state: str = _S.EMPTY
    fill_price: float = 0.0
    fill_sz: float = 0.0            # 实际成交张数（支持部分成交）
    fill_ts: float = 0.0
    contracts: float = 1.0
    entry_ts: float = 0.0
    last_attempt_ts: float = 0.0
    fail_count: int = 0             # 连续下单失败次数（用于退避）
    retry_after_ts: float = 0.0     # 指数退避到期时间


# ══════════════════════════════════════════════════════════════════════════════
# 波动率引擎（三窗口 + 状态分级）
# ══════════════════════════════════════════════════════════════════════════════

class _VolEngine:
    """
    双轨波动率引擎：
      track_short  (α=0.15, ≈6 ticks)  → 格宽计算（即时反应）
      track_medium (α=0.03, ≈33 ticks) → vol regime 判断（更稳定）

    使用 EMA(|return|) 而非 range-over-window：
      - range ATR 在 WS 高频 tick 下数值极低（bid/ask 抖动仅 0.001-0.002bps/tick）
      - EMA return 随时间积累，几十个 tick 后即可准确反映 1-5 分钟级别的波动率
      - 等效：atr_medium ≈ ETH 过去 ~30s 的平均绝对收益率（已年化前）

    阈值对应（ETH ~2375）：
      DEAD   < 1bps  → 价格几乎不动（market maker 价差内）
      CALM   1-8bps  → 正常震荡，适合网格
      NORMAL 8-25bps → 活跃，正常
      ELEVATED 25-40bps → 高波动，减半格数
      EXTREME > 40bps  → 极端行情，停止
    """
    # EMA 平滑系数：α = 2/(N+1)，N 为等效窗口 tick 数
    _ALPHA_SHORT  = 0.15   # N≈12 ticks
    _ALPHA_MEDIUM = 0.03   # N≈65 ticks

    def __init__(self) -> None:
        self._ema_short:  float = 0.0
        self._ema_medium: float = 0.0
        self._last_price: float = 0.0
        self._tick_count: int   = 0

    def update(self, price: float) -> None:
        if price <= 0:
            return
        if self._last_price > 0 and price != self._last_price:
            # 仅在价格真实变化时更新 EMA；REST 轮询常返回相同价格，
            # 若纳入零返回会将 EMA 向 0 拉偏，导致 vol_regime 误判为 DEAD。
            ret = abs(price - self._last_price) / self._last_price
            if self._tick_count == 0:
                # 首次有效 return：直接初始化 EMA
                self._ema_short  = ret
                self._ema_medium = ret
            else:
                self._ema_short  = self._ALPHA_SHORT  * ret + (1 - self._ALPHA_SHORT)  * self._ema_short
                self._ema_medium = self._ALPHA_MEDIUM * ret + (1 - self._ALPHA_MEDIUM) * self._ema_medium
            self._tick_count += 1
        self._last_price = price

    @property
    def atr_short(self) -> float:
        """即时波动率（用于格宽计算）。tick 数不足 5 时返回 0 避免噪声。"""
        return self._ema_short if self._tick_count >= 5 else 0.0

    @property
    def atr_medium(self) -> float:
        """中期波动率（用于 vol regime 判断）。tick 数不足 20 时返回 0。"""
        return self._ema_medium if self._tick_count >= 20 else 0.0

    @property
    def atr_long(self) -> float:
        """兼容接口，返回 medium（EMA 本身已含长期记忆）。"""
        return self.atr_medium

    @property
    def vol_regime(self) -> str:
        # 用 atr_medium 判断，tick 不足时默认 CALM（允许正常开格，避免过度保守）
        atr = self.atr_medium
        if self._tick_count < 20:   return VolRegime.CALM    # 数据不足→默认 CALM
        if atr < 0.000005: return VolRegime.DEAD             # <0.05bps：市场真正冻结（REST过滤后仍为0）
        if atr < 0.0008:  return VolRegime.CALM              # 0.05-8bps：正常（含 REST 模式低波动）
        if atr < 0.0025:  return VolRegime.NORMAL            # 8-25bps：活跃
        if atr < 0.0040:  return VolRegime.ELEVATED          # 25-40bps：高波动
        return VolRegime.EXTREME                              # >40bps：极端

    def active_levels(self, max_levels: int = 4) -> int:
        """根据波动率状态决定激活几档网格。"""
        vr = self.vol_regime
        if vr == VolRegime.DEAD:     return 1                  # 极低波动：挂 1 档观察
        if vr == VolRegime.CALM:     return min(2, max_levels)
        if vr == VolRegime.NORMAL:   return max_levels
        if vr == VolRegime.ELEVATED: return min(2, max_levels)
        return 0  # EXTREME：停止

    def spacing_pct(self, atr_mult: float, min_sp: float, max_sp: float) -> float:
        """格宽 = clamp(short_ATR × mult, min, max)。"""
        raw = self.atr_short * atr_mult
        return max(min_sp, min(max_sp, raw))


# ══════════════════════════════════════════════════════════════════════════════
# 市场感知器
# ══════════════════════════════════════════════════════════════════════════════

class _MarketSensor:
    """实时市场状态感知：点差 / 速度 / 流动性 / 报价时效。"""

    def __init__(self, velocity_window: int = 20) -> None:
        self._prices: deque[float] = deque(maxlen=velocity_window)
        self._ts: deque[float] = deque(maxlen=velocity_window)

    def update(self, mid: float, now: float) -> None:
        self._prices.append(mid)
        self._ts.append(now)

    @property
    def velocity_pct(self) -> float:
        """最近 N tick 的价格变化率（负值=下跌）。"""
        if len(self._prices) < 5:
            return 0.0
        p = list(self._prices)
        return (p[-1] - p[0]) / p[0] if p[0] > 0 else 0.0

    @property
    def short_velocity_pct(self) -> float:
        """最近 4 tick 的价格变化率（短窗口急跌检测）。"""
        if len(self._prices) < 4:
            return 0.0
        p = list(self._prices)
        p4 = p[-4:]
        return (p4[-1] - p4[0]) / p4[0] if p4[0] > 0 else 0.0

    def spread_ok(self, ask: float, bid: float, max_bps: float) -> bool:
        if ask <= 0 or bid <= 0:
            return True   # 数据缺失时放行（保守）
        spread_bps = (ask - bid) / bid * 10000
        return spread_bps <= max_bps

    def quote_fresh(self, quote_ts: float, now: float, max_age: float = 5.0) -> bool:
        if quote_ts <= 0:
            return True   # 无时间戳时放行
        return (now - quote_ts) <= max_age


# ══════════════════════════════════════════════════════════════════════════════
# 每日 P&L 追踪器（含目标 + 峰值回撤）
# ══════════════════════════════════════════════════════════════════════════════

class _DailyPnL:
    """
    追踪当日已实现 P&L 和峰值回撤。
    - 到达日目标后切换「利润保护模式」（不开新格，只管存量）
    - 从峰值回撤过大时触发暂停
    """

    def __init__(
        self,
        daily_stop_usdt: float,
        daily_target_usdt: float,
        drawdown_from_peak_usdt: float,
    ) -> None:
        self._stop = daily_stop_usdt
        self._target = daily_target_usdt
        self._drawdown_limit = drawdown_from_peak_usdt

        self._realized: float = 0.0
        self._peak: float = 0.0
        self._stopped: bool = False
        self._target_reached: bool = False

    def add(self, usdt: float) -> None:
        self._realized += usdt
        if self._realized > self._peak:
            self._peak = self._realized

    @property
    def realized(self) -> float:
        return self._realized

    @property
    def target_reached(self) -> bool:
        return self._realized >= self._target

    def check_stop(self, unrealized: float = 0.0) -> tuple[bool, str]:
        """
        返回 (should_stop, reason)。
        should_stop=True 时应触发紧急平仓。
        """
        total = self._realized + unrealized

        if total <= -self._stop:
            return True, f"daily_loss_limit({total:.3f}<=-{self._stop})"

        drawdown = self._peak - self._realized
        if drawdown >= self._drawdown_limit and self._peak > 0.1:
            return True, f"drawdown_from_peak({drawdown:.3f}>={self._drawdown_limit})"

        return False, ""

    def profit_protect_mode(self) -> bool:
        """达到日目标后进入保护模式：不开新格，只管存量持仓到 TP。"""
        return self._realized >= self._target


# ══════════════════════════════════════════════════════════════════════════════
# 主策略
# ══════════════════════════════════════════════════════════════════════════════

class GridProStrategy(TickStrategy):
    """
    专业智能网格策略 v2。
    on_tick 始终返回 None，所有订单由内部 OKXRestClient 直接管理。
    """

    # ── 默认参数 ─────────────────────────────────────────────────────────────
    _STATUS_LOG_INTERVAL   = 30.0    # 每 30s 打印一次状态摘要
    _REGIME_STATS_INTERVAL = 3600.0  # 每小时打印 Regime 分类统计（验证分类有效性）
    _POSITION_SYNC_INTERVAL = 10.0   # 每 10s 从 API 同步持仓（校验内部状态）
    _FUNDING_CHECK_INTERVAL = 30.0   # 每 30s 刷新资金费率
    _QUOTE_MAX_AGE          = 5.0    # 报价超过 5s 视为过期
    _SPREAD_MAX_BPS         = 12.0   # 点差超过 12bps 不下单
    _VELOCITY_ALARM_PCT       = -0.0020 # -0.2% / 20tick 长窗口接飞刀警报
    _SHORT_VELOCITY_ALARM_PCT = -0.0030 # -0.30% / 4tick 短窗口急跌过滤（20-tick主窗口已提供飞刀保护，短窗口放宽减少假触发）
    _FUNDING_PAUSE_WINDOW   = 600.0  # 距资金费结算 10min 内暂停开新格
    _FUNDING_RATE_MAX       = 0.0005 # 资金费率 > 0.05% 时抑制做多
    _STOP_COUNT_1H_LIMIT    = 3      # 1小时内触发3次止损 → 延长冷静期
    _EXTENDED_COOLDOWN      = 3600.0 # 延长冷静期 1h
    _MIN_EQUITY_USDT        = 15.0   # 账户权益低于此值停止一切操作
    _MAX_MARGIN_USE_PCT     = 0.70   # 最多使用 70% 账户权益做保证金
    _LIQ_WARN_DISTANCE      = 0.05   # 距爆仓价 < 5% 时告警并紧急平仓
    _MAINT_MARGIN_RATE      = 0.0065 # OKX ETH-USDT-SWAP 10x 维持保证金率
    _ENTRY_RETRY_BACKOFF    = [5.0, 15.0, 60.0, 300.0]  # 失败后等待秒数
    _TP_TRAIL_MIN_INTERVAL  = 30.0   # TP 追踪最小间隔（秒），避免频繁 cancel/replace
    _EMERGENCY_CLOSE_FEE_BPS = 7.0  # 紧急平仓费率：入场 maker(2bps) + 市价 taker(5bps)

    def __init__(
        self,
        inst_id: str = "ETH-USDT-SWAP",
        leverage: float = 10.0,
        td_mode: str = "isolated",
        price_decimals: int = 2,
        ct_val: float = 0.01,
        grid_levels: int = 4,
        atr_window: int = 60,
        atr_mult: float = 1.2,
        min_spacing_pct: float = 0.0010,
        max_spacing_pct: float = 0.0050,
        whole_stop_usdt: float = 5.0,
        daily_stop_usdt: float = 6.0,
        daily_target_usdt: float = 999.0,
        drawdown_from_peak_usdt: float = 3.0,
        recenter_mult: float = 1.5,
        entry_timeout_sec: float = 120.0,
        cooldown_sec: float = 300.0,
        sync_interval_sec: float = 2.0,
        roundtrip_fee_bps: float = 4.0,
        data_dir: str = "./data",
        warmup_ticks: int = 200,
        contracts_per_slot: float = 1.0,
        per_slot_stop_usdt: float = 0.0,
    ) -> None:
        self._inst_id     = inst_id
        self._leverage    = leverage
        self._td_mode     = td_mode
        self._price_dec   = price_decimals
        self._ct_val      = ct_val
        self._max_levels  = grid_levels
        self._atr_mult    = atr_mult
        self._min_sp      = min_spacing_pct
        self._max_sp      = max_spacing_pct
        self._whole_stop  = whole_stop_usdt
        self._recenter    = recenter_mult
        self._entry_to    = entry_timeout_sec
        self._cooldown    = cooldown_sec
        self._sync_iv     = sync_interval_sec
        self._fee_bps     = roundtrip_fee_bps
        self._warmup_need = warmup_ticks
        # 单格张数（与账户规模匹配：小账户用分数张；lotSz 通常 0.01 支持）
        self._contracts_per_slot = max(0.01, float(contracts_per_slot))
        # 单仓硬止损（USDT）：任一 HOLDING 槽位浮亏超此值立即市价平该仓；0=关闭
        self._per_slot_stop = max(0.0, float(per_slot_stop_usdt))

        # 仪器规格（启动时从 API 获取）
        self._tick_sz: float = 10 ** (-price_decimals)
        self._lot_sz:  float = 1.0
        self._min_sz:  float = 1.0

        # 网格槽位（最多 grid_levels 个，实际激活由 vol_regime 决定）
        # 每个槽位初始合约张数由 contracts_per_slot 指定（默认 1.0 以向后兼容）
        self._slots: list[GridSlot] = [
            GridSlot(level=i, contracts=self._contracts_per_slot)
            for i in range(grid_levels)
        ]

        # 网格状态
        self._grid_center:  float = 0.0
        self._grid_spacing: float = 0.0
        self._grid_active:  bool  = False
        self._active_levels: int  = 0

        # VWAP 追踪
        self._total_held: float = 0.0
        self._vwap_value: float = 0.0
        self._vwap:       float = 0.0

        # TP 追踪
        self._tp_order_id: str   = ""
        self._tp_price:    float = 0.0
        self._tp_placed_ts: float = 0.0  # TP 下单时间戳（用于老化降价）

        # 子模块
        self._vol  = _VolEngine()
        self._sens = _MarketSensor(velocity_window=20)
        self._pnl  = _DailyPnL(
            daily_stop_usdt=daily_stop_usdt,
            daily_target_usdt=daily_target_usdt,
            drawdown_from_peak_usdt=drawdown_from_peak_usdt,
        )
        self._regime  = RegimeDetector(warmup_ticks=warmup_ticks)
        self._tracker = SessionTracker(
            data_dir=data_dir,
            leverage=leverage,
            ct_val=ct_val,
            roundtrip_fee_bps=roundtrip_fee_bps,
            persist_file="grid_session.json",
        )
        self._tracker.load()

        # EMA（Regime 特征）
        self._ema_fast  = StreamingEMA(alpha=0.12)
        self._ema_slow  = StreamingEMA(alpha=0.04)
        self._ema_macro = StreamingEMA(alpha=0.003)

        # 运行时状态
        self._warmup_ticks:  int   = 0
        self._last_bid:      float = 0.0   # 最新 bid（用于下单前校验价格不越叉）
        self._last_sync_ts:  float = 0.0
        self._last_pos_sync: float = 0.0
        self._last_fund_ts:  float = 0.0
        self._last_status_ts: float = 0.0
        self._last_regime_stats_ts: float = 0.0
        self._last_stop_ts:  float = 0.0
        self._last_cooldown_log_ts: float = 0.0   # 冷静期日志节流
        self._last_tp_trail_ts: float = 0.0       # 上次 TP 追踪时间（节流用）
        self._emergency_closing: bool = False

        # 止损计数（1h 窗口）
        self._stop_times: deque[float] = deque()  # 触发止损的时间戳列表

        # 资金费率缓存
        self._funding_rate:     float = 0.0
        self._next_funding_ms:  float = 0.0

        # 危险 Regime 持仓宽限期（TRENDING_DOWN/VOLATILE 进入时不立即割肉，
        # 给 45s 让 TP 自然成交或价格恢复；浮亏 > 1U 则立即止损）
        self._bearish_regime_since: float = 0.0

        # 恐贪指数缓存（每小时更新；FGI < 25 极度恐慌时在 _place_grid 减1档）
        self._fear_greed_index: int   = 50
        self._last_fgi_ts:      float = 0.0

        # REST 客户端
        self._rest = OKXRestClient()

        # 启动对账
        self._boot_reconcile()

        # 关键风控配置一次性打印（便于日志核对）
        log.info(
            "[grid] 风控配置 lev=%.1fx grid_levels=%d contracts_per_slot=%.3f "
            "whole_stop=%.2fU daily_stop=%.2fU per_slot_stop=%.2fU peak_dd=%.2fU "
            "ct_val_init=%.3f",
            self._leverage, self._max_levels, self._contracts_per_slot,
            self._whole_stop, self._pnl._stop, self._per_slot_stop,
            self._pnl._drawdown_limit, self._ct_val,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 启动对账
    # ══════════════════════════════════════════════════════════════════════════

    def _boot_reconcile(self) -> None:
        """启动时：获取仪器规格 → 设置杠杆 → 撤销残留挂单 → 同步持仓。"""
        self._fetch_instrument_spec()
        self._set_leverage()
        self._cancel_stale_orders()
        self._sync_existing_position()

    def _fetch_instrument_spec(self) -> None:
        """从 OKX API 获取真实的 tickSz / lotSz / minSz / ctVal。"""
        try:
            resp = self._rest.request(
                "GET",
                f"/api/v5/public/instruments?instType=SWAP&instId={self._inst_id}",
            )
            spec = (resp.get("data") or [{}])[0]
            self._tick_sz = float(spec.get("tickSz") or self._tick_sz)
            self._lot_sz  = float(spec.get("lotSz")  or self._lot_sz)
            self._min_sz  = float(spec.get("minSz")  or self._min_sz)
            ct_val_api    = float(spec.get("ctVal")   or self._ct_val)
            if ct_val_api > 0:
                self._ct_val = ct_val_api
            log.info(
                "[grid] 仪器规格 tickSz=%s lotSz=%s minSz=%s ctVal=%s",
                self._tick_sz, self._lot_sz, self._min_sz, self._ct_val,
            )
            eff_sz = self._round_sz(self._contracts_per_slot)
            if abs(eff_sz - self._contracts_per_slot) > 1e-9:
                log.warning(
                    "[grid] contracts_per_slot=%.3f → 实际下单张数=%.6g"
                    "（受lotSz=%s minSz=%s约束，每张%.4f ETH）",
                    self._contracts_per_slot, eff_sz,
                    self._lot_sz, self._min_sz, self._ct_val,
                )
        except Exception as e:
            log.warning("[grid] 获取仪器规格失败，使用默认值: %s", e)

    def _set_leverage(self) -> None:
        try:
            self._rest.request("POST", "/api/v5/account/set-leverage", {
                "instId": self._inst_id,
                "lever": str(int(self._leverage)),
                "mgnMode": self._td_mode,
            })
            log.info("[grid] 杠杆设置: %dx %s", int(self._leverage), self._td_mode)
        except Exception as e:
            log.warning("[grid] 杠杆设置失败: %s", e)

    def _cancel_stale_orders(self) -> None:
        """撤销 OKX 上所有该合约的残留挂单（避免与内部状态冲突）。"""
        try:
            resp = self._rest.request(
                "GET",
                f"/api/v5/trade/orders-pending?instType=SWAP&instId={self._inst_id}",
            )
            orders = resp.get("data") or []
            if not orders:
                log.info("[grid] 启动对账：无残留挂单")
                return
            for o in orders:
                oid = o.get("ordId", "")
                if oid:
                    try:
                        self._rest.request("POST", "/api/v5/trade/cancel-order", {
                            "instId": self._inst_id,
                            "ordId": oid,
                        })
                        log.info("[grid] 撤销残留挂单 ordId=%s", oid)
                    except Exception as e:
                        log.warning("[grid] 撤销残留单失败 %s: %s", oid, e)
        except Exception as e:
            log.warning("[grid] 查询残留挂单失败: %s", e)

    def _sync_existing_position(self) -> None:
        """同步已有持仓到内部状态，避免重启后不知道自己有仓。"""
        try:
            resp = self._rest.request(
                "GET",
                f"/api/v5/account/positions?instType=SWAP&instId={self._inst_id}",
            )
            for pos in (resp.get("data") or []):
                sz = float(pos.get("pos") or 0)
                avg_px = float(pos.get("avgPx") or 0)
                if sz > 0 and avg_px > 0:
                    log.warning(
                        "[grid] 检测到已有多仓 %.1f张 avgPx=%.2f，加入追踪",
                        sz, avg_px,
                    )
                    # 分配到槽位（按持仓量分配到前 N 个槽）
                    self._total_held = sz
                    self._vwap = avg_px
                    self._vwap_value = sz * avg_px
                    slots_to_fill = min(int(sz), len(self._slots))
                    for i in range(slots_to_fill):
                        self._slots[i].state = _S.HOLDING
                        self._slots[i].fill_price = avg_px
                        self._slots[i].fill_sz = 1.0
                    self._grid_active = True
                    self._grid_center = avg_px
                    # 将在第一次 on_tick 中计算 spacing 并挂 TP
        except Exception as e:
            log.warning("[grid] 启动同步持仓失败: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # 精度对齐
    # ══════════════════════════════════════════════════════════════════════════

    def _round_price(self, price: float) -> float:
        """将价格对齐到 tickSz。"""
        if self._tick_sz <= 0:
            return round(price, self._price_dec)
        n_decimals = max(0, -int(math.floor(math.log10(self._tick_sz))))
        return round(round(price / self._tick_sz) * self._tick_sz, n_decimals)

    def _round_sz(self, contracts: float) -> float:
        """将张数对齐到 lotSz 并确保 >= minSz。"""
        if self._lot_sz <= 0:
            return max(self._min_sz, contracts)
        aligned = round(contracts / self._lot_sz) * self._lot_sz
        return max(self._min_sz, aligned)

    def _px(self, price: float) -> str:
        return str(self._round_price(price))

    def _sz(self, contracts: float) -> str:
        v = self._round_sz(contracts)
        if self._lot_sz >= 1.0:
            return str(int(v))
        # fractional lot_sz（如0.01）：避免 int() 截断导致返回 "0"
        n_dec = max(0, -int(math.floor(math.log10(self._lot_sz))))
        return f"{v:.{n_dec}f}"

    # ══════════════════════════════════════════════════════════════════════════
    # 辅助计算
    # ══════════════════════════════════════════════════════════════════════════

    def _notional(self, contracts: float, price: float) -> float:
        return contracts * self._ct_val * price

    def _roundtrip_fee(self, contracts: float, price: float) -> float:
        return self._notional(contracts, price) * self._fee_bps / 10000.0

    def _calc_unrealized(self, mid: float) -> float:
        total = 0.0
        for s in self._slots:
            if s.state == _S.HOLDING and s.fill_price > 0:
                pnl_pct = (mid - s.fill_price) / s.fill_price
                total += pnl_pct * self._notional(s.fill_sz, mid) * self._leverage
        return total

    def _liq_price(self) -> float:
        """估算当前净多仓的理论爆仓价。"""
        if self._vwap <= 0 or self._total_held <= 0:
            return 0.0
        # isolated 逐仓：liq ≈ avgPx × (1 - 1/lever + maint_margin)
        return self._vwap * (1.0 - 1.0 / self._leverage + self._MAINT_MARGIN_RATE)

    def _total_margin_used(self, price: float) -> float:
        """当前持仓占用的保证金（USDT）。"""
        return self._notional(self._total_held, price) / self._leverage

    # ══════════════════════════════════════════════════════════════════════════
    # 市场状态检查
    # ══════════════════════════════════════════════════════════════════════════

    def _market_ok_to_enter(
        self, runtime: dict[str, Any], mid: float, now: float,
        bid: float = 0.0, ask: float = 0.0
    ) -> tuple[bool, str]:
        """
        综合检查市场条件是否允许下新网格单。
        返回 (ok, reason_if_not_ok)。
        """
        # 1. 报价时效：micro_ts 来自 runner WS 推送时间戳
        quote_ts = float(runtime.get("micro_ts") or 0.0)
        if quote_ts > 0 and not self._sens.quote_fresh(quote_ts, now, self._QUOTE_MAX_AGE):
            return False, f"stale_quote({now-quote_ts:.1f}s)"

        # 2. 点差：优先直接参数，fallback order_book
        _ask, _bid = ask, bid
        if not (_ask > 0 and _bid > 0):
            book = runtime.get("order_book") or {}
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            if asks and bids:
                _ask = float(asks[0][0]) if asks else 0.0
                _bid = float(bids[0][0]) if bids else 0.0
        if _ask > 0 and _bid > 0:
            if not self._sens.spread_ok(_ask, _bid, self._SPREAD_MAX_BPS):
                spread_bps = (_ask - _bid) / _bid * 10000 if _bid > 0 else 0
                return False, f"spread_wide({spread_bps:.1f}bps)"

        # 3. 价格速度（接飞刀检测）
        if self._sens.velocity_pct < self._VELOCITY_ALARM_PCT:
            return False, f"falling_knife({self._sens.velocity_pct*100:.3f}%/4s)"
        # 短窗口急跌：最近4个tick下跌超过0.25%，跳过开格（比20tick窗口更敏感）
        if self._sens.short_velocity_pct < self._SHORT_VELOCITY_ALARM_PCT:
            return False, f"short_drop({self._sens.short_velocity_pct*100:.3f}%/4tick)"

        # 4. 资金费率
        time_to_fund = (self._next_funding_ms / 1000.0 - now) if self._next_funding_ms > 0 else 9999.0
        if self._funding_rate > self._FUNDING_RATE_MAX and time_to_fund < self._FUNDING_PAUSE_WINDOW:
            return False, f"funding_risk(rate={self._funding_rate:.5f} eta={time_to_fund:.0f}s)"

        # 5. 波动率状态
        # DEAD：active_levels() 返回 1（单档观察），允许入场；仅 EXTREME 才完全禁止。
        vr = self._vol.vol_regime
        if vr == VolRegime.EXTREME:
            return False, "vol_regime_extreme"

        return True, ""

    def _check_leverage_safety(self, mid: float, equity: float | None) -> tuple[bool, str]:
        """
        检查杠杆安全性：
        - 账户权益是否充足
        - 保证金占比是否超限
        - 是否接近爆仓
        返回 (safe, reason_if_not_safe)。
        """
        if equity is not None:
            if equity < self._MIN_EQUITY_USDT:
                return False, f"equity_too_low({equity:.2f}<{self._MIN_EQUITY_USDT})"
            margin_used = self._total_margin_used(mid)
            if equity > 0 and margin_used / equity > self._MAX_MARGIN_USE_PCT:
                return False, f"margin_overuse({margin_used:.2f}/{equity:.2f}={margin_used/equity:.0%})"

        if self._total_held > 0:
            liq = self._liq_price()
            if liq > 0 and mid > 0:
                dist = (mid - liq) / mid
                if dist < self._LIQ_WARN_DISTANCE:
                    return False, f"near_liquidation(mid={mid:.2f} liq={liq:.2f} dist={dist:.2%})"

        return True, ""

    # ══════════════════════════════════════════════════════════════════════════
    # 订单操作（精度对齐 + 失败分类 + 指数退避）
    # ══════════════════════════════════════════════════════════════════════════

    def _place_entry(self, slot: GridSlot, now: float) -> bool:
        """
        下 post_only 限价买单。
        根据失败原因决定是否重试以及等待时长。
        """
        if now < slot.retry_after_ts:
            return False  # 退避等待中

        try:
            resp = self._rest.request("POST", "/api/v5/trade/order", {
                "instId": self._inst_id,
                "tdMode": self._td_mode,
                "side": "buy",
                "ordType": "post_only",
                "sz": self._sz(slot.contracts),
                "px": self._px(slot.target_price),
            })
            row = (resp.get("data") or [{}])[0]
            oid   = str(row.get("ordId") or "")
            scode = str(row.get("sCode") or "0")

            if oid and scode == "0":
                slot.entry_order_id = oid
                slot.state          = _S.ENTRY_LIVE
                slot.entry_ts       = now
                slot.fail_count     = 0
                slot.retry_after_ts = 0.0
                log.info("[grid] L%d 挂单 buy@%s ordId=%s", slot.level, self._px(slot.target_price), oid)
                return True

            # 下单被拒绝，分类处理
            self._handle_entry_rejection(slot, scode, now)
            return False

        except Exception as e:
            log.warning("[grid] L%d 下单异常: %s", slot.level, e)
            self._apply_backoff(slot, now)
            return False

    def _handle_entry_rejection(self, slot: GridSlot, scode: str, now: float) -> None:
        """根据 OKX 错误码决定重试策略。"""
        if scode == _ERR_POST_ONLY:
            # 价格已穿越盘口：稍等 2s 后用更低价格重试（让出更多空间）
            slot.target_price = self._round_price(slot.target_price * 0.9999)
            slot.retry_after_ts = now + 2.0
            log.info("[grid] L%d post_only 拒绝，降价后 2s 重试 px=%s", slot.level, self._px(slot.target_price))

        elif scode == _ERR_LOT_SIZE:
            # 张数精度问题：修正后立即重试
            slot.contracts = self._round_sz(slot.contracts)
            slot.retry_after_ts = now + 0.5
            log.warning("[grid] L%d 张数精度错误，修正为 %.1f", slot.level, slot.contracts)

        elif scode == _ERR_NO_MARGIN:
            # 保证金不足：不重试，记录告警
            slot.fail_count += 1
            slot.retry_after_ts = now + 600.0   # 10 分钟后再试
            log.error("[grid] L%d 保证金不足，暂停 10 分钟", slot.level)

        elif scode == _ERR_PRICE_BAND:
            # 价格超出涨跌停：不重试
            slot.retry_after_ts = now + 30.0
            log.warning("[grid] L%d 价格超出涨跌停限制", slot.level)

        else:
            # 未知错误：指数退避
            self._apply_backoff(slot, now)
            log.warning("[grid] L%d 未知拒绝 sCode=%s", slot.level, scode)

    def _apply_backoff(self, slot: GridSlot, now: float) -> None:
        """指数退避：根据 fail_count 决定等待时长。"""
        slot.fail_count += 1
        idx = min(slot.fail_count - 1, len(self._ENTRY_RETRY_BACKOFF) - 1)
        wait = self._ENTRY_RETRY_BACKOFF[idx]
        slot.retry_after_ts = now + wait
        log.info("[grid] L%d 退避等待 %.0fs (第%d次失败)", slot.level, wait, slot.fail_count)

    def _cancel_order(self, oid: str) -> bool:
        if not oid:
            return True
        try:
            self._rest.request("POST", "/api/v5/trade/cancel-order", {
                "instId": self._inst_id,
                "ordId": oid,
            })
            return True
        except Exception as e:
            log.warning("[grid] 撤单失败 %s: %s", oid, e)
            return False

    def _query_order(self, oid: str) -> dict[str, Any]:
        if not oid:
            return {}
        try:
            resp = self._rest.request(
                "GET",
                f"/api/v5/trade/order?instId={self._inst_id}&ordId={oid}",
            )
            data = resp.get("data") or []
            return data[0] if data else {}
        except Exception as e:
            log.warning("[grid] 查单失败 %s: %s", oid, e)
            return {}

    def _place_tp(self, contracts: float, tp_price: float) -> str:
        """挂 post_only 限价卖单（reduce_only），返回 ordId 或空串。"""
        # 确保 TP 价格高于 VWAP + 最小盈利空间（避免挂亏损TP）
        min_tp = self._vwap * (1.0 + self._fee_bps / 10000.0 * 1.5)
        tp_price = max(tp_price, min_tp)
        try:
            resp = self._rest.request("POST", "/api/v5/trade/order", {
                "instId": self._inst_id,
                "tdMode": self._td_mode,
                "side": "sell",
                "ordType": "post_only",
                "sz": self._sz(contracts),
                "px": self._px(tp_price),
                "reduceOnly": True,
            })
            row   = (resp.get("data") or [{}])[0]
            oid   = str(row.get("ordId") or "")
            scode = str(row.get("sCode") or "0")
            if oid and scode == "0":
                log.info("[grid] TP 挂单 sell@%s x%s ordId=%s", self._px(tp_price), self._sz(contracts), oid)
                return oid
            log.warning("[grid] TP 下单失败 sCode=%s", scode)
            return ""
        except Exception as e:
            log.warning("[grid] TP 下单异常: %s", e)
            return ""

    def _market_close_all(self, mid: float, reason: str) -> None:
        """市价平仓所有持仓槽位，记录盈亏。"""
        held = [s for s in self._slots if s.state == _S.HOLDING and s.fill_sz > 0]
        total = sum(s.fill_sz for s in held)
        if total <= 0:
            return
        try:
            self._rest.request("POST", "/api/v5/trade/order", {
                "instId": self._inst_id,
                "tdMode": self._td_mode,
                "side": "sell",
                "ordType": "market",
                "sz": self._sz(total),
                "reduceOnly": True,
            })
            log.warning("[grid] 市价平仓 %s张 @%.2f reason=%s", self._sz(total), mid, reason)
        except Exception as e:
            log.error("[grid] 市价平仓失败: %s", e)

        for s in held:
            pnl_pct = (mid - s.fill_price) / s.fill_price if s.fill_price > 0 else 0.0
            net = pnl_pct * self._notional(s.fill_sz, mid) * self._leverage
            # 紧急平仓：入场 maker(2bps) + 市价 taker(5bps) = 7bps，比常规 4bps 高
            fee = self._notional(s.fill_sz, mid) * self._EMERGENCY_CLOSE_FEE_BPS / 10000.0
            net_after = net - fee
            self._pnl.add(net_after)
            self._tracker.record(
                channel="grid",
                pnl_pct=pnl_pct,
                mid_price=mid,
                contracts=s.fill_sz,
                exit_reason=f"force_{reason}",
            )
            s.state      = _S.EMPTY
            s.entry_order_id = ""
            s.fill_price = 0.0
            s.fill_sz    = 0.0

    # ══════════════════════════════════════════════════════════════════════════
    # 紧急平仓
    # ══════════════════════════════════════════════════════════════════════════

    def _emergency_close(self, reason: str, mid: float) -> None:
        if self._emergency_closing:
            return
        self._emergency_closing = True
        log.warning("[grid] ═══ 紧急平仓 reason=%s ═══", reason)

        # 取消所有入场单
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                self._cancel_order(s.entry_order_id)
                s.state = _S.EMPTY
                s.entry_order_id = ""

        # 取消 TP 单
        if self._tp_order_id:
            self._cancel_order(self._tp_order_id)
            self._tp_order_id = ""

        # 市价平仓
        self._market_close_all(mid, reason)
        self._reset_grid()

        # 记录本次止损
        now = time.time()
        self._stop_times.append(now)
        # 清理 1h 窗口外的记录
        while self._stop_times and now - self._stop_times[0] > 3600:
            self._stop_times.popleft()

        # 决定冷静期时长
        stop_count_1h = len(self._stop_times)
        if stop_count_1h >= self._STOP_COUNT_1H_LIMIT:
            wait = self._EXTENDED_COOLDOWN
            log.warning("[grid] 1小时内止损 %d 次，延长冷静期 %.0fs", stop_count_1h, wait)
        else:
            wait = self._cooldown

        self._last_stop_ts = now
        self._cooldown_until = now + wait
        self._emergency_closing = False

    # ══════════════════════════════════════════════════════════════════════════
    # 网格管理
    # ══════════════════════════════════════════════════════════════════════════

    def _reset_grid(self) -> None:
        for s in self._slots:
            s.state = _S.EMPTY
            s.entry_order_id = ""
            s.fill_price = 0.0
            s.fill_sz    = 0.0
            s.fail_count = 0
            s.retry_after_ts = 0.0
        self._tp_order_id  = ""
        self._tp_price     = 0.0
        self._tp_placed_ts = 0.0
        self._total_held   = 0.0
        self._vwap_value   = 0.0
        self._vwap         = 0.0
        self._grid_active  = False
        self._grid_center  = 0.0

    def _place_grid(self, center: float, regime: Regime, now: float) -> None:
        """
        以 center 为基准放置网格：
        - TRENDING_UP：向上偏置，买单更接近当前价（等回调，不追跌）
        - RANGING：标准向下展开
        """
        n_active = self._vol.active_levels(self._max_levels)
        if n_active == 0:
            log.info("[grid] 波动率状态=%s，跳过开格", self._vol.vol_regime)
            return

        spacing = self._vol.spacing_pct(self._atr_mult, self._min_sp, self._max_sp)
        # 负资金费率（空头溢价）时减少1档，降低多头暴露
        if self._funding_rate < -0.0003 and n_active > 1:
            n_active -= 1
            log.info("[grid] 负资金费率 %.5f，激活档位减1 → %d", self._funding_rate, n_active)

        # FGI 极度恐慌（< 25）时再减1档，恐慌行情下降低多头敞口
        if self._fear_greed_index < 25 and n_active > 1:
            n_active -= 1
            log.info("[grid] 极度恐慌 FGI=%d，激活档位减1 → %d", self._fear_greed_index, n_active)

        # TRENDING_UP：买单更靠近当前价（期望浅回调），格宽放大1.3倍让TP更远以捕捉更多上行利润
        if regime == Regime.TRENDING_UP:
            bias = 0.5
            spacing = min(spacing * 1.3, self._max_sp)
            # 贪婪市场（FGI>60）+ 上升趋势：顺势多激活1档（行情好多赚）
            # 但负资金费率（< -0.0003）时不加档：负费率已触发减1档惩罚，加档会抵消保护效果
            if (
                self._fear_greed_index > 60
                and n_active < self._max_levels
                and self._funding_rate >= -0.0003
            ):
                n_active += 1
                log.info(
                    "[grid] 贪婪 FGI=%d + TRENDING_UP，激活档位加1 → %d",
                    self._fear_greed_index, n_active,
                )
        else:
            bias = 1.0

        self._grid_spacing  = spacing
        self._grid_center   = center
        self._active_levels = n_active
        self._grid_active   = True
        placed = 0

        for i, s in enumerate(self._slots):
            if i >= n_active:
                break
            if s.state != _S.EMPTY:
                continue
            s.target_price    = center * (1.0 - spacing * (i + 1) * bias)
            s.last_attempt_ts = now
            if self._place_entry(s, now):
                placed += 1

        log.info(
            "[grid] 网格启动 regime=%s center=%.2f spacing=%.4f%% "
            "levels=%d/%d placed=%d vol=%s",
            regime.value, center, spacing * 100,
            placed, n_active, placed, self._vol.vol_regime,
        )

    def _update_tp(self) -> None:
        """取消旧 TP，以当前 VWAP + 格宽重新挂单。
        RANGING 模式用 0.8×spacing（快速小利润，避免横盘回撤吃掉浮盈）；
        其他模式（TRENDING_UP 等）保持 1.0×spacing。
        """
        if self._total_held <= 0:
            return
        if self._tp_order_id:
            self._cancel_order(self._tp_order_id)
            self._tp_order_id = ""
        tp_mult = 0.8 if self._regime.current == Regime.RANGING else 1.0
        tp = self._vwap * (1.0 + self._grid_spacing * tp_mult)
        self._tp_price = tp
        oid = self._place_tp(self._total_held, tp)
        if oid:
            self._tp_order_id = oid
            self._tp_placed_ts = time.time()

    def _maybe_trail_tp(self, mid: float) -> None:
        """
        TP 追踪：若市场已大幅超过 TP 价格（说明价格继续涨），
        上调 TP 以锁住更多利润。
        RANGING 模式：触发阈值收窄至 0.3格宽，步长收窄至 0.15格宽（价格快速反转，需更早锁定）。
        其他模式：触发阈值 0.4格宽，步长 0.25格宽（给趋势更多空间）。
        节流：两次追踪间隔不小于 _TP_TRAIL_MIN_INTERVAL（30s），避免频繁 API 调用。
        成功追踪后重置 _tp_placed_ts，给TP新的超时窗口（市场上行时不应过早止损）。
        """
        if not self._tp_order_id or self._tp_price <= 0:
            return
        now = time.time()
        if now - self._last_tp_trail_ts < self._TP_TRAIL_MIN_INTERVAL:
            return  # 节流：避免每个 tick 都 cancel/replace TP 单
        spacing_abs = self._grid_spacing * self._vwap
        # RANGING 均值回归：更早追踪（0.3格）+ 更近落点（0.15格），确保在价格反转前锁定利润
        is_ranging = self._regime.current == Regime.RANGING
        trail_trigger_mult = 0.3 if is_ranging else 0.4
        trail_step_mult    = 0.15 if is_ranging else 0.25
        if mid > self._tp_price + spacing_abs * trail_trigger_mult:
            new_tp = mid - spacing_abs * trail_step_mult
            log.info(
                "[grid] TP 追踪上调(%s)：mid=%.2f > tp=%.2f + %.2f格，新TP=%.2f",
                "ranging" if is_ranging else "default",
                mid, self._tp_price, trail_trigger_mult, new_tp,
            )
            if new_tp > self._tp_price:
                self._cancel_order(self._tp_order_id)
                self._tp_order_id = ""
                self._tp_price = new_tp
                oid = self._place_tp(self._total_held, new_tp)
                if oid:
                    self._tp_order_id = oid
                    self._tp_placed_ts = now   # 重置超时计时器：市场上行时不应过早触发止损
                self._last_tp_trail_ts = now   # 无论成功与否都更新节流时间戳

    def _reset_grid_state(self, reason: str, now: float, cooldown: float = 10.0) -> None:
        """统一网格重置入口：撤销所有入场挂单，清空网格状态，设冷静期。"""
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                self._cancel_order(s.entry_order_id)
                s.state = _S.EMPTY
                s.entry_order_id = ""
        self._grid_active = False
        self._grid_center = 0.0
        self._grid_spacing = 0.0
        self._cooldown_until = max(getattr(self, "_cooldown_until", 0.0), now + cooldown)
        log.info("[grid] 网格重置: %s | 冷静期 %.0fs", reason, cooldown)

    def _maybe_recenter(self, mid: float, now: float) -> None:
        """
        价格偏离检测：两种触发条件：
        1. 距离触发：偏离 > recenter_mult × spacing（缩短至 1.5 倍）
        2. 穿叉触发：任意 EMPTY 槽位的计算目标价 >= 当前 bid（说明中心已失效）
        """
        if not self._grid_center or not self._grid_spacing:
            return
        if self._total_held > 0:
            return  # 有持仓不重置

        # ── 条件1：距离偏离 ─────────────────────────────────────────────────
        deviation = abs(mid - self._grid_center) / self._grid_center
        threshold = self._recenter * self._grid_spacing   # recenter_mult × spacing
        if deviation > threshold:
            self._reset_grid_state(
                f"距离偏离 {deviation*100:.4f}%>{threshold*100:.4f}%", now, cooldown=8.0
            )
            return

        # ── 条件2：买单价格穿越盘口（买单 >= bid → post_only 必败）──────────
        if self._last_bid > 0:
            for s in self._slots:
                if s.state == _S.EMPTY:
                    calc_px = self._grid_center * (1.0 - self._grid_spacing * (s.level + 1))
                    if calc_px >= self._last_bid:
                        self._reset_grid_state(
                            f"L{s.level}目标价 {calc_px:.2f} >= bid {self._last_bid:.2f}",
                            now, cooldown=5.0,
                        )
                        return

    # ══════════════════════════════════════════════════════════════════════════
    # 订单同步
    # ══════════════════════════════════════════════════════════════════════════

    def _sync_orders(self, now: float) -> None:
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                self._sync_entry(s, now)
        if self._tp_order_id:
            self._sync_tp(now)

    def _sync_entry(self, s: GridSlot, now: float) -> None:
        order = self._query_order(s.entry_order_id)
        if not order:
            return
        state   = str(order.get("state", ""))
        fill_sz = float(order.get("fillSz") or 0.0)
        fill_px = float(order.get("avgPx") or order.get("fillPx") or s.target_price)

        if state == "filled":
            actual_fill = fill_sz if fill_sz > 0 else s.contracts
            s.fill_price = fill_px
            s.fill_sz    = actual_fill
            s.fill_ts    = now
            s.state      = _S.HOLDING
            s.entry_order_id = ""
            # 更新 VWAP
            self._vwap_value += fill_px * actual_fill
            self._total_held += actual_fill
            self._vwap = self._vwap_value / self._total_held
            log.info(
                "[grid] L%d 成交 fill=%.2f sz=%.1f vwap=%.2f total=%.1f张",
                s.level, fill_px, actual_fill, self._vwap, self._total_held,
            )
            try:
                from quant.detailed_daily_log import record_analysis
                record_analysis(
                    "fill_entry",
                    level=s.level,
                    fill_price=fill_px,
                    fill_sz=actual_fill,
                    target_price=s.target_price,
                    vwap=self._vwap,
                    total_held=self._total_held,
                )
            except Exception:
                pass
            self._update_tp()

        elif state == "partially_filled":
            # 部分成交：等待继续成交，记录当前已成交量
            if fill_sz > 0 and fill_sz != s.fill_sz:
                log.info("[grid] L%d 部分成交 filled=%.1f", s.level, fill_sz)
                s.fill_sz = fill_sz  # 记录部分成交量

        elif state in ("canceled", "partially_canceled"):
            # 部分成交后撤销：对已成交部分计为持仓
            if fill_sz > 0:
                s.fill_price = fill_px
                s.fill_sz    = fill_sz
                s.fill_ts    = now
                s.state      = _S.HOLDING
                s.entry_order_id = ""
                self._vwap_value += fill_px * fill_sz
                self._total_held += fill_sz
                self._vwap = self._vwap_value / self._total_held
                log.info("[grid] L%d 部分成交后撤销 filled=%.1f", s.level, fill_sz)
                self._update_tp()
            else:
                log.info("[grid] L%d 入场单已撤销（零成交）", s.level)
                s.state = _S.EMPTY
                s.entry_order_id = ""

        elif state == "live":
            if now - s.entry_ts > self._entry_to:
                log.info("[grid] L%d 入场单超时 %.0fs，撤销", s.level, self._entry_to)
                if self._cancel_order(s.entry_order_id):
                    s.state = _S.EMPTY
                    s.entry_order_id = ""

    def _sync_tp(self, now: float) -> None:
        order = self._query_order(self._tp_order_id)
        if not order:
            return
        state   = str(order.get("state", ""))
        fill_px = float(order.get("avgPx") or order.get("fillPx") or self._tp_price)
        fill_sz = float(order.get("fillSz") or 0.0)

        if state == "filled":
            actual_fill = fill_sz if fill_sz > 0 else self._total_held
            total_net = 0.0
            for s in self._slots:
                if s.state == _S.HOLDING and s.fill_sz > 0:
                    pnl_pct = (fill_px - s.fill_price) / s.fill_price
                    net = pnl_pct * self._notional(s.fill_sz, fill_px) * self._leverage
                    fee = self._roundtrip_fee(s.fill_sz, fill_px)
                    net_after = net - fee
                    total_net += net_after
                    self._pnl.add(net_after)
                    self._tracker.record(
                        channel="grid",
                        pnl_pct=pnl_pct,
                        mid_price=fill_px,
                        contracts=s.fill_sz,
                        exit_reason="tp",
                    )
            log.info(
                "[grid] TP 成交 @%.2f sz=%.1f net=%.4f USDT | 日累计=%.4f USDT",
                fill_px, actual_fill, total_net, self._pnl.realized,
            )
            try:
                from quant.detailed_daily_log import record_analysis
                record_analysis(
                    "fill_tp",
                    fill_price=fill_px,
                    fill_sz=actual_fill,
                    net_pnl_usdt=total_net,
                    daily_pnl_realized=self._pnl.realized,
                    entry_vwap=self._vwap,
                )
            except Exception:
                pass
            self._reset_grid()

        elif state in ("canceled", "partially_canceled"):
            # TP 部分成交后被撤：调整剩余持仓的 TP
            if fill_sz > 0:
                remaining = self._total_held - fill_sz
                log.warning("[grid] TP 部分成交 filled=%.1f remaining=%.1f，重新挂单", fill_sz, remaining)
                self._total_held = max(0.0, remaining)
                self._vwap_value = self._vwap * self._total_held
            self._tp_order_id = ""
            if self._total_held > 0:
                self._update_tp()
            else:
                self._reset_grid()

    # ══════════════════════════════════════════════════════════════════════════
    # 资金费率更新
    # ══════════════════════════════════════════════════════════════════════════

    def _refresh_funding(self, runtime: dict[str, Any], now: float) -> None:
        if now - self._last_fund_ts < self._FUNDING_CHECK_INTERVAL:
            return
        # 优先从 runtime 取（runner 已定期拉取）
        _srt = runtime.get("strategy_runtime") or {}
        fr = runtime.get("funding_rate") or _srt.get("funding_rate")
        nf = runtime.get("next_funding_time_ms") or _srt.get("next_funding_time_ms")
        if fr is not None:
            self._funding_rate    = float(fr)
            self._next_funding_ms = float(nf or 0.0)
            self._last_fund_ts    = now

    # ══════════════════════════════════════════════════════════════════════════
    # 恐贪指数（每小时更新）
    # ══════════════════════════════════════════════════════════════════════════

    def _refresh_fgi(self, now: float) -> None:
        """每小时从 alternative.me 获取一次恐贪指数，失败时保留上次缓存。"""
        if now - self._last_fgi_ts < 3600.0:
            return
        self._last_fgi_ts = now
        try:
            import urllib.request as _ur, json as _json
            with _ur.urlopen(
                "https://api.alternative.me/fng/?limit=1", timeout=5
            ) as r:
                d = _json.loads(r.read())["data"][0]
                self._fear_greed_index = int(d["value"])
                log.info(
                    "[grid] 恐贪指数更新: %d (%s)",
                    self._fear_greed_index, d["value_classification"],
                )
        except Exception as e:
            log.debug("[grid] 恐贪指数获取失败（保留缓存值 %d）: %s", self._fear_greed_index, e)

    # ══════════════════════════════════════════════════════════════════════════
    # 持仓同步校验
    # ══════════════════════════════════════════════════════════════════════════

    def _position_sync_check(self, runtime: dict[str, Any], now: float) -> None:
        """
        每 10s 从 runtime 中获取实际持仓，与内部记录对比并自动修复：
        - 交易所 > 内部 + 0.5张：补录差额，用 UPL 反推估算成本，重挂 TP
        - 交易所 < 内部 - 0.5张（幽灵仓）：以交易所为准，清除多余内部状态
        """
        if now - self._last_pos_sync < self._POSITION_SYNC_INTERVAL:
            return
        self._last_pos_sync = now
        strat_rt = runtime.get("strategy_runtime") or {}
        pos_summary = runtime.get("swap_position_summary") or strat_rt.get("swap_position_summary")
        if pos_summary is None:
            return
        exchange_long = float(pos_summary.get("long_sz") or 0.0)
        internal_held = self._total_held
        diff = exchange_long - internal_held
        # 同步阈值：半个槽位大小（适配 contracts_per_slot=0.2 等小规模配置），
        # 避免硬编码 0.5 在小账户下漏检 1-2 槽位的真实持仓差异
        _thresh = max(self._contracts_per_slot * 0.5, 0.05)

        if diff > _thresh:
            # 交易所有仓但内部无记录 → 用 UPL 反推估算成本价，补录内部状态
            long_upl = float(pos_summary.get("long_upl") or 0.0)
            mid = self._last_bid if self._last_bid > 0 else self._vwap
            if mid > 0 and exchange_long > 0:
                # UPL ≈ (mid - avg_entry) × sz × ct_val × leverage
                notional_factor = exchange_long * self._ct_val * self._leverage
                est_entry = mid - long_upl / notional_factor if notional_factor > 0 else mid
                # 合理性校验：成本价偏离当前价 >5% 则降级为用当前价
                if est_entry <= 0 or abs(est_entry - mid) / mid > 0.05:
                    est_entry = mid
            else:
                est_entry = mid if mid > 0 else 0.0

            log.warning(
                "[grid] 持仓不一致！交易所=%.3f 内部=%.3f 差额=%.3f est_entry=%.2f，自动补录",
                exchange_long, internal_held, diff, est_entry,
            )
            if est_entry > 0:
                self._vwap_value += est_entry * diff
                self._total_held = exchange_long
                self._vwap = self._vwap_value / self._total_held
                if self._grid_spacing <= 0:
                    self._grid_spacing = self._vol.spacing_pct(
                        self._atr_mult, self._min_sp, self._max_sp
                    ) or self._min_sp
                self._update_tp()
                log.info(
                    "[grid] 持仓修复完成：total_held=%.3f vwap=%.2f TP已补挂",
                    self._total_held, self._vwap,
                )

        elif diff < -_thresh:
            # 内部认为有仓但交易所实际为0 → 幽灵持仓，清除防止错误操作
            log.warning(
                "[grid] 幽灵持仓！交易所=%.3f < 内部=%.3f，清除内部状态",
                exchange_long, internal_held,
            )
            if exchange_long < _thresh:
                # 交易所基本无仓：全部清除
                self._reset_grid()
            else:
                # 交易所有部分仓位：以交易所为准等比缩减
                self._total_held = exchange_long
                self._vwap_value = self._vwap * exchange_long
                held = [s for s in self._slots if s.state == _S.HOLDING]
                kept = 0.0
                for s in held:
                    if kept + s.fill_sz <= exchange_long + _thresh:
                        kept += s.fill_sz
                    else:
                        s.state = _S.EMPTY
                        s.fill_sz = 0.0
                        s.fill_price = 0.0
                log.info("[grid] 幽灵仓缩减完成：total_held=%.3f", self._total_held)

    # ══════════════════════════════════════════════════════════════════════════
    # 定期状态日志
    # ══════════════════════════════════════════════════════════════════════════

    def _log_status(self, mid: float, regime: Regime, now: float) -> None:
        if now - self._last_status_ts < self._STATUS_LOG_INTERVAL:
            return
        self._last_status_ts = now
        liq = self._liq_price()
        unrealized = self._calc_unrealized(mid)
        slot_states = {s.level: s.state[:4] for s in self._slots}
        session = self._tracker.session_summary()
        log.info(
            "[grid·status] regime=%s vol=%s mid=%.2f | "
            "held=%.1f vwap=%.2f liq=%.2f | "
            "unreal=%.3f daily_pnl=%.3f | "
            "slots=%s session_wr=%.0f%%",
            regime.value, self._vol.vol_regime, mid,
            self._total_held, self._vwap, liq,
            unrealized, self._pnl.realized,
            slot_states,
            session.get("win_rate", 0) * 100,
        )
        if now - self._last_regime_stats_ts >= self._REGIME_STATS_INTERVAL:
            self._last_regime_stats_ts = now
            stats = self._regime.stats_summary()
            log.warning(
                "[grid·regime·stats] 每小时Regime分类统计: %s",
                " | ".join(
                    f"{r}:trades={v['trades']} wins={v['wins']} pnl={v['total_pnl']:.3f}U"
                    for r, v in stats.items()
                    if v["trades"] > 0
                ) or "暂无成交数据",
            )

        try:
            from quant.detailed_daily_log import record_analysis

            record_analysis(
                "grid_status",
                mid=mid,
                regime=regime.value,
                vol_regime=self._vol.vol_regime,
                liq_price=liq,
                unrealized_usdt=unrealized,
                daily_pnl_realized=self._pnl.realized,
                slot_states=slot_states,
                session_tracker=session,
                status_summary=self.status_summary(),
            )
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # 主循环
    # ══════════════════════════════════════════════════════════════════════════

    def on_tick(
        self,
        *,
        last: float,
        bid: float,
        ask: float,
        market_context: dict[str, Any] | None = None,
    ) -> OrderIntent | None:
        runtime: dict[str, Any] = (market_context or {})
        now = time.time()

        # 中间价：优先盘口均价，fallback last
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        elif last > 0:
            mid = last
        else:
            return None
        if bid > 0:
            self._last_bid = bid

        # 更新所有指标
        self._ema_fast.update(mid)
        self._ema_slow.update(mid)
        self._ema_macro.update(mid)
        self._vol.update(mid)
        self._sens.update(mid, now)
        self._warmup_ticks += 1

        if self._warmup_ticks < self._warmup_need:
            return None

        # 资金费率刷新
        self._refresh_funding(runtime, now)
        self._refresh_fgi(now)

        # ── 重启恢复：有持仓但无TP ────────────────────────────────────────────
        # 重启时 _cancel_stale_orders 会撤掉旧TP，而 _tp_order_id 初始化为 ""，
        # 导致持仓裸露（无止盈保护）。在此处检测并自动补挂TP。
        if self._total_held > 0 and not self._tp_order_id:
            if self._grid_spacing <= 0:
                self._grid_spacing = self._vol.spacing_pct(
                    self._atr_mult, self._min_sp, self._max_sp
                ) or self._min_sp
                log.info(
                    "[grid] 重启后持仓无TP，用vol格宽=%.4f%% 补挂TP",
                    self._grid_spacing * 100,
                )
            self._update_tp()

        # 持仓同步校验
        self._position_sync_check(runtime, now)

        # Regime 更新
        ema_f = self._ema_fast.value or mid
        ema_s = self._ema_slow.value or mid
        ema_macro = self._ema_macro.value or mid
        feat = {
            "mid": mid,
            "macro_bias": (mid - ema_macro) / ema_macro if ema_macro > 0 else 0.0,
            "trend_strength": (ema_f - ema_s) / ema_s if ema_s > 0 else 0.0,
            "rel_vol": self._vol.atr_short,
            "trend_up": (ema_f - ema_s) / ema_s > 0.00030 if ema_s > 0 else False,
            "trend_down": (ema_f - ema_s) / ema_s < -0.00030 if ema_s > 0 else False,
        }
        regime = self._regime.update(feat, now)

        # 定期状态日志
        self._log_status(mid, regime, now)

        # ── 1. 日亏损 / 峰值回撤检查 ────────────────────────────────────────
        unrealized = self._calc_unrealized(mid)
        should_stop, stop_reason = self._pnl.check_stop(unrealized)
        if should_stop:
            log.warning("[grid] 风控触发: %s", stop_reason)
            self._emergency_close(stop_reason, mid)
            return None

        # ── 2. 趋势过滤：危险 Regime 处理 ───────────────────────────────────
        # 策略：挂单立即撤销（无损）；持仓给 45s 宽限期让 TP 自然成交或价格恢复。
        # 45s 覆盖了 Regime 最小保持时间（20s），避免短暂下跌触发不必要割肉。
        # 浮亏超过 1U 则不等待，立即止损。
        if regime in (Regime.TRENDING_DOWN, Regime.VOLATILE):
            # 立即撤销所有入场挂单（无损操作）
            for s in self._slots:
                if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                    self._cancel_order(s.entry_order_id)
                    s.state = _S.EMPTY
                    s.entry_order_id = ""
            self._grid_active = False

            # 持仓宽限期：60s 或浮亏 > 1U 才触发平仓（60s 覆盖 Regime 最小保持20s，减少误割）
            has_holding = any(s.state == _S.HOLDING for s in self._slots)
            if has_holding:
                if self._bearish_regime_since == 0.0:
                    self._bearish_regime_since = now
                    log.info("[grid] Regime=%s 宽限期开始，持仓等待TP或价格恢复", regime.value)
                elapsed = now - self._bearish_regime_since
                if elapsed > 60.0 or unrealized < -1.0:
                    log.warning(
                        "[grid] Regime=%s 宽限到期 elapsed=%.0fs unreal=%.3fU，平仓",
                        regime.value, elapsed, unrealized,
                    )
                    self._emergency_close(f"regime_{regime.value}_t{elapsed:.0f}s", mid)
            else:
                self._bearish_regime_since = 0.0
            return None
        else:
            # 安全 Regime：重置宽限计时器
            self._bearish_regime_since = 0.0

        # ── 3. 冷静期 ──────────────────────────────────────────────────────
        cooldown_until = getattr(self, "_cooldown_until", 0.0)
        if now < cooldown_until:
            remaining = cooldown_until - now
            if now - self._last_cooldown_log_ts >= 30.0:
                self._last_cooldown_log_ts = now
                log.info("[grid] 冷静期 剩余%.0fs", remaining)
            return None

        # ── 4. 杠杆安全检查 ────────────────────────────────────────────────
        _strat_rt = runtime.get("strategy_runtime") or {}
        equity = float(
            runtime.get("usdt_avail_swap") or _strat_rt.get("usdt_avail_swap") or 0.0
        ) or None
        safe, safety_reason = self._check_leverage_safety(mid, equity)
        if not safe:
            log.warning("[grid] 杠杆安全警报: %s", safety_reason)
            if "near_liquidation" in safety_reason:
                self._emergency_close(safety_reason, mid)
            return None

        # ── 5. 同步订单状态（节流） ─────────────────────────────────────────
        if now - self._last_sync_ts >= self._sync_iv:
            self._sync_orders(now)
            self._last_sync_ts = now

        # ── 6a. 单仓硬止损（每个 HOLDING 槽位独立评估） ───────────────────
        # 单槽浮亏公式与 _calc_unrealized 一致：
        #   pnl_pct = (mid - fill_price) / fill_price
        #   slot_upl = pnl_pct * notional(fill_sz, mid) * leverage
        # 任一槽位超过 per_slot_stop 立即触发紧急平仓（整仓）
        # 原因：小账户下，即使只有一个槽位失控，也会迅速穿透整体止损阈值；
        # 所以"快一步"在单仓层先行截断
        if self._per_slot_stop > 0:
            for s in self._slots:
                if s.state != _S.HOLDING or s.fill_sz <= 0 or s.fill_price <= 0:
                    continue
                pnl_pct = (mid - s.fill_price) / s.fill_price
                slot_upl = pnl_pct * self._notional(s.fill_sz, mid) * self._leverage
                if slot_upl <= -self._per_slot_stop:
                    log.warning(
                        "[grid] 单仓硬止损: L%d 浮亏=%.4f USDT (fill=%.2f, mid=%.2f, sz=%.3f)",
                        s.level, slot_upl, s.fill_price, mid, s.fill_sz,
                    )
                    self._emergency_close(f"per_slot_stop_L{s.level}", mid)
                    return None

        # ── 6. 整体浮亏止损 ─────────────────────────────────────────────────
        unrealized = self._calc_unrealized(mid)
        if unrealized <= -self._whole_stop:
            log.warning("[grid] 整体止损: 浮亏=%.4f USDT", unrealized)
            self._emergency_close("whole_grid_stop", mid)
            return None

        # ── 7. TP 追踪（市场大涨时上移 TP） ─────────────────────────────────
        if self._total_held > 0:
            self._maybe_trail_tp(mid)
            # ── 7b. TP 超时止损：持仓超过N分钟且（价格跌破VWAP-1格宽 OR 浮亏>0.5U）
            # TRENDING_UP 时延长至10分钟：上升趋势中 TP 需更多时间触发，不应过早止损
            _TP_AGING_SEC = 600.0 if regime == Regime.TRENDING_UP else 480.0
            _tp_price_breach = self._vwap > 0 and mid < self._vwap * (1.0 - self._grid_spacing)
            _tp_loss_breach = unrealized < -0.5  # 浮亏超0.5U触发止损
            if (
                self._tp_placed_ts > 0
                and now - self._tp_placed_ts > _TP_AGING_SEC
                and (_tp_price_breach or _tp_loss_breach)
            ):
                log.warning(
                    "[grid] TP 超时止损: 持仓%.0fs mid=%.2f < vwap=%.2f-格宽",
                    now - self._tp_placed_ts, mid, self._vwap,
                )
                self._emergency_close("tp_timeout_stoploss", mid)
                return None

        # ── 8. 利润保护模式 ─────────────────────────────────────────────────
        if self._pnl.profit_protect_mode():
            # 已达日目标：只管存量仓位等 TP，不开新格
            if not getattr(self, "_profit_protect_logged", False):
                log.info("[grid] 日收益目标达成 %.4f USDT，进入利润保护模式", self._pnl.realized)
                self._profit_protect_logged = True
            return None

        # ── 9. 网格中心偏移检查 ─────────────────────────────────────────────
        self._maybe_recenter(mid, now)

        # ── 10. 市场条件检查（新开格前）───────────────────────────────────
        market_ok, market_reason = self._market_ok_to_enter(runtime, mid, now, bid=bid, ask=ask)

        # ── 10b. 宏观趋势过滤（macro_bias = mid偏离5分钟EMA）───────────────
        # macro_bias < -0.002 = 价格跌破5分钟均线0.2%，与 regime.py _MACRO_DOWN_KILL 对齐
        # 原阈值 -0.0015 比 regime 更保守，导致 Regime=RANGING 时格仍不开（矛盾）
        macro_bias = feat["macro_bias"]
        macro_bearish = macro_bias < -0.0020
        if macro_bearish and not self._grid_active:
            if market_ok:  # 仅在原本可开格时才记日志（避免刷屏）
                log.info("[grid] 宏观偏空 macro_bias=%.4f，跳过开格", macro_bias)
            return None

        # ── 11. 激活网格 ───────────────────────────────────────────────────
        if not self._grid_active and market_ok and regime in (Regime.RANGING, Regime.TRENDING_UP):
            self._profit_protect_logged = False
            self._place_grid(mid, regime, now)
            return None

        # ── 12. 补充空置槽位 ───────────────────────────────────────────────
        if self._grid_active and market_ok:
            for s in self._slots:
                if (
                    s.state == _S.EMPTY
                    and s.level < self._active_levels
                    and now - s.last_attempt_ts > 5.0
                    and now >= s.retry_after_ts
                    and self._grid_center > 0
                ):
                    calc_px = self._grid_center * (
                        1.0 - self._grid_spacing * (s.level + 1)
                    )
                    # 计算目标价越叉：买单 >= bid → 中心已过期，触发网格重置
                    if self._last_bid > 0 and calc_px >= self._last_bid:
                        self._reset_grid_state(
                            f"补仓L{s.level}目标价{calc_px:.2f}>=bid{self._last_bid:.2f}",
                            now, cooldown=5.0,
                        )
                        break
                    s.target_price = calc_px
                    s.last_attempt_ts = now
                    self._place_entry(s, now)

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # 状态摘要
    # ══════════════════════════════════════════════════════════════════════════

    def status_summary(self) -> dict[str, Any]:
        held = {s.level: {"fill": s.fill_price, "sz": s.fill_sz}
                for s in self._slots if s.state == _S.HOLDING}
        live = [s.level for s in self._slots if s.state == _S.ENTRY_LIVE]
        return {
            "regime":        self._regime.current.value,
            "vol_regime":    self._vol.vol_regime,
            "atr_short_bps": round(self._vol.atr_short * 10000, 2),
            "atr_medium_bps": round(self._vol.atr_medium * 10000, 2),
            "grid_active":   self._grid_active,
            "grid_center":   round(self._grid_center, 2),
            "grid_spacing_bps": round(self._grid_spacing * 10000, 2),
            "active_levels": self._active_levels,
            "slots_live":    live,
            "slots_holding": held,
            "total_held":    self._total_held,
            "vwap":          round(self._vwap, 2),
            "tp_price":      round(self._tp_price, 2),
            "liq_price":     round(self._liq_price(), 2),
            "daily_pnl":     round(self._pnl.realized, 4),
            "profit_protect": self._pnl.profit_protect_mode(),
            "funding_rate":  self._funding_rate,
            "session":       self._tracker.session_summary(),
        }
