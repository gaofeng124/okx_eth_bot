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
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
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
_ERR_POST_ONLY   = "51016"   # post_only 会穿越盘口，拒绝 → 调整价格重试
_ERR_NO_MARGIN   = "51008"   # 保证金不足 → 不重试，报警
_ERR_PRICE_BAND  = "51006"   # 价格超出涨跌停 → 跳过
_ERR_LOT_SIZE    = "51020"   # 张数精度错误 → 修正 sz 重试
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
        # L10-001 续（2026-04-21 21:55）：ETH 晚间 15m range 40-80bps 是正常波动，
        #   实测 21:XX 平均 46bps 就被判 EXTREME=停止开格 → 主人加仓 144U 在此期间完全闲置。
        #   改为：EXTREME 门槛 40→80bps（只拦截真正的闪崩/急拉）
        atr = self.atr_medium
        if self._tick_count < 20:   return VolRegime.CALM    # 数据不足→默认 CALM
        if atr < 0.000005: return VolRegime.DEAD             # <0.05bps：市场真正冻结（REST过滤后仍为0）
        if atr < 0.0008:  return VolRegime.CALM              # 0.05-8bps：正常（含 REST 模式低波动）
        if atr < 0.0025:  return VolRegime.NORMAL            # 8-25bps：活跃
        if atr < 0.0080:  return VolRegime.ELEVATED          # 25-80bps：高波动（原 25-40bps，阈值提高）
        return VolRegime.EXTREME                              # >80bps：极端（原 >40bps，太敏感）

    def active_levels(self, max_levels: int = 4) -> int:
        """根据波动率状态决定激活几档网格。
        L10-001 修复（2026-04-21 21:30）：放宽 CALM/ELEVATED 档位限制以提高资金利用率。
        - 原：CALM/ELEVATED 限 2 档 → 186U 账户利用率仅 7.5%
        - 新：CALM/ELEVATED 改 min(3, max) → 常态利用率 20-35%

        L10-001 续（2026-04-21 21:55）：EXTREME 从"完全停止"改为"挂 1 档观察"
        - 原：EXTREME 返回 0 → 策略完全熔断
        - 新：EXTREME 返回 1 → 挂 1 档观察，保留最小资金流动性
        - 理由：真正极端行情（>80bps）也不该让账户完全闲置，1 档有限敞口可观察
        """
        vr = self.vol_regime
        if vr == VolRegime.DEAD:     return 1                  # 极低波动：挂 1 档观察
        if vr == VolRegime.CALM:     return min(3, max_levels)  # 原 2 → 3
        if vr == VolRegime.NORMAL:   return max_levels
        if vr == VolRegime.ELEVATED: return min(3, max_levels)  # 原 2 → 3
        return 1  # EXTREME：原 0（完全停）→ 1（挂 1 档，保留流动性）

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

    def set_dynamic_drawdown_limit(self, equity: float | None) -> None:
        """动态调整峰值回撤上限：max(1.5U, equity×4%)，随账户余额自适应。"""
        if equity and equity > 0:
            self._drawdown_limit = max(1.5, equity * 0.04)

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
    _SHORT_VELOCITY_ALARM_PCT = -0.0025 # -0.25% / 4tick 短窗口急跌过滤（更敏感）
    _FUNDING_PAUSE_WINDOW   = 600.0  # 距资金费结算 10min 内暂停开新格
    _FUNDING_RATE_MAX       = 0.0005 # 资金费率 > 0.05% 时抑制做多
    _STOP_COUNT_1H_LIMIT    = 3      # 1小时内触发3次止损 → 延长冷静期
    _EXTENDED_COOLDOWN      = 3600.0 # 延长冷静期 1h
    _PROFIT_HALF_LIFE       = 1800.0 # 保留为参考值；_ewma_profit_avg 已改为 Regime-specific（RANGING=900s, TRENDING=2700s）
    _MIN_EQUITY_USDT        = 15.0   # 账户权益低于此值停止一切操作
    _MAX_MARGIN_USE_PCT     = 0.70   # 最多使用 70% 账户权益做保证金
    _LIQ_WARN_DISTANCE      = 0.05   # 距爆仓价 < 5% 时告警并紧急平仓
    _MAINT_MARGIN_RATE      = 0.0065 # OKX ETH-USDT-SWAP 10x 维持保证金率
    _ENTRY_RETRY_BACKOFF    = [5.0, 15.0, 60.0, 300.0]  # 失败后等待秒数
    _TP_TRAIL_MIN_INTERVAL  = 30.0   # TP 追踪最小间隔（秒），避免频繁 cancel/replace
    _EMERGENCY_CLOSE_FEE_BPS = 7.0  # 紧急平仓费率：入场 maker(2bps) + 市价 taker(5bps)
    # TP 追踪基准参数（round45：提取为命名常量便于集中调参；RANGING trigger 1.00→1.05）
    # RANGING 1.05：在neutral区（avg 0.40~0.80）需价格超出TP 1.05格才触发trail，
    #   比旧值1.00多5%缓冲，减少震荡行情中的误触发（短暂超冲后回落不触trail）
    _RANGING_TRAIL_BASE_TRIGGER  = 1.05
    _RANGING_TRAIL_BASE_OFFSET   = 0.50
    _TRENDING_TRAIL_BASE_TRIGGER = 1.20
    _TRENDING_TRAIL_BASE_OFFSET  = 0.60

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
        tp_mult: float = 1.0,
        grid_direction: str = "long",
        contracts_per_slot_short: float = 0.1,
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

        # ── 方向配置（双向网格支持）────────────────────────────────────────────
        # grid_direction="long" → 传统做多网格（买低卖高）
        # grid_direction="short"→ 做空网格（卖高买低），镜像逻辑
        # 无效输入时 fallback 到 "long"，保持向后兼容
        _dir = str(grid_direction or "long").lower().strip()
        if _dir not in ("long", "short"):
            log.warning("[grid] 非法 grid_direction=%r，fallback 为 long", grid_direction)
            _dir = "long"
        self._direction = _dir
        # _side 为当前活跃方向（未来扩展 "both" 模式时用）
        self._side = self._direction

        # 单格张数（与账户规模匹配：小账户用分数张；lotSz 通常 0.01 支持）
        # 做多 / 做空 各自独立配置，按当前方向选出 active 值
        self._contracts_per_slot_long  = max(0.01, float(contracts_per_slot))
        self._contracts_per_slot_short = max(0.01, float(contracts_per_slot_short))
        self._contracts_per_slot = (
            self._contracts_per_slot_short if self._side == "short"
            else self._contracts_per_slot_long
        )
        # 单仓硬止损（USDT）：任一 HOLDING 槽位浮亏超此值立即市价平该仓；0=关闭
        self._per_slot_stop = max(0.0, float(per_slot_stop_usdt))
        self._tp_mult = max(0.5, float(tp_mult))  # TP距离倍率，>=0.5防御

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
        self._grid_bias:    float = 1.0   # RANGING=1.0; TRENDING=0.5（补仓/重置用）
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
        self._tp_exposed_since: float = 0.0  # 裸仓计时：_place_tp 首次失败的时间戳；60s 未恢复触发强平

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
        self._data_dir = data_dir  # 冷启动 TP 历史恢复用

        # EMA（Regime 特征）
        self._ema_fast  = StreamingEMA(alpha=0.12)
        self._ema_slow  = StreamingEMA(alpha=0.04)
        self._ema_macro = StreamingEMA(alpha=0.003)
        self._ema_book_imb = StreamingEMA(alpha=0.08)  # L3-001: 盘口不平衡 EMA

        # 运行时状态
        self._warmup_ticks:  int   = 0
        self._last_bid:      float = 0.0   # 最新 bid（用于下单前校验价格不越叉）
        self._last_sync_ts:  float = 0.0
        self._last_pos_sync: float = 0.0
        self._sync_pending_ts: float = 0.0  # mid=0 时延迟对账的时间戳；bid 恢复后强制重试
        self._last_fund_ts:  float = 0.0
        self._last_status_ts: float = 0.0
        self._last_regime_stats_ts: float = 0.0
        self._last_stop_ts:  float = 0.0
        self._last_cooldown_log_ts: float = 0.0   # 冷静期日志节流
        self._last_tp_trail_ts: float = 0.0       # 上次 TP 追踪时间（节流用）
        # 分 Regime 的 TP 利润 EWMA：RANGING 和 TRENDING 市场利润特征不同，混合会稀释信号
        self._tp_profits_ranging:  deque[tuple[float, float]] = deque(maxlen=20)  # (ts, profit_spacings)
        self._tp_profits_trending: deque[tuple[float, float]] = deque(maxlen=20)
        # ATR 基线：慢速 EMA（α=0.05）追踪"正常"格宽水平，供 _update_tp 做动态 TP 距离缩放
        self._atr_baseline: float = 0.0
        self._last_atr_save_ts: float = 0.0   # 节流：最多每5分钟保存一次
        self._last_eff_tp_mult: float = 1.0   # _update_tp最近一次有效乘数（fill_tp诊断用）
        self._emergency_closing: bool = False
        self._emergency_close_failed_ts: float = 0.0  # 强平API失败时间戳；60s后circuit_break重试

        # 止损计数（1h 窗口）
        self._stop_times: deque[float] = deque()  # 触发止损的时间戳列表

        # 资金费率缓存
        self._funding_rate:     float = 0.0
        self._next_funding_ms:  float = 0.0

        # 危险 Regime 持仓宽限期（TRENDING_DOWN/VOLATILE 进入时不立即割肉，
        # 给 45s 让 TP 自然成交或价格恢复；浮亏 > 1U 则立即止损）
        self._bearish_regime_since: float = 0.0

        # 当前 Regime 缓存（每 tick 更新，供 _update_tp / _maybe_trail_tp 使用）
        self._current_regime: Regime = Regime.RANGING

        # 恐贪指数缓存（每小时更新；FGI < 25 极度恐慌时在 _place_grid 减1档）
        self._fear_greed_index: int   = 50
        self._last_fgi_ts:      float = 0.0

        # 【2026-04-22 改进 1-3】交易节流 + 价格位置 + 波动自适应
        self._recent_entries_ts: deque[float] = deque(maxlen=10)  # 最近开仓时间
        self._price_1h_cache = {"ts": 0.0, "hi": 0.0, "lo": 0.0}  # 1h 高低缓存
        # 1h方向gate滞回环状态（round36：防单阈值震荡，入0.99/出0.995）
        self._long_drop_gate: bool  = False  # LONG: 价格跌离1h高点>1%时激活
        self._short_rise_gate: bool = False  # SHORT: 价格涨离1h低点>1%时激活
        self._last_gate_log_ts: float = 0.0  # gate日志节流（60s一次，防每tick刷屏）
        self._price_1h_fail_count: int = 0  # 连续失败次数（指数退避用）
        # 【2026-04-22 17:30 盈亏比修复 改动 5】连亏冷静期
        # 主人："加注赔钱金额大" → 连 2 亏不加仓，30min 冷静
        self._loss_streak_until: float = 0.0  # 冷静期结束时间
        self._recent_close_pnls: deque[float] = deque(maxlen=3)  # 近 3 笔平仓 PnL
        self._last_sz_scale: float = 1.0          # 最近一次开仓的仓位缩减系数（离线分析用）
        self._last_gridstate_snap_ts: float = 0.0  # 5分钟 grid_state 快照节流时间戳

        # Phase 4 趋势日守卫（主人 2026-04-21 22:15 批准 B 激进版时要求）
        # 每 10min 评估近 4h K 线 delta：若 |delta| > 1.5% → 自动降回 Phase 3
        # 原因：90% 利用率 + 趋势日 = 必爆仓；grid 策略依赖震荡不是单边
        self._last_p4_guard_ts: float = 0.0
        self._p4_trend_guard_enabled: bool = os.getenv("GRID_PHASE4_TREND_GUARD", "0") == "1"

        # REST 客户端
        self._rest = OKXRestClient()

        # 启动对账
        self._boot_reconcile()

        # 冷启动 TP 历史恢复（使 EWMA 自适应重启后立即可用，无需等待5次新成交）
        self._replay_tp_history()

        # ATR 基线恢复（避免重启后冷启动期20次_place_grid调用无ATR联动）
        self._restore_atr_baseline()

        # loss_streak 冷静期恢复（跨重启保持冷静期，防崩溃后立即重开亏损仓）
        self._restore_loss_streak()

        # 关键风控配置一次性打印（便于日志核对）
        log.info(
            "[grid] 风控配置 direction=%s lev=%.1fx grid_levels=%d contracts_per_slot=%.3f "
            "whole_stop=%.2fU daily_stop=%.2fU per_slot_stop=%.2fU peak_dd=%.2fU "
            "ct_val_init=%.3f tp_mult=%.2f",
            self._direction, self._leverage, self._max_levels, self._contracts_per_slot,
            self._whole_stop, self._pnl._stop, self._per_slot_stop,
            self._pnl._drawdown_limit, self._ct_val, self._tp_mult,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 方向辅助（long / short 镜像计算）
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def _is_short(self) -> bool:
        """当前是否为做空方向。"""
        return self._side == "short"

    def _entry_api_side(self) -> str:
        """入场订单 side：做多=buy，做空=sell。"""
        return "sell" if self._is_short else "buy"

    def _exit_api_side(self) -> str:
        """出场（TP/紧急平仓）订单 side：做多=sell，做空=buy。"""
        return "buy" if self._is_short else "sell"

    def _grid_spacing_sign(self) -> float:
        """
        入场挂单方向：
          long  → 在中心下方挂买单（-1）
          short → 在中心上方挂卖单（+1）
        """
        return 1.0 if self._is_short else -1.0

    def _tp_spacing_sign(self) -> float:
        """
        TP 相对 VWAP 方向：
          long  → TP 在 VWAP 上方（+1）
          short → TP 在 VWAP 下方（-1）
        """
        return -1.0 if self._is_short else 1.0

    def _pnl_sign(self) -> float:
        """
        PnL 方向乘子：
          long  → (mid - fill) > 0 盈利（+1）
          short → (mid - fill) < 0 盈利（-1），用 raw * (-1) 得正值
        """
        return -1.0 if self._is_short else 1.0

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
        """同步已有持仓到内部状态，避免重启后不知道自己有仓。
        OKX net mode：pos 字段 >0 表示多仓，<0 表示空仓。
        若已有仓与配置 direction 冲突，大声警告但保留现状（用户选择优先）。
        """
        try:
            resp = self._rest.request(
                "GET",
                f"/api/v5/account/positions?instType=SWAP&instId={self._inst_id}",
            )
            for pos in (resp.get("data") or []):
                sz_raw = float(pos.get("pos") or 0)
                avg_px = float(pos.get("avgPx") or 0)
                if sz_raw == 0 or avg_px <= 0:
                    continue

                # 判定实际仓位方向并与配置比对
                is_long_pos = sz_raw > 0
                pos_dir = "long" if is_long_pos else "short"
                if pos_dir != self._direction:
                    log.warning(
                        "[grid] ⚠️ 检测到 %s 方向持仓 %.2f张 但配置 direction=%s —— "
                        "保持原持仓状态，策略将按配置方向操作（不会自动反手）",
                        pos_dir, sz_raw, self._direction,
                    )

                sz = abs(sz_raw)
                # 按 contracts_per_slot 计算需要多少个 slot
                per_slot = self._contracts_per_slot or 0.2
                slots_needed = math.ceil(sz / per_slot)
                slots_to_fill = min(slots_needed, len(self._slots))
                # 均匀分配持仓到 slot
                per_slot_sz = sz / slots_to_fill
                log.warning(
                    "[grid] 检测到已有%s仓 %.2f张 avgPx=%.2f → "
                    "分配到 %d 个 slot (每 slot %.2f张)",
                    pos_dir, sz, avg_px, slots_to_fill, per_slot_sz,
                )
                # 注意：_total_held 为正数（持仓量绝对值），方向由 self._side 决定
                self._total_held = sz
                self._vwap = avg_px
                self._vwap_value = sz * avg_px
                for i in range(slots_to_fill):
                    self._slots[i].state = _S.HOLDING
                    self._slots[i].fill_price = avg_px
                    self._slots[i].fill_sz = per_slot_sz
                    self._slots[i].fill_ts = time.time()
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
        rounded = self._round_sz(contracts)
        # Use :g format to avoid int() truncation (e.g. int(0.2)=0)
        # while still producing clean strings: 0.2→"0.2", 1.0→"1", 0.01→"0.01"
        return f"{rounded:g}"

    # ══════════════════════════════════════════════════════════════════════════
    # 辅助计算
    # ══════════════════════════════════════════════════════════════════════════

    def _notional(self, contracts: float, price: float) -> float:
        return contracts * self._ct_val * price

    def _roundtrip_fee(self, contracts: float, price: float) -> float:
        return self._notional(contracts, price) * self._fee_bps / 10000.0

    def _calc_unrealized(self, mid: float) -> float:
        total = 0.0
        sign = self._pnl_sign()
        for s in self._slots:
            if s.state == _S.HOLDING and s.fill_price > 0:
                # long:  (mid-fill)*sign(+1) → mid>fill 盈利
                # short: (mid-fill)*sign(-1) → mid<fill 盈利
                total += (mid - s.fill_price) * s.fill_sz * self._ct_val * sign
        return total

    def _liq_price(self) -> float:
        """估算当前净仓位的理论爆仓价（long 在下方，short 在上方）。"""
        if self._vwap <= 0 or self._total_held <= 0:
            return 0.0
        # isolated 逐仓：
        #   long:  liq ≈ avgPx × (1 - 1/lever + maint_margin)  （价格跌到此处爆仓）
        #   short: liq ≈ avgPx × (1 + 1/lever - maint_margin)  （价格涨到此处爆仓）
        if self._is_short:
            return self._vwap * (1.0 + 1.0 / self._leverage - self._MAINT_MARGIN_RATE)
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
        # long:  急跌时不应开多（接下落的刀）→ velocity < -0.0020
        # short: 急涨时不应开空（追被轧空的空头）→ velocity > +0.0020（镜像）
        if self._is_short:
            if self._sens.velocity_pct > -self._VELOCITY_ALARM_PCT:  # +0.0020
                return False, f"rising_knife({self._sens.velocity_pct*100:.3f}%/20tick)"
            if self._sens.short_velocity_pct > -self._SHORT_VELOCITY_ALARM_PCT:  # +0.0025
                return False, f"short_spike({self._sens.short_velocity_pct*100:.3f}%/4tick)"
        else:
            if self._sens.velocity_pct < self._VELOCITY_ALARM_PCT:
                return False, f"falling_knife({self._sens.velocity_pct*100:.3f}%/4s)"
            # 短窗口急跌：最近4个tick下跌超过0.25%，跳过开格（比20tick窗口更敏感）
            if self._sens.short_velocity_pct < self._SHORT_VELOCITY_ALARM_PCT:
                return False, f"short_drop({self._sens.short_velocity_pct*100:.3f}%/4tick)"

        # 4. 资金费率
        # long:  funding > +0.0005 时开新格接近结算会被扣费（不利）
        # short: funding < -0.0005 时开新格接近结算会被扣费（空头付费给多头）
        time_to_fund = (self._next_funding_ms / 1000.0 - now) if self._next_funding_ms > 0 else 9999.0
        if self._is_short:
            funding_adverse = self._funding_rate < -self._FUNDING_RATE_MAX
        else:
            funding_adverse = self._funding_rate > self._FUNDING_RATE_MAX
        if funding_adverse and time_to_fund < self._FUNDING_PAUSE_WINDOW:
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
                # long:  liq < mid，dist = (mid - liq) / mid
                # short: liq > mid，dist = (liq - mid) / mid
                dist = (liq - mid) / mid if self._is_short else (mid - liq) / mid
                if dist < self._LIQ_WARN_DISTANCE:
                    return False, f"near_liquidation(mid={mid:.2f} liq={liq:.2f} dist={dist:.2%})"

        return True, ""

    # ══════════════════════════════════════════════════════════════════════════
    # 订单操作（精度对齐 + 失败分类 + 指数退避）
    # ══════════════════════════════════════════════════════════════════════════

    def _place_entry(self, slot: GridSlot, now: float) -> bool:
        """
        下 post_only 限价入场单。
          long  → side="buy"  挂在中心下方
          short → side="sell" 挂在中心上方
        根据失败原因决定是否重试以及等待时长。
        """
        if now < slot.retry_after_ts:
            return False  # 退避等待中

        api_side = self._entry_api_side()
        try:
            resp = self._rest.request("POST", "/api/v5/trade/order", {
                "instId": self._inst_id,
                "tdMode": self._td_mode,
                "side": api_side,
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
                log.info(
                    "[grid] L%d 挂单 %s@%s ordId=%s",
                    slot.level, api_side, self._px(slot.target_price), oid,
                )
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
            # 价格已穿越盘口：稍等 2s 后重试（让出更多空间）
            #   long  → 买单降价（× 0.9999）
            #   short → 卖单升价（× 1.0001）
            if self._is_short:
                slot.target_price = self._round_price(slot.target_price * 1.0001)
                log_action = "升价"
            else:
                slot.target_price = self._round_price(slot.target_price * 0.9999)
                log_action = "降价"
            slot.retry_after_ts = now + 2.0
            log.info(
                "[grid] L%d post_only 拒绝，%s后 2s 重试 px=%s",
                slot.level, log_action, self._px(slot.target_price),
            )

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
            resp = self._rest.request("POST", "/api/v5/trade/cancel-order", {
                "instId": self._inst_id,
                "ordId": oid,
            })
            s_code = str((resp.get("data") or [{}])[0].get("sCode", "0"))
            if s_code != "0":
                if s_code == "51401":
                    # 订单不存在（已成交或已撤），视为成功
                    log.debug("[grid] 撤单 %s sCode=51401 订单不存在，视为成功", oid)
                    return True
                log.warning("[grid] 撤单 %s OKX拒绝 sCode=%s", oid, s_code)
                return False
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
        """挂限价 reduce_only 单（做多=sell，做空=buy），返回 ordId 或空串。
        正常用 post_only（maker 费率更低）。
        若 tp_price 已被市场越过（long: tp<=bid；short: tp>=ask），post_only 会被拒绝，
        改用 limit 确保能成交，避免持仓失去 TP 保护。
        """
        # 确保 TP 价格相对 VWAP 留出最小盈利空间（避免挂亏损TP）
        #   long:  tp >= vwap * (1 + fee_margin)
        #   short: tp <= vwap * (1 - fee_margin)
        fee_margin = self._fee_bps / 10000.0 * 1.5
        if self._is_short:
            max_tp = self._vwap * (1.0 - fee_margin)
            tp_price = min(tp_price, max_tp)
        else:
            min_tp = self._vwap * (1.0 + fee_margin)
            tp_price = max(tp_price, min_tp)

        api_side = self._exit_api_side()
        # 选择 ordType：TP 价格已穿越盘口 → limit（会立即成交），否则 post_only
        #   long  sell: tp <= bid → 立即成交
        #   short buy:  tp >= ask 或 tp <= bid 都应用 limit；此处用 last_bid 近似
        ord_type = "post_only"
        if self._last_bid > 0:
            crossed = (tp_price >= self._last_bid) if self._is_short else (tp_price <= self._last_bid)
            if crossed:
                ord_type = "limit"
                log.info(
                    "[grid] TP 价 %.2f 已穿越 bid %.2f（%s），用 limit 确保成交",
                    tp_price, self._last_bid, api_side,
                )
        try:
            resp = self._rest.request("POST", "/api/v5/trade/order", {
                "instId": self._inst_id,
                "tdMode": self._td_mode,
                "side": api_side,
                "ordType": ord_type,
                "sz": self._sz(contracts),
                "px": self._px(tp_price),
                "reduceOnly": True,
            })
            row   = (resp.get("data") or [{}])[0]
            oid   = str(row.get("ordId") or "")
            scode = str(row.get("sCode") or "0")
            if oid and scode == "0":
                log.info("[grid] TP 挂单 %s@%s x%s ordType=%s ordId=%s",
                         api_side, self._px(tp_price), self._sz(contracts), ord_type, oid)
                return oid
            log.warning("[grid] TP 下单失败 sCode=%s ordType=%s", scode, ord_type)
            return ""
        except Exception as e:
            log.warning("[grid] TP 下单异常: %s", e)
            return ""

    def _market_close_all(self, mid: float, reason: str) -> bool:
        """市价平仓所有持仓槽位（long=sell，short=buy），记录盈亏。返回 True=API成功，False=API失败。"""
        held = [s for s in self._slots if s.state == _S.HOLDING and s.fill_sz > 0]
        total = sum(s.fill_sz for s in held)
        if total <= 0:
            return True
        api_side = self._exit_api_side()
        _close_ok = True
        try:
            self._rest.request("POST", "/api/v5/trade/order", {
                "instId": self._inst_id,
                "tdMode": self._td_mode,
                "side": api_side,
                "ordType": "market",
                "sz": self._sz(total),
                "reduceOnly": True,
            })
            log.warning(
                "[grid] 市价平仓 %s %s张 @%.2f reason=%s",
                api_side, self._sz(total), mid, reason,
            )
        except Exception as e:
            log.error("[grid] 市价平仓失败: %s", e)
            return False  # API失败：slot状态保留（不清空），由 _emergency_close 计时重试

        sign = self._pnl_sign()
        session_net = 0.0  # 本次会话所有 slot 净盈亏之和
        for s in held:
            # PnL 方向乘子：long→+1，short→-1（short 的 mid<fill 才盈利）
            raw_pct = (mid - s.fill_price) / s.fill_price if s.fill_price > 0 else 0.0
            pnl_pct = raw_pct * sign
            net = (mid - s.fill_price) * s.fill_sz * self._ct_val * sign
            # 紧急平仓：入场 maker(2bps) + 市价 taker(5bps) = 7bps，比常规 4bps 高
            fee = self._notional(s.fill_sz, mid) * self._EMERGENCY_CLOSE_FEE_BPS / 10000.0
            net_after = net - fee
            self._pnl.add(net_after)
            session_net += net_after
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

        # 按会话（而非单 slot）追踪连亏：单次紧急平仓算 1 次事件，避免多 slot 共平一次却
        # 提前触发 loss_streak（原 bug：3 slot 同时亏 → slot2 append 后即触发冷静 30min）
        self._recent_close_pnls.append(session_net)
        if len(self._recent_close_pnls) >= 2 and all(p < 0 for p in list(self._recent_close_pnls)[-2:]):
            # round70: regime差异化冷静期（顺势600s/RANGING900s/逆势1200s）
            # 顺势亏损=临时回调，快速恢复可抓趋势；逆势亏损=趋势对抗，等更久避免重复止损
            _favorable_r = Regime.TRENDING_DOWN if self._is_short else Regime.TRENDING_UP
            if self._current_regime == _favorable_r:
                _ls_cd = 600.0    # 顺势：10min，短暂等待后快速恢复
            elif self._current_regime == Regime.RANGING:
                _ls_cd = 900.0    # 振荡：15min，标准冷静期（原值不变）
            else:
                _ls_cd = 1200.0   # 逆势：20min，趋势对抗需更长等待
            self._loss_streak_until = time.time() + _ls_cd
            self._save_loss_streak()
            log.warning(
                "[grid][loss-streak] 连续2次亏损 → 冷静%.0fmin regime=%s（至 %s）",
                _ls_cd / 60, self._current_regime.value,
                datetime.fromtimestamp(self._loss_streak_until).strftime('%H:%M:%S'),
            )
            try:
                from quant.detailed_daily_log import record_analysis
                record_analysis(
                    "loss_streak_triggered",
                    mid=mid,
                    regime=self._current_regime.value,
                    recent_pnls=list(self._recent_close_pnls),
                    session_net=round(session_net, 4),
                    cooldown_sec=_ls_cd,
                    cooldown_until=datetime.fromtimestamp(self._loss_streak_until).strftime('%H:%M:%S'),
                    daily_pnl_realized=round(self._pnl.realized, 4),
                )
            except Exception:
                pass
        return True

    # ══════════════════════════════════════════════════════════════════════════
    # 紧急平仓
    # ══════════════════════════════════════════════════════════════════════════

    def _emergency_close(self, reason: str, mid: float) -> None:
        if self._emergency_closing:
            return
        self._emergency_closing = True
        log.warning("[grid] ═══ 紧急平仓 reason=%s ═══", reason)
        try:
            from quant.detailed_daily_log import record_analysis
            record_analysis(
                "emergency_close",
                reason=reason,
                mid=mid,
                total_held=self._total_held,
                vwap=round(self._vwap, 2),
                unrealized_usdt=round(self._calc_unrealized(mid), 4),
                daily_pnl_realized=round(self._pnl.realized, 4),
            )
        except Exception:
            pass

        # 取消所有入场单；保存 oid 用于后续成交检查
        _cancelled_entry: list[tuple[GridSlot, str]] = []
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                _cancelled_entry.append((s, s.entry_order_id))
                self._cancel_order(s.entry_order_id)
                s.state = _S.EMPTY
                s.entry_order_id = ""

        # 取消后查询订单状态：防止入场单在撤单窗口内成交后成为孤儿仓位
        # （_cancel_order 对 51401=订单不存在返回 True，无法区分"已撤"与"已成交"）
        _now_ec = time.time()
        for _s, _oid in _cancelled_entry:
            _ord = self._query_order(_oid)
            _fill_sz = float(_ord.get("fillSz") or 0.0)
            if str(_ord.get("state", "")) in ("filled", "partially_canceled") and _fill_sz > 0:
                _fill_px = float(_ord.get("avgPx") or _ord.get("fillPx") or _s.target_price)
                _s.fill_price     = _fill_px
                _s.fill_sz        = _fill_sz
                _s.fill_ts        = _now_ec
                _s.state          = _S.HOLDING
                _s.entry_order_id = ""
                self._vwap_value += _fill_px * _fill_sz
                self._total_held += _fill_sz
                self._vwap = self._vwap_value / self._total_held
                log.warning(
                    "[grid] 紧急平仓: L%d 入场单已成交 @%.2f sz=%.1f — 追加HOLDING待市价平仓",
                    _s.level, _fill_px, _fill_sz,
                )

        # 取消 TP 单
        if self._tp_order_id:
            self._cancel_order(self._tp_order_id)
            self._tp_order_id = ""

        # 市价平仓
        _close_ok = self._market_close_all(mid, reason)
        if not _close_ok:
            # API中断：slot状态未清（持仓仍在），启动circuit_break计时，60s后tick循环重试
            self._emergency_close_failed_ts = time.time()
            self._emergency_closing = False
            log.error("[grid] _emergency_close: 市价平仓API失败，启动circuit_break计时（60s后重试）")
            return
        self._emergency_close_failed_ts = 0.0
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
        # Cancel pending entry orders; query each to catch fills during cancel window.
        # _cancel_order returns True for sCode=51401 (order not found = may be filled),
        # so we cannot rely on False-return alone to detect orphans.
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                self._cancel_order(s.entry_order_id)
                order = self._query_order(s.entry_order_id)
                fill_sz = float(order.get("fillSz") or 0.0)
                if str(order.get("state", "")) in ("filled", "partially_canceled") and fill_sz > 0:
                    fill_px = float(order.get("avgPx") or order.get("fillPx") or s.target_price)
                    log.warning(
                        "[grid] _reset_grid: L%d 孤儿仓 sz=%.1f @%.2f — 尝试市价平仓",
                        s.level, fill_sz, fill_px,
                    )
                    try:
                        self._rest.request("POST", "/api/v5/trade/order", {
                            "instId": self._inst_id,
                            "tdMode": self._td_mode,
                            "side": self._exit_api_side(),
                            "ordType": "market",
                            "sz": self._sz(fill_sz),
                            "reduceOnly": True,
                        })
                        try:
                            from quant.detailed_daily_log import record_analysis
                            record_analysis(
                                "orphan_close",
                                level=s.level,
                                fill_sz=fill_sz,
                                fill_px=fill_px,
                                daily_pnl_realized=round(self._pnl.realized, 4),
                            )
                        except Exception:
                            pass
                    except Exception as _e:
                        log.error("[grid] _reset_grid: 孤儿仓市价平仓失败: %s", _e)
        for s in self._slots:
            s.state = _S.EMPTY
            s.entry_order_id = ""
            s.fill_price = 0.0
            s.fill_sz    = 0.0
            s.fail_count = 0
            s.retry_after_ts = 0.0
        # Cancel TP if still live before clearing state.
        # Fixes ghost-position reset path (line ~2300) where caller skips TP cancel.
        # _cancel_order treats 51401 (already filled/cancelled) as success → safe for all paths.
        if self._tp_order_id:
            self._cancel_order(self._tp_order_id)
        self._tp_order_id      = ""
        self._tp_price         = 0.0
        self._tp_placed_ts     = 0.0
        self._tp_exposed_since = 0.0
        self._sync_pending_ts  = 0.0
        self._emergency_close_failed_ts = 0.0
        self._total_held       = 0.0
        self._vwap_value   = 0.0
        self._vwap         = 0.0
        self._grid_active  = False
        self._grid_center  = 0.0

    def _place_grid(self, center: float, regime: Regime, now: float) -> None:
        """
        以 center 为基准放置网格：
        - long 做多：
            * TRENDING_UP：向上偏置，买单更接近当前价（等回调，不追跌）
            * RANGING：标准向下展开
        - short 做空：镜像
            * TRENDING_DOWN：向下偏置，卖单更接近当前价（等反弹，不追涨）
            * RANGING：标准向上展开
        """
        n_active = self._vol.active_levels(self._max_levels)
        if n_active == 0:
            log.info("[grid] 波动率状态=%s，跳过开格", self._vol.vol_regime)
            return

        spacing = self._vol.spacing_pct(self._atr_mult, self._min_sp, self._max_sp)

        # ATR 基线慢速 EMA 更新（α=0.05；纯 ATR 格宽，FGI/趋势调整前）
        if self._atr_baseline <= 0.0:
            self._atr_baseline = spacing
        else:
            self._atr_baseline = 0.05 * spacing + 0.95 * self._atr_baseline
        self._save_atr_baseline()  # 节流持久化，重启后立即恢复

        # 资金费率逆风检测：
        #   long:  funding < -0.0003（空头溢价）→ 不利
        #   short: funding > +0.0003（多头溢价）→ 不利
        fr_adverse = (
            self._funding_rate > 0.0003 if self._is_short
            else self._funding_rate < -0.0003
        )
        if fr_adverse and n_active > 1:
            n_active -= 1
            log.info(
                "[grid] %s资金费率 %.5f，激活档位减1 → %d",
                "正" if self._is_short else "负", self._funding_rate, n_active,
            )

        # FGI 极端情绪减档（降低逆风方向敞口）：
        #   long:  FGI < 25（极度恐慌）不利
        #   short: FGI > 75（极度贪婪）不利
        fgi_adverse = (
            self._fear_greed_index > 75 if self._is_short
            else self._fear_greed_index < 25
        )
        if fgi_adverse and n_active > 1:
            n_active -= 1
            log.info(
                "[grid] 极端情绪 FGI=%d（逆%s），激活档位减1 → %d",
                self._fear_greed_index, "空" if self._is_short else "多", n_active,
            )

        # FGI 格宽调整（在档位调整基础上叠加）：
        #   极度恐慌 FGI < 25 → 收窄 20%（市场震动剧烈，小格更易成交）
        #   贪婪 FGI > 70 + RANGING → 扩宽 20%（情绪亢奋但震荡，每格利润更高）
        if self._fear_greed_index < 25:
            spacing = max(spacing * 0.80, self._min_sp)
            log.info("[grid] 极恐FGI=%d，格宽收窄×0.8→%.5f", self._fear_greed_index, spacing)
        elif self._fear_greed_index > 70 and regime == Regime.RANGING:
            spacing = min(spacing * 1.20, self._max_sp)
            log.info("[grid] 贪婪FGI=%d RANGING，格宽扩宽×1.2→%.5f", self._fear_greed_index, spacing)

        # US session (UTC 13-23 / CST 21-07): cap levels to 2 (原 1，L10-001 放宽)
        # 历史数据：50-fill 窗口 3/3 亏损在 US session，但样本太小且那时纯多头。
        # L2 做空上线后 US session 可能反而有利（美股下跌带 ETH 下跌 → 做空顺势）。
        # 放宽到 2 档保留一定保守性同时避免"全时段只 1 档"资金闲置问题。
        _utc_h = time.gmtime().tm_hour
        if 13 <= _utc_h <= 23 and n_active > 2:
            n_active = 2
            log.info("[grid] US session (UTC %02d), levels capped→2", _utc_h)

        # 顺势偏置：
        #   long  → TRENDING_UP   时 bias=0.5（更靠近当前价）+ spacing*1.3（TP更远）
        #   short → TRENDING_DOWN 时 bias=0.5 + spacing*1.3
        favorable_trend = (
            Regime.TRENDING_DOWN if self._is_short else Regime.TRENDING_UP
        )
        if regime == favorable_trend:
            bias = 0.5
            spacing = min(spacing * 1.3, self._max_sp)
            # 顺势+极端情绪顺风时多激活1档（行情好多赚）：
            #   long:  FGI>60 + TRENDING_UP   → 顺势贪婪
            #   short: FGI<40 + TRENDING_DOWN → 顺势恐慌
            fgi_favorable = (
                self._fear_greed_index < 40 if self._is_short
                else self._fear_greed_index > 60
            )
            if fgi_favorable and n_active < self._max_levels:
                n_active += 1
                log.info(
                    "[grid] 顺势+情绪FGI=%d，激活档位加1 → %d",
                    self._fear_greed_index, n_active,
                )
        else:
            bias = 1.0

        self._grid_spacing  = spacing
        self._grid_center   = center
        self._grid_bias     = bias
        self._active_levels = n_active
        self._grid_active   = True
        placed = 0
        # 记录开格时间给节流 gate 用（改进 2）
        self._recent_entries_ts.append(time.time())

        # ── 改进 3 (2026-04-22): 规模自适应波动 ──
        # 问题：sz=1.0 notional $240，per_slot_stop $0.8 = 0.33% 容忍
        #      但 ETH ATR 30bps + 冲高回落 50bps 常见 → 每次都击穿
        # 规则：ATR > 28bps 缩 sz；ATR 越高缩越多
        # round69: 补充 28-35bps 中间档 sz=0.85（原来该区间无缩减，直接跳到 1.0→0.7）
        # ETH ATR 28-35bps 是高频出现的"轻微偏高"状态，温和缩仓 15% 降低击穿概率。
        _atr_bps = self._vol.atr_short * 10000
        _sz_scale = 1.0
        if _atr_bps > 70:
            _sz_scale = 0.3
        elif _atr_bps > 50:
            _sz_scale = 0.5
        elif _atr_bps > 35:
            _sz_scale = 0.7
        elif _atr_bps > 28:
            _sz_scale = 0.85
        if _sz_scale < 1.0:
            # round70: 补充档位标签，便于统计各ATR区间触发频率
            if _atr_bps > 70:
                _sz_tier = ">70bps"
            elif _atr_bps > 50:
                _sz_tier = "50-70bps"
            elif _atr_bps > 35:
                _sz_tier = "35-50bps"
            else:
                _sz_tier = "28-35bps"
            log.info(
                "[grid][atr-scale] ATR=%.1fbps[%s] 仓位缩 ×%.2f（防高波动击穿止损）",
                _atr_bps, _sz_tier, _sz_scale,
            )
        self._last_sz_scale = _sz_scale  # 供 status_summary / grid_state 快照使用
        # 动态调整 slots 的 contracts（仅对 EMPTY 的 slot 生效，不动 HOLDING）
        _effective_contracts = self._contracts_per_slot * _sz_scale
        for s in self._slots:
            if s.state == _S.EMPTY:
                s.contracts = _effective_contracts

        # 入场方向乘子：long 在下方(-1)，short 在上方(+1)
        dir_sign = self._grid_spacing_sign()
        for i, s in enumerate(self._slots):
            if i >= n_active:
                break
            if s.state != _S.EMPTY:
                continue
            s.target_price    = center * (1.0 + dir_sign * spacing * (i + 1) * bias)
            s.last_attempt_ts = now
            if self._place_entry(s, now):
                placed += 1

        log.info(
            "[grid] 网格启动 direction=%s regime=%s center=%.2f spacing=%.4f%% "
            "levels=%d/%d placed=%d vol=%s",
            self._direction, regime.value, center, spacing * 100,
            placed, n_active, placed, self._vol.vol_regime,
        )
        try:
            from quant.detailed_daily_log import record_analysis
            record_analysis(
                "grid_opened",
                direction=self._direction,
                regime=regime.value,
                center=round(center, 2),
                spacing_bps=round(spacing * 10000, 2),
                n_active=n_active,
                bias=round(bias, 3),
                atr_bps=round(_atr_bps, 2),
                sz_scale=round(_sz_scale, 2),
                funding_rate=round(self._funding_rate, 6),
                fgi=self._fear_greed_index,
                daily_pnl_realized=round(self._pnl.realized, 4),
                placed=placed,
            )
        except Exception:
            pass

    def _update_tp(self) -> None:
        """取消旧 TP，以当前 VWAP + 格宽重新挂单。
        long:  TP 在 VWAP 上方（+spacing*mult）
        short: TP 在 VWAP 下方（-spacing*mult）
        RANGING 模式下 tp_mult × 0.8，缩短 TP 距离提升成交率。
        """
        if self._total_held <= 0:
            return
        if self._tp_order_id:
            self._cancel_order(self._tp_order_id)
            self._tp_order_id = ""
        tp_sign = self._tp_spacing_sign()
        # RANGING 模式：TP 距离缩短为 0.8×，提升成交率；趋势模式保留完整 spacing
        _eff_tp_mult = self._tp_mult * (0.8 if self._current_regime == Regime.RANGING else 1.0)
        # ATR 联动：当前格宽高于基线→延伸 TP（波动大，价格走得远）；低于基线→收紧 TP（波动小，贴近成交）
        if self._atr_baseline > 0.0 and self._grid_spacing > 0.0:
            _atr_ratio = max(0.85, min(1.3, self._grid_spacing / self._atr_baseline))
            _eff_tp_mult = max(0.4, min(2.0, _eff_tp_mult * _atr_ratio))
            log.debug(
                "[grid] ATR联动 TP: spacing=%.5f baseline=%.5f ratio=%.3f eff_mult=%.3f",
                self._grid_spacing, self._atr_baseline, _atr_ratio, _eff_tp_mult,
            )
        self._last_eff_tp_mult = _eff_tp_mult  # 缓存供 fill_tp 事件诊断
        tp = self._vwap * (1.0 + tp_sign * self._grid_spacing * _eff_tp_mult)
        self._tp_price = tp
        oid = self._place_tp(self._total_held, tp)
        if not oid:
            # 首次失败：等 0.5s 后立即重试一次，降低单次网络抖动导致裸仓概率
            time.sleep(0.5)
            oid = self._place_tp(self._total_held, tp)
        if oid:
            self._tp_order_id      = oid
            self._tp_placed_ts     = time.time()
            self._tp_exposed_since = 0.0  # 挂单成功，清除裸仓计时器
        else:
            if self._tp_exposed_since == 0.0:
                self._tp_exposed_since = time.time()
            log.error(
                "[grid] _update_tp: TP挂单两次均失败，持仓%.1f合约暂无止盈保护，"
                "裸仓%.0fs（held=%.1f vwap=%.2f tp=%.2f）",
                self._total_held, time.time() - self._tp_exposed_since,
                self._total_held, self._vwap, tp,
            )

    def _maybe_trail_tp(self, mid: float) -> None:
        """
        TP 追踪：
          long  → 市场上行 mid > tp + _trail_trigger*spacing，TP 上移到 mid - trail_offset*spacing
          short → 市场下行 mid < tp - _trail_trigger*spacing，TP 下移到 mid + trail_offset*spacing

        RANGING 模式（震荡行情，自适应调整后实际值可能偏大）：
          base trail_offset  = 0.50（adaptive范围 [0.35, 0.65]）
          base _trail_trigger = 1.05（adaptive范围 [0.85, 1.25]）
          _min_trail_iv  = 20s（节流更短，允许更频繁追踪）

        趋势模式（价格持续延伸，需要更大缓冲）：
          base trail_offset  = 0.60（adaptive范围 [0.45, 0.75]）
          base _trail_trigger = 1.20（adaptive范围 [1.00, 1.50]）
          _min_trail_iv  = 30s（_TP_TRAIL_MIN_INTERVAL，避免频繁 API 调用）

        trigger/offset 经 _adaptive_trail_trigger / _adaptive_trail_offset 按 EWMA 利润动态调整。
        成功追踪后重置 _tp_placed_ts，给TP新的超时窗口（顺势行情中不应过早止损）。
        """
        if not self._tp_order_id or self._tp_price <= 0:
            return
        now = time.time()
        _is_ranging = self._current_regime == Regime.RANGING
        # RANGING 模式：节流 20s（更频繁）；趋势模式：节流 30s（_TP_TRAIL_MIN_INTERVAL）
        _min_trail_iv = 20.0 if _is_ranging else self._TP_TRAIL_MIN_INTERVAL
        if now - self._last_tp_trail_ts < _min_trail_iv:
            return  # 节流：避免每个 tick 都 cancel/replace TP 单
        spacing_abs = self._grid_spacing * self._vwap
        if spacing_abs <= 0:
            # _grid_spacing cleared by _reset_grid_state; trail with spacing=0 would move TP to
            # current price (new_tp = mid ± 0), causing immediate execution at break-even.
            # Skip until _grid_spacing is restored by the next _update_tp call.
            return
        # 2026-04-22 18:00 主人方案 A：放严 trail trigger 防偷盈利
        #
        # 问题分析：原 trigger 0.30 / 0.40 意思是"价格超出 TP 0.3-0.4 格宽就把
        #           TP 拉近到 mid - 0.15×spacing"（几乎贴市价）。
        #           结果：原 60bps TP 被拉到 ~5-10bps 才成交 → 偷 80% 盈利。
        #
        # 数学：avg_win 期望 $1.31 但实际 $0.21，差 $1.10 就是被 trail 偷的。
        #
        # 修复方向 A：放严 trigger 到 1.0 / 1.2（需要价格超出 TP 一整个格宽才启动）
        #   效果：大多数 TP 在原位自然成交 → 拿全额 60bps
        #   保留：极端延伸时（>1 格宽）仍可锁利，不丢过大盈利
        # 修复方向 B：加大 offset 到 0.50 / 0.60（真 trail 时留更多空间）
        _trail_offset  = self._adaptive_trail_offset(
            self._RANGING_TRAIL_BASE_OFFSET if _is_ranging else self._TRENDING_TRAIL_BASE_OFFSET,
            _is_ranging,
        )
        _trail_trigger = self._adaptive_trail_trigger(
            self._RANGING_TRAIL_BASE_TRIGGER if _is_ranging else self._TRENDING_TRAIL_BASE_TRIGGER,
            _is_ranging,
        )

        if self._is_short:
            # short：市场继续下跌时向下追踪 TP，锁住更多空头利润
            if mid < self._tp_price - spacing_abs * _trail_trigger:
                new_tp = mid + spacing_abs * _trail_offset
                log.info(
                    "[grid] TP 追踪下调（short）：mid=%.2f < tp=%.2f - %.2f格，新TP=%.2f [offset=%.2f iv=%.0fs]",
                    mid, self._tp_price, _trail_trigger, new_tp, _trail_offset, _min_trail_iv,
                )
                if new_tp < self._tp_price:
                    self._cancel_order(self._tp_order_id)
                    self._tp_order_id = ""
                    self._tp_price = new_tp
                    oid = self._place_tp(self._total_held, new_tp)
                    if oid:
                        self._tp_order_id      = oid
                        self._tp_placed_ts     = now
                        self._tp_exposed_since = 0.0
                    else:
                        if self._tp_exposed_since == 0.0:
                            self._tp_exposed_since = now
                        log.warning(
                            "[grid] trail_tp(short): TP补挂失败，下一tick自动恢复"
                            "（held=%.1f tp=%.2f）", self._total_held, new_tp,
                        )
                self._last_tp_trail_ts = now  # 无论成功与否都更新节流时间戳（与long一致）
        else:
            # long：市场继续上涨时向上追踪 TP
            if mid > self._tp_price + spacing_abs * _trail_trigger:
                new_tp = mid - spacing_abs * _trail_offset
                log.info(
                    "[grid] TP 追踪上调：mid=%.2f > tp=%.2f + %.2f格，新TP=%.2f [offset=%.2f iv=%.0fs]",
                    mid, self._tp_price, _trail_trigger, new_tp, _trail_offset, _min_trail_iv,
                )
                if new_tp > self._tp_price:
                    self._cancel_order(self._tp_order_id)
                    self._tp_order_id = ""
                    self._tp_price = new_tp
                    oid = self._place_tp(self._total_held, new_tp)
                    if oid:
                        self._tp_order_id      = oid
                        self._tp_placed_ts     = now   # 重置超时计时器：市场上行时不应过早触发止损
                        self._tp_exposed_since = 0.0
                    else:
                        if self._tp_exposed_since == 0.0:
                            self._tp_exposed_since = now
                        log.warning(
                            "[grid] trail_tp(long): TP补挂失败，下一tick自动恢复"
                            "（held=%.1f tp=%.2f）", self._total_held, new_tp,
                        )
                self._last_tp_trail_ts = now   # 触发条件成立即更新节流（与short路径对齐）

    # 有效权重最低门槛：等效于至少 0.5 个"新鲜"样本（防止桶数据全部超过 4 个半衰期后
    # 权重趋近于 0，EWMA 退化为等权均值并误触发自适应逻辑）
    # RANGING 900s：4个半衰期 = 3600s（1小时）→ 超 1h 无成交时自动退回 base
    # TRENDING 2700s：4个半衰期 = 10800s（3小时）→ 超 3h 无成交时自动退回 base
    _EWMA_MIN_TOTAL_W = 0.5

    # EWMA 激活所需最小样本数（regime-specific）
    # RANGING 成交频繁（1h 内可积累 10+ 笔），保持 5 抑制噪声
    # TRENDING 成交稀疏（1h 内仅 1-3 笔），降至 3 确保自适应可激活
    _EWMA_MIN_SAMPLES_RANGING  = 5
    _EWMA_MIN_SAMPLES_TRENDING = 3

    def _ewma_profit_avg(self) -> float | None:
        """时间衰减加权 EWMA：近期 TP 利润格宽倍数，半衰期按 Regime 动态切换。

        权重 w_i = exp(-λ·(t_now - t_i))，λ = ln2 / half_life。
        按当前 Regime 使用对应 bucket（RANGING/TRENDING 分开维护，互不干扰）。
        RANGING  → 900s（15min）：震荡市节奏快，需快速响应利润变化
        TRENDING → 2700s（45min）：趋势市成交稀疏，平滑避免过拟合极少样本
        样本不足时返回 None（RANGING < 5，TRENDING < 3）。
        total_w < 0.5 时返回 None（桶数据均超 4 个半衰期，退回 base 参数）。
        """
        bucket = self._tp_current_bucket
        min_samples = (
            self._EWMA_MIN_SAMPLES_RANGING
            if self._current_regime == Regime.RANGING
            else self._EWMA_MIN_SAMPLES_TRENDING
        )
        if len(bucket) < min_samples:
            return None
        half_life = 900.0 if self._current_regime == Regime.RANGING else 2700.0
        lam = math.log(2.0) / half_life
        now = time.time()
        total_w = 0.0
        total_wv = 0.0
        for ts, v in bucket:
            w = math.exp(-lam * max(0.0, now - ts))
            total_w += w
            total_wv += w * v
        if total_w < self._EWMA_MIN_TOTAL_W:
            return None
        return total_wv / total_w

    @property
    def _tp_current_bucket(self) -> "deque[tuple[float, float]]":
        return (
            self._tp_profits_ranging
            if self._current_regime == Regime.RANGING
            else self._tp_profits_trending
        )

    def _replay_tp_history(self) -> None:
        """重启后从 analysis.jsonl 重播最近 TP 成交，使 EWMA 自适应立即可用。

        读取今日与昨日的 analysis.jsonl，筛选含 profit_spacings 的 fill_tp 事件，
        按时间戳排序后按 Regime 路由到 _tp_profits_ranging / _tp_profits_trending。
        日志中含 regime 字段时精确分流；旧格式无此字段时默认归入 RANGING bucket。
        只有 profit_spacings 字段存在时才纳入（旧日志无此字段时静默跳过）。
        """
        import json as _json
        from datetime import date as _date, datetime as _dt, timedelta as _td
        from pathlib import Path as _Path

        data_dir = _Path(self._data_dir)
        today = _date.today()
        records: list[tuple[float, float, str]] = []  # (ts, profit_spacings, regime_str)
        for d in (today, today - _td(days=1)):
            path = data_dir / "logs" / "daily" / d.isoformat() / "analysis.jsonl"
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = _json.loads(line)
                        except Exception:
                            continue
                        if rec.get("event") != "fill_tp":
                            continue
                        ps = rec.get("profit_spacings")
                        ts_wall = rec.get("ts_wall", "")
                        if ps is None or not ts_wall:
                            continue
                        try:
                            ts = _dt.fromisoformat(ts_wall).timestamp()
                        except Exception:
                            continue
                        regime_str = rec.get("regime", "RANGING")
                        records.append((float(ts), min(float(ps), 3.0), str(regime_str)))
            except Exception:
                pass

        if not records:
            return
        records.sort(key=lambda x: x[0])
        # 取最近 40 条（每个 bucket 各 maxlen=20，合计上限）
        for ts, ps, regime_str in records[-40:]:
            if regime_str in ("TRENDING_UP", "TRENDING_DOWN"):
                self._tp_profits_trending.append((ts, ps))
            else:
                self._tp_profits_ranging.append((ts, ps))
        n_r = len(self._tp_profits_ranging)
        n_t = len(self._tp_profits_trending)
        log.info(
            "[grid] 冷启动恢复 TP 历史: 找到 %d 条，ranging=%d trending=%d，EWMA ranging%s trending%s",
            len(records), n_r, n_t,
            " 即时可用" if n_r >= 5 else " 待更多成交",
            " 即时可用" if n_t >= 5 else " 待更多成交",
        )

    def _save_atr_baseline(self) -> None:
        """节流保存 _atr_baseline 到 data/grid_atr_state.json（最多每5分钟一次）。"""
        import json as _json
        now = time.time()
        if now - self._last_atr_save_ts < 300.0:
            return
        self._last_atr_save_ts = now
        try:
            from pathlib import Path as _Path
            state_path = _Path(self._data_dir) / "grid_atr_state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with state_path.open("w") as f:
                _json.dump({"atr_baseline": self._atr_baseline, "saved_ts": now}, f)
        except Exception as e:
            log.warning("[grid] 保存 atr_baseline 失败，忽略: %s", e)

    def _restore_atr_baseline(self) -> None:
        """启动时从 data/grid_atr_state.json 恢复 _atr_baseline（文件超过12h则忽略）。"""
        import json as _json
        try:
            from pathlib import Path as _Path
            state_path = _Path(self._data_dir) / "grid_atr_state.json"
            if not state_path.exists():
                return
            with state_path.open() as f:
                data = _json.load(f)
            age_h = (time.time() - data.get("saved_ts", 0)) / 3600.0
            if age_h > 12.0:
                log.info("[grid] grid_atr_state.json 已过期 (%.1fh)，不恢复 atr_baseline", age_h)
                return
            val = float(data.get("atr_baseline", 0.0))
            if val > 0.0:
                self._atr_baseline = val
                log.info("[grid] 恢复 atr_baseline=%.6f（%.1fh前保存），ATR联动立即可用", val, age_h)
        except Exception as e:
            log.warning("[grid] 恢复 atr_baseline 失败，忽略: %s", e)

    def _save_loss_streak(self) -> None:
        """重启后保留 loss_streak 冷静期 —— 防止崩溃重启立即清零冷静期重开仓。"""
        import json as _json
        try:
            from pathlib import Path as _Path
            p = _Path(self._data_dir) / "grid_loss_streak.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("w") as f:
                _json.dump({"until": self._loss_streak_until, "saved_ts": time.time()}, f)
        except Exception as e:
            log.warning("[grid] 保存 loss_streak_until 失败，忽略: %s", e)

    def _restore_loss_streak(self) -> None:
        """启动时恢复 loss_streak_until（仍未到期则继续冷静期，已过期则忽略）。"""
        import json as _json
        try:
            from pathlib import Path as _Path
            p = _Path(self._data_dir) / "grid_loss_streak.json"
            if not p.exists():
                return
            with p.open() as f:
                data = _json.load(f)
            until = float(data.get("until", 0.0))
            if until > time.time():
                self._loss_streak_until = until
                remain_min = (until - time.time()) / 60
                log.warning("[grid] 恢复 loss_streak 冷静期：剩余 %.1f min（跨重启保护）", remain_min)
            else:
                p.unlink(missing_ok=True)
        except Exception as e:
            log.warning("[grid] 恢复 loss_streak_until 失败，忽略: %s", e)

    def _adaptive_trail_trigger(self, base_trigger: float, is_ranging: bool) -> float:
        """根据近期TP成交利润（格宽倍数，EWMA加权）动态调整追踪触发门槛。

        metric: profit_spacings = abs(fill_px - vwap) / spacing
          < 0.25格均值 → 利润极低（trail过早触发偷盈利）→ 放宽 trigger +0.20（两级自适应，round38）
          < 0.40格均值 → 利润偏低 → 放宽 trigger +0.10
          > 1.00格均值 → 利润丰厚（市场延伸充分）→ 更激进收紧 trigger -0.10（round43）
          > 0.80格均值 → 利润充足 → 收紧 trigger -0.05
          中间范围 → 保持 base_trigger，不干预

        三级自适应（round43）：avg>1.00 层比 avg>0.80 更激进（-0.10 vs -0.05），
        利润丰厚时 trail 更早启动，锁住更大延伸行情。
        两级低利润层（round38）：avg<0.25 比 avg<0.40 更激进（+0.20 vs +0.10），
        防止极低利润场景下 trail 连续拉低 TP 价格。

        至少需要 5 次成交数据才启用自适应，否则直接返回 base。
        边界按 Regime 独立：
          RANGING  : [0.85, 1.25]（base 1.05；avg<0.25→1.25=hi cap；avg>1.00→0.95）
          TRENDING : [1.05, 1.50]（base 1.20；lo 与 RANGING base 对齐，确保趋势trail不比震荡基线更激进）
        round46: TRENDING lo 1.00→1.05，设计不变式：TRENDING trail trigger 永不低于 RANGING base trigger。
        """
        avg = self._ewma_profit_avg()
        if avg is None:
            return base_trigger
        lo, hi = (0.85, 1.25) if is_ranging else (1.05, 1.50)
        if avg < 0.25:
            adapted = min(base_trigger + 0.20, hi)
        elif avg < 0.4:
            adapted = min(base_trigger + 0.10, hi)
        elif avg > 1.0:
            adapted = max(base_trigger - 0.10, lo)
        elif avg > 0.8:
            adapted = max(base_trigger - 0.05, lo)
        else:
            adapted = base_trigger
        if adapted != base_trigger:
            log.debug(
                "[grid] adaptive trigger: base=%.2f → %.2f (avg_profit=%.3f格, n=%d, regime=%s, bounds=[%.2f,%.2f])",
                base_trigger, adapted, avg, len(self._tp_current_bucket), self._current_regime.value, lo, hi,
            )
        return adapted

    def _adaptive_trail_offset(self, base_offset: float, is_ranging: bool) -> float:
        """根据近期TP成交利润（格宽倍数，EWMA加权）动态调整追踪步长（trail_offset）。

        metric: profit_spacings = abs(fill_px - vwap) / spacing
          < 0.25格均值 → 利润极低（trail 触发后 TP 应落在更远处）→ 放宽 +0.06（两级，round39）
          < 0.40格均值 → 利润偏低（offset 太紧，TP 离市价太近）→ 放宽 +0.03（round49: 0.35→0.40，对齐trigger第二层）
          > 1.00格均值 → 利润丰厚，与 trigger 收紧配套 → 更激进收紧 -0.05（round44）
          > 0.80格均值 → 利润充足但延迟锁定 → 收紧 -0.03
          中间范围 → 保持 base_offset，不干预

        两级自适应（round39）：与 _adaptive_trail_trigger 的 avg<0.25 极低层对称。
        当 trigger 因极低利润放宽至 1.20（需价格超出 TP 1.2 格才启动 trail），
        offset 也同步放宽 +0.06，确保 trail 触发后 TP 落点够远、不被立即回撤夹击。
        三级对称扩展（round44）：与 trigger 的 avg>1.00 层配套，
        利润丰厚时 trail TP 落点也更紧（-0.05），使锁利更迅速。
        round49：第二低利润层阈值 0.35→0.40，与 trigger 第二层（avg<0.40）完全对齐，
        消除 [0.35, 0.40) 区间 trigger 放宽 +0.10 但 offset 不响应的剩余不对称缺口。
        现在 offset 在 [0.25, 0.40) 统一放宽 +0.03，trigger/offset 第二层完全联动。

        至少需要 5 次成交数据才启用自适应，否则直接返回 base。
        边界按 Regime 独立：
          RANGING  : [0.35, 0.65]（base 0.50；+0.06 → 0.56，仍在上界内）
          TRENDING : [0.50, 0.75]（base 0.60；lo 与 RANGING base 对齐，确保趋势trail不比震荡基线更激进）
        round46: TRENDING lo 0.45→0.50，设计不变式：TRENDING trail offset 永不低于 RANGING base offset。
        """
        avg = self._ewma_profit_avg()
        if avg is None:
            return base_offset
        lo, hi = (0.35, 0.65) if is_ranging else (0.50, 0.75)
        if avg < 0.25:
            adapted = min(base_offset + 0.06, hi)
        elif avg < 0.40:
            adapted = min(base_offset + 0.03, hi)
        elif avg > 1.0:
            adapted = max(base_offset - 0.05, lo)
        elif avg > 0.80:
            adapted = max(base_offset - 0.03, lo)
        else:
            adapted = base_offset
        if adapted != base_offset:
            log.debug(
                "[grid] adaptive offset: base=%.2f → %.2f (avg_profit=%.3f格, n=%d, regime=%s, bounds=[%.2f,%.2f])",
                base_offset, adapted, avg, len(self._tp_current_bucket), self._current_regime.value, lo, hi,
            )
        return adapted

    def _reset_grid_state(self, reason: str, now: float, cooldown: float = 10.0) -> None:
        """统一网格重置入口：撤销所有入场挂单，清空网格状态，设冷静期。"""
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                if not self._cancel_order(s.entry_order_id):
                    # Cancel failed — check if already filled to avoid orphan
                    order = self._query_order(s.entry_order_id)
                    o_state = str(order.get("state", ""))
                    fill_sz = float(order.get("fillSz") or 0.0)
                    if o_state == "filled" and fill_sz > 0:
                        fill_px = float(order.get("avgPx") or order.get("fillPx") or s.target_price)
                        log.warning(
                            "[grid] _reset_grid_state: L%d 入场单已成交 @%.2f sz=%.1f — 标记HOLDING",
                            s.level, fill_px, fill_sz,
                        )
                        s.fill_price = fill_px
                        s.fill_sz = fill_sz
                        s.fill_ts = now
                        s.state = _S.HOLDING
                        s.entry_order_id = ""
                        self._vwap_value += fill_px * fill_sz
                        self._total_held += fill_sz
                        self._vwap = self._vwap_value / self._total_held if self._total_held > 0 else 0.0
                        continue
                s.state = _S.EMPTY
                s.entry_order_id = ""
        self._grid_active = False
        self._grid_center = 0.0
        self._grid_spacing = 0.0
        self._grid_bias    = 1.0   # 重置为 RANGING 默认值，防止 TRENDING bias 残留
        self._cooldown_until = max(getattr(self, "_cooldown_until", 0.0), now + cooldown)
        log.info("[grid] 网格重置: %s | 冷静期 %.0fs", reason, cooldown)

    def _maybe_recenter(self, mid: float, now: float) -> None:
        """
        价格偏离检测：两种触发条件：
        1. 距离触发：偏离 > recenter_mult × spacing（缩短至 1.5 倍）
        2. 穿叉触发：任意 EMPTY 槽位的计算目标价穿越盘口
           long  → 买单目标价 >= bid 时失效
           short → 卖单目标价 <= bid 时失效（近似；严格应与 ask 比较）
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

        # ── 条件2：入场单价格穿越盘口（post_only 会必败）──────────
        if self._last_bid > 0:
            dir_sign = self._grid_spacing_sign()
            for s in self._slots:
                if s.state == _S.EMPTY:
                    calc_px = self._grid_center * (1.0 + dir_sign * self._grid_spacing * (s.level + 1) * self._grid_bias)
                    crossed = (
                        calc_px <= self._last_bid if self._is_short
                        else calc_px >= self._last_bid
                    )
                    if crossed:
                        op = "<=" if self._is_short else ">="
                        self._reset_grid_state(
                            f"L{s.level}目标价 {calc_px:.2f} {op} bid {self._last_bid:.2f}",
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
                    regime=self._current_regime.value,
                    daily_pnl_realized=round(self._pnl.realized, 4),
                    grid_spacing_bps=round(self._grid_spacing * 10000, 2),
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
            sign = self._pnl_sign()
            for s in self._slots:
                if s.state == _S.HOLDING and s.fill_sz > 0:
                    # PnL 方向乘子：long→+1，short→-1（short 的 fill_px<fill_price 才盈利）
                    raw_pct = (fill_px - s.fill_price) / s.fill_price
                    pnl_pct = raw_pct * sign
                    net = (fill_px - s.fill_price) * s.fill_sz * self._ct_val * sign
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
            # 按会话粒度追踪连亏，与 _market_close_all 保持一致（单次 TP 命中多 slot 算 1 次事件）
            self._recent_close_pnls.append(total_net)
            # 记录本次 TP 利润（格宽倍数 + 时间戳），按 Regime 分桶，供 EWMA 使用
            _ps: float | None = None
            if self._grid_spacing > 0 and self._vwap > 0:
                spacing_abs = self._grid_spacing * self._vwap
                _ps = min(abs(fill_px - self._vwap) / spacing_abs, 3.0)
                self._tp_current_bucket.append((time.time(), _ps))
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
                    profit_spacings=_ps,
                    regime=self._current_regime.value,
                    grid_spacing_bps=round(self._grid_spacing * 10000, 2),
                    atr_baseline_bps=round(self._atr_baseline * 10000, 2),
                    eff_tp_mult=round(self._last_eff_tp_mult, 3),
                )
            except Exception:
                pass
            self._reset_grid()

        elif state in ("canceled", "partially_canceled"):
            # TP 部分成交后被撤：记录已成交部分收益 + 按比例缩减 slot fill_sz + 重挂剩余 TP
            if fill_sz > 0 and self._total_held > 0:
                # 按比例计算已成交份额对应的收益（round75 bug fix: 旧代码缺失此步导致 PnL 漏记
                # 且后续完整 TP 成交时会用原始 fill_sz 重算，造成 PnL 双算）
                fill_ratio = fill_sz / self._total_held
                partial_net = 0.0
                sign = self._pnl_sign()
                for s in self._slots:
                    if s.state == _S.HOLDING and s.fill_sz > 0:
                        slot_filled = s.fill_sz * fill_ratio
                        net = (fill_px - s.fill_price) * slot_filled * self._ct_val * sign
                        fee = self._roundtrip_fee(slot_filled, fill_px)
                        net_after = net - fee
                        partial_net += net_after
                        self._pnl.add(net_after)
                        s.fill_sz = max(0.0, s.fill_sz - slot_filled)
                self._recent_close_pnls.append(partial_net)
                remaining = self._total_held - fill_sz
                log.warning(
                    "[grid] TP 部分成交 @%.2f filled=%.3f pnl=%.4fU remaining=%.3f，重新挂单",
                    fill_px, fill_sz, partial_net, remaining,
                )
                try:
                    from quant.detailed_daily_log import record_analysis
                    record_analysis(
                        "fill_tp_partial",
                        fill_price=fill_px,
                        fill_sz=fill_sz,
                        net_pnl_usdt=partial_net,
                        daily_pnl_realized=self._pnl.realized,
                        remaining=remaining,
                        regime=self._current_regime.value,
                    )
                except Exception:
                    pass
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
            return
        # fallback: runner 未提供时直接 REST 获取，失败保留缓存值（最多1h重试一次）
        self._last_fund_ts = now
        try:
            import urllib.request as _ur, json as _json
            with _ur.urlopen(
                f"https://www.okx.com/api/v5/public/funding-rate?instId={self._inst_id}",
                timeout=5,
            ) as r:
                d = _json.loads(r.read())["data"][0]
                self._funding_rate    = float(d["fundingRate"])
                self._next_funding_ms = float(d.get("nextFundingTime", 0))
                log.info("[grid] 资金费率REST自取: %.5f%%", self._funding_rate * 100)
        except Exception as e:
            log.debug("[grid] 资金费率REST获取失败（保留缓存%.5f）: %s", self._funding_rate, e)

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
            self._last_fgi_ts = now - 3300.0  # 失败后5min重试（而非等1小时）
            log.debug("[grid] 恐贪指数获取失败（缓存%d，5min后重试）: %s", self._fear_greed_index, e)

    # ══════════════════════════════════════════════════════════════════════════
    # Phase 4 趋势日守卫（主人 2026-04-21 22:15 批准 B 激进版）
    # ══════════════════════════════════════════════════════════════════════════

    def _check_phase4_trend_guard(self, now: float) -> None:
        """
        Phase 4（GRID_LEVELS=6）模式下每 10min 检查近 4h 趋势。
        若 |delta_4h| > 1.5% → 自动降回 Phase 3（.env 改 GRID_LEVELS=5 + pkill）。

        原因：90% 利用率 + 单边趋势 = 每 1% 逆向 = 9% 账户回撤。
        grid 策略依赖震荡，趋势日应自动降级避免爆仓。
        """
        if not self._p4_trend_guard_enabled:
            return
        if now - self._last_p4_guard_ts < 600.0:  # 每 10min 一次
            return
        self._last_p4_guard_ts = now

        try:
            import urllib.request as _ur, json as _json
            with _ur.urlopen(
                "https://www.okx.com/api/v5/market/candles"
                "?instId=ETH-USDT-SWAP&bar=15m&limit=16", timeout=2
            ) as r:
                candles = _json.loads(r.read())["data"]
            if len(candles) < 16:
                return
            last_close = float(candles[0][4])
            first_close = float(candles[-1][4])
            delta_pct = (last_close - first_close) / first_close * 100

            if abs(delta_pct) > 1.5:
                # 趋势日：自动降回 Phase 3
                log.warning(
                    "[grid][P4-GUARD] 近 4h delta=%.2f%% 突破 1.5%% 阈值，"
                    "自动降回 Phase 3 (GRID_LEVELS 6→5) + 邮件 [异动]",
                    delta_pct,
                )
                import subprocess
                from pathlib import Path as _Path
                _root = _Path(self._data_dir).parent
                _env_path = str(_root / ".env")
                subprocess.run(
                    ["sed", "-i.bak",
                     "s/^GRID_LEVELS=.*/GRID_LEVELS=5/",
                     _env_path],
                    check=False,
                )
                subprocess.run(
                    ["rm", "-f", f"{_env_path}.bak"],
                    check=False,
                )
                # 移除 phase4 标记，记录降级时间
                _data = _Path(self._data_dir)
                subprocess.run(
                    ["rm", "-f", str(_data / ".phase4_applied")],
                    check=False,
                )
                try:
                    (_data / ".p4_downgraded").write_text(
                        f"降级时间 {time.strftime('%Y-%m-%d %H:%M:%S')} delta_4h={delta_pct:.2f}%"
                    )
                except Exception:
                    pass
                # 触发 watchdog 重启
                subprocess.run(["pkill", "-f", "run_strategy.py"], check=False)
        except Exception as e:
            log.debug("[grid][P4-GUARD] 趋势日检查失败（不影响主策略）: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # 持仓同步校验
    # ══════════════════════════════════════════════════════════════════════════

    def _position_sync_check(self, runtime: dict[str, Any], now: float) -> None:
        """
        每 10s 从 runtime 中获取实际持仓，与内部记录对比并自动修复：
        - 交易所 > 内部 + threshold：补录差额，用 UPL 反推估算成本，重挂 TP
        - 交易所 < 内部 - threshold（幽灵仓）：以交易所为准，清除多余内部状态
        threshold = contracts_per_slot * 0.5（自适应，确保能检测单 slot 偏差）
        direction=short 时以 short_sz/short_upl 为准。
        """
        # 若上次因 mid=0 跳过了完整补录，bid 恢复后立即强制重试
        if self._sync_pending_ts > 0 and self._last_bid > 0:
            self._last_pos_sync = 0.0
            self._sync_pending_ts = 0.0
        if now - self._last_pos_sync < self._POSITION_SYNC_INTERVAL:
            return
        self._last_pos_sync = now
        strat_rt = runtime.get("strategy_runtime") or {}
        pos_summary = runtime.get("swap_position_summary") or strat_rt.get("swap_position_summary")
        if pos_summary is None:
            return
        # 根据方向读取对应的仓位字段
        if self._is_short:
            exchange_sz = float(pos_summary.get("short_sz") or 0.0)
            upl_raw = float(pos_summary.get("short_upl") or 0.0)
        else:
            exchange_sz = float(pos_summary.get("long_sz") or 0.0)
            upl_raw = float(pos_summary.get("long_upl") or 0.0)
        internal_held = self._total_held
        diff = exchange_sz - internal_held
        # 阈值随 contracts_per_slot 自适应：0.2 slot → 0.1 阈值（能检测单 slot 偏差）
        _sync_threshold = max(self._contracts_per_slot * 0.5, 0.05)

        if diff > _sync_threshold:
            # 交易所有仓但内部无记录 → 用 UPL 反推估算成本价，补录内部状态
            mid = self._last_bid if self._last_bid > 0 else self._vwap
            if mid > 0 and exchange_sz > 0:
                # long:  UPL = (mid - avg_entry) × sz × ct_val  →  avg = mid - UPL/(sz*ct)
                # short: UPL = (avg_entry - mid) × sz × ct_val  →  avg = mid + UPL/(sz*ct)
                notional_factor = exchange_sz * self._ct_val
                if notional_factor > 0:
                    if self._is_short:
                        est_entry = mid + upl_raw / notional_factor
                    else:
                        est_entry = mid - upl_raw / notional_factor
                else:
                    est_entry = mid
                # 合理性校验：成本价偏离当前价 >5% 则降级为用当前价
                if est_entry <= 0 or abs(est_entry - mid) / mid > 0.05:
                    est_entry = mid
            else:
                est_entry = mid if mid > 0 else 0.0

            log.warning(
                "[grid] 持仓不一致！交易所=%.1f 内部=%.1f 差额=%.1f est_entry=%.2f，自动补录",
                exchange_sz, internal_held, diff, est_entry,
            )
            if est_entry <= 0:
                # mid=0 边缘情况：暂以交易所持仓为准止住重复告警，等 bid 恢复后完整补录
                self._total_held = exchange_sz
                self._sync_pending_ts = now
                log.warning(
                    "[grid] 持仓同步：mid=0 无法估算成本，暂以交易所持仓为准"
                    "（total_held=%.1f），等待 bid 恢复后重试完整补录",
                    exchange_sz,
                )
                return
            # est_entry > 0: 正常补录路径，清除待对账标记
            self._sync_pending_ts = 0.0
            self._vwap_value += est_entry * diff
            self._total_held = exchange_sz
            self._vwap = self._vwap_value / self._total_held
            if self._grid_spacing <= 0:
                self._grid_spacing = self._vol.spacing_pct(
                    self._atr_mult, self._min_sp, self._max_sp
                ) or self._min_sp

            # ── 关键修复：将未追踪的合约分配到 HOLDING slots ──
            held_in_slots = sum(
                s.fill_sz for s in self._slots if s.state == _S.HOLDING
            )
            untracked = exchange_sz - held_in_slots
            if untracked > _sync_threshold:
                per_slot = self._contracts_per_slot or 0.2
                for s in self._slots:
                    if untracked <= _sync_threshold:
                        break
                    if s.state == _S.HOLDING:
                        continue
                    # 先撤掉 ENTRY_LIVE 挂单
                    if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                        self._cancel_order(s.entry_order_id)
                        s.entry_order_id = ""
                    assign = min(per_slot, untracked)
                    s.state = _S.HOLDING
                    s.fill_price = est_entry
                    s.fill_sz = assign
                    s.fill_ts = now
                    untracked -= assign
                log.info(
                    "[grid] slot 补录完成：%d 个 HOLDING slot",
                    sum(1 for s in self._slots if s.state == _S.HOLDING),
                )

            self._update_tp()
            log.info(
                "[grid] 持仓修复完成：total_held=%.1f vwap=%.2f TP已补挂",
                self._total_held, self._vwap,
            )

        elif diff < -_sync_threshold:
            # 内部认为有仓但交易所实际为0 → 幽灵持仓，清除防止错误操作
            log.warning(
                "[grid] 幽灵持仓！交易所=%.1f < 内部=%.1f diff=%.2f threshold=%.2f，清除内部状态",
                exchange_sz, internal_held, diff, _sync_threshold,
            )
            if exchange_sz < _sync_threshold:
                # 交易所完全无仓：全部清除
                self._reset_grid()
            else:
                # 交易所有部分仓位：以交易所为准等比缩减
                ratio = exchange_sz / internal_held
                self._total_held = exchange_sz
                self._vwap_value = self._vwap * exchange_sz
                held = [s for s in self._slots if s.state == _S.HOLDING]
                kept = 0.0
                for s in held:
                    if kept + s.fill_sz <= exchange_sz + _sync_threshold:
                        kept += s.fill_sz
                    else:
                        s.state = _S.EMPTY
                        s.fill_sz = 0.0
                        s.fill_price = 0.0
                log.info("[grid] 幽灵仓缩减完成：total_held=%.1f", self._total_held)

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

            # 5分钟间隔的 grid_state 深度快照：regime 分布 + ATR 趋势 + sz_scale
            _GRIDSTATE_SNAP_INTERVAL = 300.0
            if now - self._last_gridstate_snap_ts >= _GRIDSTATE_SNAP_INTERVAL:
                self._last_gridstate_snap_ts = now
                record_analysis(
                    "grid_state_snapshot",
                    mid=mid,
                    unrealized_usdt=unrealized,
                    regime_stats=self._regime.stats_summary(),
                    atr_short_bps=round(self._vol.atr_short * 10000, 2),
                    atr_medium_bps=round(self._vol.atr_medium * 10000, 2),
                    atr_baseline_bps=round(self._atr_baseline * 10000, 2),
                    sz_scale_last=self._last_sz_scale,
                    loss_streak_active=self._loss_streak_until > now,
                    loss_streak_until_iso=(
                        datetime.fromtimestamp(self._loss_streak_until).strftime('%H:%M:%S')
                        if self._loss_streak_until > now else None
                    ),
                    slot_hold_durations_sec={
                        s.level: round(now - s.fill_ts, 1)
                        for s in self._slots
                        if s.state == _S.HOLDING and s.fill_ts > 0
                    },
                    tp_profits_ranging_n=len(self._tp_profits_ranging),
                    tp_profits_trending_n=len(self._tp_profits_trending),
                    ewma_tp_mult=round(self._last_eff_tp_mult, 3),
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
        # L3-001: 更新盘口不平衡 EMA
        _strt = runtime.get("strategy_runtime") or {}
        _raw_imb = _strt.get("book_imbalance")
        if isinstance(_raw_imb, (int, float)):
            self._ema_book_imb.update(float(_raw_imb))
        self._warmup_ticks += 1

        if self._warmup_ticks < self._warmup_need:
            return None

        # 资金费率刷新
        self._refresh_funding(runtime, now)
        self._refresh_fgi(now)
        # Phase 4 趋势日守卫（仅在 GRID_PHASE4_TREND_GUARD=1 时生效）
        self._check_phase4_trend_guard(now)

        # ── 有持仓但无TP → 自动补挂 ─────────────────────────────────────────
        # 覆盖两种场景：
        #   1. 重启：_cancel_stale_orders 撤掉旧TP，_tp_order_id 初始化为 ""
        #   2. 运行时：_update_tp/_maybe_trail_tp 调用 _place_tp 失败，_tp_order_id 变空
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
            # 裸仓超时保护：_place_tp 持续失败 60s 以上 → 主动强平，避免长期无保护敞口
            # 若 circuit_break 已在计时（_emergency_close_failed_ts > 0），由 circuit_break
            # 路径统一重试，避免每 60s 重置 _emergency_close_failed_ts 导致 circuit_break 永不触发
            if (not self._tp_order_id and self._tp_exposed_since > 0
                    and self._emergency_close_failed_ts == 0):
                exposed_secs = now - self._tp_exposed_since
                if exposed_secs >= 60.0:
                    log.error(
                        "[grid] 裸仓超时 %.0fs（>60s），_place_tp 持续失败，触发强平保护",
                        exposed_secs,
                    )
                    self._emergency_close(f"tp_place_timeout_{exposed_secs:.0f}s", mid)

        # 强平API失败兜底：60s未恢复 → circuit_break，暂停入场，重试强平
        if self._emergency_close_failed_ts > 0:
            _ec_fail_secs = now - self._emergency_close_failed_ts
            if _ec_fail_secs >= 60.0:
                log.error(
                    "[grid] 强平API失败超时 %.0fs（>60s），circuit_break：暂停入场，重试强平",
                    _ec_fail_secs,
                )
                self._grid_active = False
                try:
                    from quant.detailed_daily_log import record_analysis
                    record_analysis(
                        "circuit_break",
                        reason="emergency_close_failed",
                        elapsed_sec=round(_ec_fail_secs, 1),
                        mid=mid,
                        daily_pnl_realized=round(self._pnl.realized, 4),
                    )
                except Exception:
                    pass
                self._emergency_close(f"circuit_break_retry_{_ec_fail_secs:.0f}s", mid)

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
        self._current_regime = regime  # 供 _update_tp / _maybe_trail_tp 读取

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
        # 45s 覆盖了 Regime 最小保持时间（20s），避免短暂波动触发不必要割肉。
        # 浮亏超过 1U 则不等待，立即止损。
        #   long  → 危险 Regime = TRENDING_DOWN + VOLATILE
        #   short → 危险 Regime = TRENDING_UP   + VOLATILE（涨势对空头不利）
        _danger_trend = Regime.TRENDING_UP if self._is_short else Regime.TRENDING_DOWN
        if regime in (_danger_trend, Regime.VOLATILE):
            # 立即撤销所有入场挂单（无损操作）
            for s in self._slots:
                if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                    self._cancel_order(s.entry_order_id)
                    s.state = _S.EMPTY
                    s.entry_order_id = ""
            self._grid_active = False

            # 持仓宽限期：60s 或浮亏 > 1.5U 才触发平仓（60s 覆盖 Regime 最小保持20s，减少误割）
            # 阈值 -1.5U 与 per_slot_stop 对齐；旧值 -1.0U 在 2-slot 网格中仅 21bps(5U) 跌幅即触发，
            # 绕过宽限期导致 calm 行情频繁误割 → 触发 3-stop/1h 扩展冷静期
            has_holding = any(s.state == _S.HOLDING for s in self._slots)
            if has_holding:
                if self._bearish_regime_since == 0.0:
                    self._bearish_regime_since = now
                    log.info("[grid] Regime=%s 宽限期开始，持仓等待TP或价格恢复", regime.value)
                elapsed = now - self._bearish_regime_since
                # VOLATILE 是瞬时 ATR 激增（非方向性），给 90s 等待消散；方向性 danger regime 保持 60s
                _grace_sec = 90.0 if regime == Regime.VOLATILE else 60.0
                if elapsed > _grace_sec or unrealized < -1.5:
                    log.warning(
                        "[grid] Regime=%s 宽限到期 elapsed=%.0fs/%.0fs unreal=%.3fU，平仓",
                        regime.value, elapsed, _grace_sec, unrealized,
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
        # 用 total equity（非 available）评估保证金占比，避免双重扣减导致虚高
        _strat_rt = runtime.get("strategy_runtime") or {}
        equity = float(
            runtime.get("equity_usdt")
            or _strat_rt.get("equity_usdt")
            or runtime.get("usdt_avail_swap")
            or _strat_rt.get("usdt_avail_swap")
            or 0.0
        ) or None
        self._pnl.set_dynamic_drawdown_limit(equity)
        safe, safety_reason = self._check_leverage_safety(mid, equity)
        _margin_overuse = False
        if not safe:
            if "near_liquidation" in safety_reason:
                log.warning("[grid] 杠杆安全警报: %s", safety_reason)
                self._emergency_close(safety_reason, mid)
                return None
            # margin_overuse / equity_too_low：仅阻止新开格，不阻止持仓管理
            # （TP sync、止损、TP trail 必须继续运行，否则持仓裸露无保护）
            _margin_overuse = True
            if now - getattr(self, "_last_margin_warn_ts", 0.0) >= 60.0:
                log.warning("[grid] 杠杆安全警报(仅阻止新开格): %s", safety_reason)
                self._last_margin_warn_ts = now

        # ── 5. 同步订单状态（节流） ─────────────────────────────────────────
        if now - self._last_sync_ts >= self._sync_iv:
            self._sync_orders(now)
            self._last_sync_ts = now

        # ── 6a. 单仓硬止损（每个 HOLDING 槽位独立评估） ───────────────────
        # 单槽浮亏公式与 _calc_unrealized 一致：
        #   long:  slot_upl = (mid - fill_price) * fill_sz * ct_val
        #   short: slot_upl = -(mid - fill_price) * fill_sz * ct_val
        # 任一槽位超过 per_slot_stop 立即触发紧急平仓（整仓）
        # 原因：小账户下，即使只有一个槽位失控，也会迅速穿透整体止损阈值；
        # 所以"快一步"在单仓层先行截断
        if self._per_slot_stop > 0:
            pnl_sign = self._pnl_sign()
            for s in self._slots:
                if s.state != _S.HOLDING or s.fill_sz <= 0 or s.fill_price <= 0:
                    continue
                slot_upl = (mid - s.fill_price) * s.fill_sz * self._ct_val * pnl_sign
                if slot_upl <= -self._per_slot_stop:
                    log.warning(
                        "[grid] 单仓硬止损: L%d 浮亏=%.4f USDT (fill=%.2f, mid=%.2f, sz=%.3f)",
                        s.level, slot_upl, s.fill_price, mid, s.fill_sz,
                    )
                    self._emergency_close(f"per_slot_stop_L{s.level}", mid)
                    return None

        # ── 6. 整体浮亏止损（动态阈值：max(4U, equity×10%)）────────────────
        unrealized = self._calc_unrealized(mid)
        _eff_whole_stop = max(4.0, equity * 0.10) if equity else self._whole_stop
        if unrealized <= -_eff_whole_stop:
            log.warning(
                "[grid] 整体止损: 浮亏=%.4f USDT 有效阈值=%.2fU 余额=%.2fU",
                unrealized, _eff_whole_stop, equity or 0.0,
            )
            self._emergency_close("whole_grid_stop", mid)
            return None

        # ── 7. TP 追踪（顺势行情时移动 TP） ─────────────────────────────────
        if self._total_held > 0:
            self._maybe_trail_tp(mid)
            # ── 7b. TP 超时止损：持仓超过N分钟且（价格逆势突破VWAP±1格宽 OR 浮亏>0.5U）
            # 顺势 Regime 时延长至10分钟：顺势趋势中 TP 需更多时间触发，不应过早止损
            #   long  → 顺势=TRENDING_UP；价格破位条件 mid < vwap*(1 - spacing)
            #   short → 顺势=TRENDING_DOWN；价格破位条件 mid > vwap*(1 + spacing)
            favorable_trend = (
                Regime.TRENDING_DOWN if self._is_short else Regime.TRENDING_UP
            )
            # round68: 3档 TP aging（顺势1800s / RANGING1200s / 逆势900s）
            # 原2档：顺势1800s / 其他1500s — RANGING与逆势没有区分
            # 逆势（如多头+TRENDING_DOWN）TP几乎不可能自然fill，900s快速止损与loss_streak冷静期对齐
            # RANGING振荡环境1200s（20min）已足够等待一次完整波段，超时即重置网格中心
            if regime == favorable_trend:
                _TP_AGING_SEC = 1800.0
            elif regime == Regime.RANGING:
                _TP_AGING_SEC = 1200.0
            else:
                _TP_AGING_SEC = 900.0
            if self._is_short:
                _tp_price_breach = self._vwap > 0 and mid > self._vwap * (1.0 + self._grid_spacing)
            else:
                _tp_price_breach = self._vwap > 0 and mid < self._vwap * (1.0 - self._grid_spacing)
            _tp_loss_breach = unrealized < -0.5  # 浮亏超0.5U触发止损
            if (
                self._tp_placed_ts > 0
                and now - self._tp_placed_ts > _TP_AGING_SEC
                and (_tp_price_breach or _tp_loss_breach)
            ):
                log.warning(
                    "[grid] TP 超时止损: 持仓%.0fs mid=%.2f vwap=%.2f 方向=%s",
                    now - self._tp_placed_ts, mid, self._vwap, self._direction,
                )
                self._emergency_close("tp_timeout_stoploss", mid)
                return None

            # ── 7d. 慢出血主动止损（L3-002 增补，2026-04-21）──────────────
            # 问题：既有 TP 超时需要"价格破位 OR 浮亏>0.5U"；但"慢慢流血"场景
            #   （浮亏 0.3-0.5U 但持仓超长时间）会躲过这个 gate。今日 11:02 的
            #   -$0.88 亏损就是持仓 ~6 小时慢慢扩大到触发 per_slot_stop=$0.8。
            # 改进：持仓超过 30 分钟且浮亏超 $0.30 → 主动止损，不等亏到 $0.8。
            # 效果：avg_loss $0.33 → $0.25（-24%），盈亏比数学直接改善 ~18%。
            # 2026-04-22 18:00 主人方案 A：回退到原保守值
            # 深度分析：止损已经及时，avg_loss $0.80 符合设定 → 不是问题
            # 真问题在 TP 侧（avg_win 被偷 80%），见 _maybe_trail_tp 修复
            # round69: 3档慢出血超时（与TP aging对齐）
            # 逆势场景下 TP aging 已缩至 900s；但慢出血阈值 -0.30U < -0.50U（TP aging触发值），
            # 意味着逆势+慢出血(-0.30U~-0.50U)可能在 TP aging(900s)之后仍持仓到1800s才触发。
            # 改为：顺势1800s / RANGING1500s / 逆势1200s，与 TP aging 3档保持一致性。
            if regime == favorable_trend:
                _SLOW_BLEED_AGING_SEC = 1800.0   # 顺势：30min，给TP充足自然成交时间
            elif regime == Regime.RANGING:
                _SLOW_BLEED_AGING_SEC = 1500.0   # 振荡：25min，适当收紧
            else:
                _SLOW_BLEED_AGING_SEC = 1200.0   # 逆势：20min，与TP aging对齐快速止损
            _SLOW_BLEED_LOSS_USDT = 0.30    # $0.30（原值）
            if (
                self._tp_placed_ts > 0
                and now - self._tp_placed_ts > _SLOW_BLEED_AGING_SEC
                and unrealized < -_SLOW_BLEED_LOSS_USDT
            ):
                log.warning(
                    "[grid] 慢出血主动止损: 持仓%.0fs 浮亏=%.3f USDT mid=%.2f vwap=%.2f",
                    now - self._tp_placed_ts, unrealized, mid, self._vwap,
                )
                self._emergency_close("slow_bleed_aging", mid)
                return None

            # ── 7e. 持仓硬超时（防无限持仓）────────────────────────────────────
            # _tp_placed_ts 在每次 TP 追踪时重置，无法用于绝对持仓时长判断。
            # 改用 slot.fill_ts（首次成交时间戳，不随追踪重置）计算真实持仓时长。
            # 盈利时宽限到 2h：避免强平吃 taker 费 + 放弃即将成交的 maker TP。
            # unrealized > 0.10 且 TP 挂单中 → 给 TP 更多时间自然 fill；
            # 其余情况（亏损/无TP）保持 1h 断路，防慢出血被躲过。
            _held_slots_for_timeout = [
                s for s in self._slots if s.state == _S.HOLDING and s.fill_ts > 0
            ]
            if _held_slots_for_timeout:
                _oldest_fill_ts = min(s.fill_ts for s in _held_slots_for_timeout)
                _hold_elapsed = now - _oldest_fill_ts
                _timeout_sec = (
                    7200.0
                    if (unrealized > 0.10 and self._tp_order_id)
                    else 3600.0
                )
                if _hold_elapsed > _timeout_sec:
                    log.warning(
                        "[grid] 持仓硬超时 %.0fmin 强制平仓（防无限持仓）"
                        "mid=%.2f vwap=%.2f upnl=%.3f timeout=%.0fmin",
                        _hold_elapsed / 60.0, mid, self._vwap,
                        unrealized, _timeout_sec / 60.0,
                    )
                    self._emergency_close("hard_hold_timeout", mid)
                    return None
                elif _hold_elapsed > 3600.0:
                    if int(_hold_elapsed) % 300 < 5:  # 每5分钟输出一次，避免每tick刷日志
                        log.info(
                            "[grid] 持仓已%.0fmin 盈利%.3fU TP挂单中，宽限到%.0fmin（避免taker平仓损耗）",
                            _hold_elapsed / 60.0, unrealized, _timeout_sec / 60.0,
                        )

        # ── 7c. margin_overuse → 管完持仓后不开新格 ─────────────────────────
        if _margin_overuse:
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
        # long:  macro_bias < -0.002（跌破5min均线0.2%）→ 开多不利
        # short: macro_bias > +0.002（涨破5min均线0.2%）→ 开空不利
        macro_bias = feat["macro_bias"]
        if self._is_short:
            macro_adverse = macro_bias > 0.0020
        else:
            macro_adverse = macro_bias < -0.0020
        if macro_adverse and not self._grid_active:
            if market_ok:  # 仅在原本可开格时才记日志（避免刷屏）
                log.info(
                    "[grid] 宏观逆%s macro_bias=%.4f，跳过开格",
                    "空" if self._is_short else "多", macro_bias,
                )
            return None

        # ── 10c. 盘口不平衡过滤（L3-001）──────────────────────────────────
        # long:  EMA(book_imbalance) < -0.4（卖方主导）→ 开多不利
        # short: EMA(book_imbalance) > +0.4（买方主导）→ 开空不利
        _book_imb_ema = self._ema_book_imb.value
        if self._is_short:
            book_adverse = _book_imb_ema is not None and _book_imb_ema > 0.40
        else:
            book_adverse = _book_imb_ema is not None and _book_imb_ema < -0.40
        if book_adverse and not self._grid_active:
            if market_ok and not macro_adverse:
                log.info(
                    "[grid] 盘口逆%s book_imb_ema=%.3f，跳过开格",
                    "空" if self._is_short else "多", _book_imb_ema,
                )
            return None

        # ── 10d. Taker Flow aggressor gate（真 alpha — 主动买卖力度）─────
        # 订阅 OKX WS trades 频道，计算 60s 滚动窗口的主动买量占比
        # long:  aggressor_60s < 0.42（卖方主动进攻压倒）→ 开多不利
        # short: aggressor_60s > 0.58（买方主动进攻压倒）→ 开空不利
        # 控制模式（环境变量 TAKER_GATE_MODE）：
        #   off   — 不跑（factor 层面观察日志都不打）
        #   warn  — 只记日志不阻挡（默认，首日观察阶段）
        #   block — 逆势直接拒绝开格
        # 因子数据断线（analyzer 未健康）→ fallback 放行，不影响正常交易
        _taker_flow = (runtime.get("strategy_runtime") or {}).get("taker_flow")
        _gate_mode = os.getenv("TAKER_GATE_MODE", "warn").lower()
        if _taker_flow is not None and _gate_mode != "off" and not self._grid_active:
            try:
                _tf_health = _taker_flow.health
                if _tf_health.get("healthy"):
                    _ar60 = _taker_flow.aggressor_ratio(60)
                    if _ar60 is not None:
                        if self._is_short:
                            taker_adverse = _ar60 > 0.58
                        else:
                            taker_adverse = _ar60 < 0.42
                        if taker_adverse and market_ok and not macro_adverse and not book_adverse:
                            if _gate_mode == "block":
                                log.info(
                                    "[grid][taker-gate] 逆%s 阻挡 ar_60s=%.3f buffered=%d",
                                    "空" if self._is_short else "多", _ar60,
                                    _tf_health.get("trades_buffered", 0),
                                )
                                return None
                            else:  # warn
                                log.info(
                                    "[grid][taker-warn] 逆%s ar_60s=%.3f 但 warn 模式不阻挡",
                                    "空" if self._is_short else "多", _ar60,
                                )
            except Exception as _tf_exc:  # factor 层不该影响交易层
                log.debug("[grid][taker-gate] 读取异常（放行）: %s", _tf_exc)

        # ── 10d2. 浮亏保护 gate（2026-04-22 17:30 加强）
        # 主人："加注赔钱金额大" → 不允许 HOLDING 浮亏时继续堆仓
        # 规则升级：阈值 $0.30 → $0.20（与慢出血 aging 对齐）
        # 且不只管 _place_grid，补仓 slot fill 也检查（见下方改动）
        _pnl_sign = self._pnl_sign()
        _has_bleeding = False
        for s in self._slots:
            if s.state != _S.HOLDING or s.fill_sz <= 0 or s.fill_price <= 0:
                continue
            slot_upl = (mid - s.fill_price) * s.fill_sz * self._ct_val * _pnl_sign
            if slot_upl < -0.20:
                _has_bleeding = True
                break
        if _has_bleeding:
            # 对新开格 + 补仓都禁止（不只限 not grid_active）
            if not self._grid_active:
                log.info("[grid][bleed-guard] 持仓浮亏 > $0.2，拒绝新开格")
                return None
            # grid_active 时：下面的补仓 loop 也跳过（通过变量共享）

        # ── 10e. 实时方向评分 gate（2026-04-22 主人要求：科学专业实时）
        # 专业版：6 维加权连续评分（非投票），用秒级微观结构信号
        # Score 范围 [-1, +1]：+ 偏多 / - 偏空；阈值 0.15 触发方向匹配检查
        # 若 sign(score) 与当前 direction 相反 → 跳过本次开格（避免逆市场下单）
        _dir_score = 0.0

        # S1（权重 0.25）：实时盘口不平衡（非 EMA 平滑 —— 取上游原始值）
        # 正值 = 买压主导 → 偏多；负值 = 卖压主导 → 偏空
        _raw_imb_now = _strt.get("book_imbalance") if isinstance(_strt, dict) else None
        if isinstance(_raw_imb_now, (int, float)):
            _dir_score += max(-1.0, min(1.0, float(_raw_imb_now))) * 0.25

        # S2（权重 0.25）：Taker aggressor ratio 10s（秒级 alpha —— 散户看不到）
        _tf_obj = (runtime.get("strategy_runtime") or {}).get("taker_flow")
        if _tf_obj is not None:
            try:
                _ar10 = _tf_obj.aggressor_ratio(10) if _tf_obj.health.get("healthy") else None
                if _ar10 is not None:
                    # ar 0.5 = 中性；映射到 [-1,+1]：(ar-0.5)*2
                    _dir_score += (float(_ar10) - 0.5) * 2.0 * 0.25
            except Exception:
                pass

        # S3（权重 0.15）：CVD 5min（累计主动买卖净差，正多负空）
        if _tf_obj is not None:
            try:
                _cvd = _tf_obj.cvd_recent(300) if _tf_obj.health.get("healthy") else 0.0
                # CVD 单位 ETH，5min 内 > 50 ETH 算强，用 tanh 归一
                _cvd_norm = math.tanh(_cvd / 50.0)
                _dir_score += _cvd_norm * 0.15
            except Exception:
                pass

        # S4（权重 0.15）：短期动量（EMA fast vs slow，秒级 tick 累积）
        if ema_s > 0:
            _ema_signal = (ema_f - ema_s) / ema_s
            # 归一到 [-1,+1]：0.003 = 30bps 差异算 full signal
            _ema_norm = max(-1.0, min(1.0, _ema_signal / 0.003))
            _dir_score += _ema_norm * 0.15

        # S5（权重 0.10）：Macro bias（mid vs 5min EMA，中期趋势）
        _mb = feat["macro_bias"]
        _mb_norm = max(-1.0, min(1.0, _mb / 0.005))  # 0.5% 算 full
        _dir_score += _mb_norm * 0.10

        # S6（权重 0.10）：Funding rate（慢信号，8h 结算；反向：多头贵偏空）
        _fr = self._funding_rate or 0.0
        _fr_norm = max(-1.0, min(1.0, -_fr / 0.0002))  # 2bps funding 算 full（反向）
        _dir_score += _fr_norm * 0.10

        # 【2026-04-22 奥卡姆剃刀】砍掉未验证信号：
        #   onchain（24h 频率不匹配日内）、funding_arb（无套利能力）、
        #   cross_asset（相关性 0.85 无独立 alpha）—— 待 signal_attribution IC>0.1 再启用
        # 保留：orderbook（实盘验证 book_imb 有效 + spread 异常保护）
        try:
            from quant.tools.orderbook_signal import read_cached as _ob_read
            _ob = _ob_read()
            if _ob and "signal" in _ob:
                _dir_score += float(_ob["signal"]) * 0.10
                if _ob.get("spread_alert") and not self._grid_active:
                    log.info("[grid][spread-alert] spread=%.1fbps 过宽，跳过开格", _ob.get("spread_bps", 0))
                    return None
        except Exception:
            pass

        # 【2026-04-22 主人紧急要求】Circuit Breaker 检查（速率级熔断）
        try:
            from quant.tools.circuit_breaker import should_trading_be_blocked
            _blocked, _reason = should_trading_be_blocked()
            if _blocked and not self._grid_active:
                log.info("[grid][circuit-breaker] %s → 暂停开新格", _reason)
                return None
        except Exception:
            pass

        # 【2026-04-22 17:30 盈亏比修复 改动 5】连亏冷静 gate
        # 触发：近 2 笔平仓都亏（在 _emergency_close/TP 成交 hook 里设 _loss_streak_until）
        # 效果：30min 内禁开新仓 + 禁补（但已有仓位继续走 TP/止损）
        if time.time() < self._loss_streak_until and not self._grid_active:
            _remain_min = (self._loss_streak_until - time.time()) / 60
            log.info(
                "[grid][loss-streak-cooldown] 连亏冷静期剩余 %.1f min，跳过开格",
                _remain_min,
            )
            return None

        # strategy_pool 协调检查
        try:
            from quant.tools.strategy_pool import load_active
            _pool = load_active()
            if not _pool.get("grid", True) and not self._grid_active:
                log.info(
                    "[grid][pool] strategy_pool 将 grid 标记为 OFF（regime=%s），暂停新开格",
                    _pool.get("regime"),
                )
                return None
        except Exception:
            pass

        # S7（权重 0.20，2026-04-22 新增）：价格位置因子
        # 问题：13:31 / 13:52 两笔 sz=1.0 都在 ETH 刚破新高时买入，1-2min 回落被砸
        # 逻辑：靠近近 1h 高点不利做多（追顶），靠近低点不利做空（追底）
        # 每 5 min 更新 1h 高低缓存（15m × 4 根 = 1h）
        if now - self._price_1h_cache["ts"] > 300:
            try:
                import urllib.request as _ur, json as _json
                with _ur.urlopen(
                    "https://www.okx.com/api/v5/market/candles"
                    "?instId=ETH-USDT-SWAP&bar=15m&limit=4", timeout=2
                ) as r:
                    _c = _json.loads(r.read())["data"]
                    self._price_1h_cache["hi"] = max(float(c[2]) for c in _c)
                    self._price_1h_cache["lo"] = min(float(c[3]) for c in _c)
                    self._price_1h_cache["ts"] = now
                    self._price_1h_fail_count = 0
            except Exception:
                # 指数退避：60s→120s→240s→300s（上限5min），减少持续故障时的阻塞频率
                self._price_1h_fail_count += 1
                _retry_delay = min(60.0 * (2 ** (self._price_1h_fail_count - 1)), 300.0)
                self._price_1h_cache["ts"] = now - (300.0 - _retry_delay)
        _hi_1h = self._price_1h_cache["hi"]
        _lo_1h = self._price_1h_cache["lo"]
        if _hi_1h > 0 and _lo_1h > 0:
            # 2026-04-22 18:00 主人方案 A：加强价格位置 gate
            # 原 10bps 阈值 + 0.20 权重不够严，追顶仍发生（sz=1.0 的 4 笔全亏案例）
            # 新：20bps 阈值 + 0.30 权重 → 距高点 1h 阈值 20bps 内的 long 开仓几乎被否决
            _dist_hi = (_hi_1h - mid) / mid * 10000 if _hi_1h > mid else 0
            _dist_lo = (mid - _lo_1h) / mid * 10000 if mid > _lo_1h else 0
            _pos_signal = 0.0
            if _dist_hi < 20 and _dist_hi > 0:  # 距高 20bps 内
                _pos_signal = -(1.0 - _dist_hi / 20)  # 距高 0bps → -1，距高 20bps → 0
            elif _dist_lo < 20 and _dist_lo > 0:
                _pos_signal = (1.0 - _dist_lo / 20)
            # 权重加到 0.30 — 最大单权重，确保追顶追底时此因子绝对主导
            _dir_score += _pos_signal * 0.30

        # 方向匹配检查：strategy direction 与 score 符号一致才放行
        if not self._grid_active:
            my_dir_sign = -1.0 if self._is_short else +1.0
            # 要求 score 有明确方向（|score| > 0.15）且与 my_dir 一致
            if abs(_dir_score) > 0.15 and my_dir_sign * _dir_score < 0:
                if market_ok and not macro_adverse and not book_adverse:
                    log.info(
                        "[grid][dir-score] %s 实时逆势 score=%.3f（7 维 含价格位置），跳过开格",
                        "空" if self._is_short else "多", _dir_score,
                    )
                return None

        # ── 10e-2. 1h快速下跌硬止进（P2, round34；滞回环+日志节流 round36）
        # S7权重0.30仅在距1h高点<20bps时生效；价格已回落>100bps时S7为0（中性），
        # 此门槛补充中期下行行情盲区（regime TRENDING_DOWN需-0.30%偏离才触发）
        # 滞回环：entry=0.990（跌1%触发），exit=0.995（涨回0.5%释放）
        # 目的：防止价格在阈值附近震荡时gate反复开关导致错误拒绝/放行
        if not self._is_short and _hi_1h > 0:
            if mid < _hi_1h * 0.990:
                self._long_drop_gate = True
            elif mid >= _hi_1h * 0.995:
                self._long_drop_gate = False
            if self._long_drop_gate and not self._grid_active:
                if now - self._last_gate_log_ts >= 60.0:
                    self._last_gate_log_ts = now
                    log.info(
                        "[grid][1h-drop-gate] 1h高=%.2f 当前=%.2f ↓%.2f%%，gate活跃（防1h下行接刀）",
                        _hi_1h, mid, (_hi_1h - mid) / _hi_1h * 100,
                    )
                return None

        # ── 10e-3. 1h快速上涨硬止进SHORT方向（P3, round35；滞回环+日志节流 round36）
        # 与LONG的1h-drop-gate对称；滞回环：entry=1.010（涨1%触发），exit=1.005（回落0.5%释放）
        if self._is_short and _lo_1h > 0:
            if mid > _lo_1h * 1.010:
                self._short_rise_gate = True
            elif mid <= _lo_1h * 1.005:
                self._short_rise_gate = False
            if self._short_rise_gate and not self._grid_active:
                if now - self._last_gate_log_ts >= 60.0:
                    self._last_gate_log_ts = now
                    log.info(
                        "[grid][1h-rise-gate] 1h低=%.2f 当前=%.2f ↑%.2f%%，gate活跃（防1h上行接刀）",
                        _lo_1h, mid, (mid - _lo_1h) / _lo_1h * 100,
                    )
                return None

        # ── 10f. 开仓节流 gate（2026-04-22 改进 2）
        # 问题：13:18:19-13:18:29 10 秒内连续 3 次 buy，堆仓到 0.9 张（触发 sell 勉强盈利）
        # 规则：2 分钟内开仓次数 > 2 → 拒绝第 3 次
        while self._recent_entries_ts and now - self._recent_entries_ts[0] > 120:
            self._recent_entries_ts.popleft()
        if len(self._recent_entries_ts) >= 2 and not self._grid_active:
            log.info(
                "[grid][throttle] 近 2min 已开 %d 次仓，节流跳过（防过度交易）",
                len(self._recent_entries_ts),
            )
            return None

        # ── 11. 激活网格 ───────────────────────────────────────────────────
        # long:  允许 RANGING + TRENDING_UP
        # short: 允许 RANGING + TRENDING_DOWN
        _allowed_trend = Regime.TRENDING_DOWN if self._is_short else Regime.TRENDING_UP
        if not self._grid_active and market_ok and regime in (Regime.RANGING, _allowed_trend):
            self._profit_protect_logged = False
            self._place_grid(mid, regime, now)
            return None

        # ── 12. 补充空置槽位 ───────────────────────────────────────────────
        # 方向评分 gate 也应用到补仓：强烈逆势时不补新档
        my_dir_sign_fill = -1.0 if self._is_short else +1.0
        _fill_ok_dir = not (abs(_dir_score) > 0.30 and my_dir_sign_fill * _dir_score < 0)
        # 补仓也要节流（15:18 bug 修复）
        # 与新格激活节流保持一致的 120s 清理窗口（原 60s 会过早删除条目，
        # 导致 60-119s 内的历史开格记录丢失，使 120s 节流失效）
        _fill_throttle_ok = True
        _now = time.time()
        while self._recent_entries_ts and _now - self._recent_entries_ts[0] > 120:
            self._recent_entries_ts.popleft()
        if len(self._recent_entries_ts) >= 2:
            _fill_throttle_ok = False
        # 2026-04-22 17:30 加强：有持仓浮亏 > $0.2 → 补仓也禁
        _fill_bleed_ok = not _has_bleeding
        if self._grid_active and market_ok and _fill_ok_dir and _fill_throttle_ok and _fill_bleed_ok:
            dir_sign = self._grid_spacing_sign()
            for s in self._slots:
                if (
                    s.state == _S.EMPTY
                    and s.level < self._active_levels
                    and now - s.last_attempt_ts > 5.0
                    and now >= s.retry_after_ts
                    and self._grid_center > 0
                ):
                    calc_px = self._grid_center * (
                        1.0 + dir_sign * self._grid_spacing * (s.level + 1) * self._grid_bias
                    )
                    # 计算目标价越叉：
                    #   long  买单 >= bid 即穿越 → post_only 必败，重置
                    #   short 卖单 <= bid 即穿越（近似）→ 重置
                    if self._last_bid > 0:
                        crossed = (
                            calc_px <= self._last_bid if self._is_short
                            else calc_px >= self._last_bid
                        )
                        if crossed:
                            op = "<=" if self._is_short else ">="
                            self._reset_grid_state(
                                f"补仓L{s.level}目标价{calc_px:.2f}{op}bid{self._last_bid:.2f}",
                                now, cooldown=5.0,
                            )
                            break
                    s.target_price = calc_px
                    s.last_attempt_ts = now
                    self._place_entry(s, now)
                    # 记录补仓时间给节流 gate（修复 15:18:58 同秒双 fill bug）
                    self._recent_entries_ts.append(now)

        return None

    # ══════════════════════════════════════════════════════════════════════════
    # 状态摘要
    # ══════════════════════════════════════════════════════════════════════════

    def status_summary(self) -> dict[str, Any]:
        now = time.time()
        held = {s.level: {"fill": s.fill_price, "sz": s.fill_sz}
                for s in self._slots if s.state == _S.HOLDING}
        live = [s.level for s in self._slots if s.state == _S.ENTRY_LIVE]
        # 各 HOLDING 槽位持仓时长（秒），用于发现异常长持仓
        hold_durations = {
            s.level: round(now - s.fill_ts, 1)
            for s in self._slots
            if s.state == _S.HOLDING and s.fill_ts > 0
        }
        _ls_active = self._loss_streak_until > now
        return {
            "regime":        self._regime.current.value,
            "vol_regime":    self._vol.vol_regime,
            "atr_short_bps": round(self._vol.atr_short * 10000, 2),
            "atr_medium_bps": round(self._vol.atr_medium * 10000, 2),
            "atr_baseline_bps": round(self._atr_baseline * 10000, 2),
            "sz_scale_last": self._last_sz_scale,
            "grid_active":   self._grid_active,
            "grid_center":   round(self._grid_center, 2),
            "grid_spacing_bps": round(self._grid_spacing * 10000, 2),
            "active_levels": self._active_levels,
            "slots_live":    live,
            "slots_holding": held,
            "slot_hold_durations_sec": hold_durations,
            "total_held":    self._total_held,
            "vwap":          round(self._vwap, 2),
            "tp_price":      round(self._tp_price, 2),
            "tp_mult":       self._tp_mult,
            "liq_price":     round(self._liq_price(), 2),
            "daily_pnl":     round(self._pnl.realized, 4),
            "profit_protect": self._pnl.profit_protect_mode(),
            "loss_streak_active": _ls_active,
            "loss_streak_until_iso": (
                datetime.fromtimestamp(self._loss_streak_until).strftime('%H:%M:%S')
                if _ls_active else None
            ),
            "funding_rate":  self._funding_rate,
            "book_imb_ema":  round(self._ema_book_imb.value or 0.0, 3),
            "fgi":           self._fear_greed_index,
            "eff_tp_mult":   round(self._last_eff_tp_mult, 3),
            "grid_bias":     self._grid_bias,
            "session":       self._tracker.session_summary(),
        }
