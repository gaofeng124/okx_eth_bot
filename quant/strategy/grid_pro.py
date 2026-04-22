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
        self._ema_book_imb = StreamingEMA(alpha=0.08)  # L3-001: 盘口不平衡 EMA

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
        self._tp_fill_profits: deque[float] = deque(maxlen=10)  # 近10次TP成交利润（格宽倍数）
        self._emergency_closing: bool = False

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

        # Phase 4 趋势日守卫（主人 2026-04-21 22:15 批准 B 激进版时要求）
        # 每 10min 评估近 4h K 线 delta：若 |delta| > 1.5% → 自动降回 Phase 3
        # 原因：90% 利用率 + 趋势日 = 必爆仓；grid 策略依赖震荡不是单边
        self._last_p4_guard_ts: float = 0.0
        self._p4_trend_guard_enabled: bool = os.getenv("GRID_PHASE4_TREND_GUARD", "0") == "1"

        # REST 客户端
        self._rest = OKXRestClient()

        # 启动对账
        self._boot_reconcile()

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

    def _market_close_all(self, mid: float, reason: str) -> None:
        """市价平仓所有持仓槽位（long=sell，short=buy），记录盈亏。"""
        held = [s for s in self._slots if s.state == _S.HOLDING and s.fill_sz > 0]
        total = sum(s.fill_sz for s in held)
        if total <= 0:
            return
        api_side = self._exit_api_side()
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

        sign = self._pnl_sign()
        for s in held:
            # PnL 方向乘子：long→+1，short→-1（short 的 mid<fill 才盈利）
            raw_pct = (mid - s.fill_price) / s.fill_price if s.fill_price > 0 else 0.0
            pnl_pct = raw_pct * sign
            net = (mid - s.fill_price) * s.fill_sz * self._ct_val * sign
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
        # Cancel any pending entry orders BEFORE clearing state (orphan prevention)
        for s in self._slots:
            if s.state == _S.ENTRY_LIVE and s.entry_order_id:
                if not self._cancel_order(s.entry_order_id):
                    # Cancel failed — order may already be filled; check and handle
                    order = self._query_order(s.entry_order_id)
                    o_state = str(order.get("state", ""))
                    fill_sz = float(order.get("fillSz") or 0.0)
                    if o_state == "filled" and fill_sz > 0:
                        fill_px = float(order.get("avgPx") or order.get("fillPx") or 0)
                        log.warning(
                            "[grid] _reset_grid: L%d 入场单已成交 @%.2f sz=%.1f — 需要手动处理或等待reconcile",
                            s.level, fill_px, fill_sz,
                        )
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
        self._active_levels = n_active
        self._grid_active   = True
        placed = 0
        # 记录开格时间给节流 gate 用（改进 2）
        self._recent_entries_ts.append(time.time())

        # ── 改进 3 (2026-04-22): 规模自适应波动 ──
        # 问题：sz=1.0 notional $240，per_slot_stop $0.8 = 0.33% 容忍
        #      但 ETH ATR 30bps + 冲高回落 50bps 常见 → 每次都击穿
        # 规则：ATR > 35bps 缩 sz；ATR 越高缩越多
        _atr_bps = self._vol.atr_short * 10000
        _sz_scale = 1.0
        if _atr_bps > 70:
            _sz_scale = 0.3
        elif _atr_bps > 50:
            _sz_scale = 0.5
        elif _atr_bps > 35:
            _sz_scale = 0.7
        if _sz_scale < 1.0:
            log.info(
                "[grid][atr-scale] ATR=%.1fbps 偏高，仓位缩 ×%.1f（防高波动击穿止损）",
                _atr_bps, _sz_scale,
            )
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
        tp = self._vwap * (1.0 + tp_sign * self._grid_spacing * _eff_tp_mult)
        self._tp_price = tp
        oid = self._place_tp(self._total_held, tp)
        if oid:
            self._tp_order_id = oid
            self._tp_placed_ts = time.time()

    def _maybe_trail_tp(self, mid: float) -> None:
        """
        TP 追踪：
          long  → 市场上行 mid > tp + _trail_trigger*spacing，TP 上移到 mid - trail_offset*spacing
          short → 市场下行 mid < tp - _trail_trigger*spacing，TP 下移到 mid + trail_offset*spacing

        RANGING 模式（震荡行情价格延伸有限，需快速锁利）：
          trail_offset  = 0.15（更紧，TP 离市价更近）
          _trail_trigger = 0.30（更敏感，价格超出 TP 0.3 格即开始追踪）
          _min_trail_iv  = 20s（节流更短，允许更频繁追踪）

        趋势模式（价格可能持续延伸，给 TP 更多空间）：
          trail_offset  = 0.25（更宽，追更大利润）
          _trail_trigger = 0.40（标准，避免趋势中过早锁定）
          _min_trail_iv  = 30s（_TP_TRAIL_MIN_INTERVAL，避免频繁 API 调用）

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
        # RANGING：步长 0.15 + 触发门槛 0.30；趋势：步长 0.25 + 触发门槛 0.40
        # 两者均通过实盘成交利润自适应调整，形成双维度闭环
        _trail_offset  = self._adaptive_trail_offset(0.15 if _is_ranging else 0.25)
        _trail_trigger = self._adaptive_trail_trigger(0.30 if _is_ranging else 0.40)

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
                        self._tp_order_id = oid
                        self._tp_placed_ts = now
                    self._last_tp_trail_ts = now
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
                        self._tp_order_id = oid
                        self._tp_placed_ts = now   # 重置超时计时器：市场上行时不应过早触发止损
                    self._last_tp_trail_ts = now   # 无论成功与否都更新节流时间戳

    def _adaptive_trail_trigger(self, base_trigger: float) -> float:
        """根据近期TP成交利润（格宽倍数）动态调整追踪触发门槛。

        metric: profit_spacings = abs(fill_px - vwap) / spacing
          < 0.4格均值 → TP 锁利太少（trigger 触发太早/offset 太紧）→ 放宽 trigger +0.10
          > 0.8格均值 → 市场延伸后才成交（RANGING 中易被回撤）→ 收紧 trigger -0.05
          中间范围 → 保持 base_trigger，不干预

        至少需要 5 次成交数据才启用自适应，否则直接返回 base。
        调整幅度有界：[0.20, 0.50]，不超出合理范围。
        """
        if len(self._tp_fill_profits) < 5:
            return base_trigger
        avg = sum(self._tp_fill_profits) / len(self._tp_fill_profits)
        if avg < 0.4:
            adapted = min(base_trigger + 0.10, 0.50)
        elif avg > 0.8:
            adapted = max(base_trigger - 0.05, 0.20)
        else:
            adapted = base_trigger
        if adapted != base_trigger:
            log.debug(
                "[grid] adaptive trigger: base=%.2f → %.2f (avg_profit=%.3f格, n=%d)",
                base_trigger, adapted, avg, len(self._tp_fill_profits),
            )
        return adapted

    def _adaptive_trail_offset(self, base_offset: float) -> float:
        """根据近期TP成交利润（格宽倍数）动态调整追踪步长（trail_offset）。

        metric: profit_spacings = abs(fill_px - vwap) / spacing
          < 0.30格均值 → 利润偏低（offset 太紧，TP 离市价太近）→ 放宽 +0.03
          > 0.80格均值 → 利润充足但延迟锁定 → 收紧 -0.03
          中间范围 → 保持 base_offset，不干预

        至少需要 5 次成交数据才启用自适应，否则直接返回 base。
        调整幅度有界：[0.08, 0.35]，防止极端飘移。
        与 _adaptive_trail_trigger 共用同一信号源（_tp_fill_profits）形成双维度闭环。
        """
        if len(self._tp_fill_profits) < 5:
            return base_offset
        avg = sum(self._tp_fill_profits) / len(self._tp_fill_profits)
        if avg < 0.30:
            adapted = min(base_offset + 0.03, 0.35)
        elif avg > 0.80:
            adapted = max(base_offset - 0.03, 0.08)
        else:
            adapted = base_offset
        if adapted != base_offset:
            log.debug(
                "[grid] adaptive offset: base=%.2f → %.2f (avg_profit=%.3f格, n=%d)",
                base_offset, adapted, avg, len(self._tp_fill_profits),
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
                    calc_px = self._grid_center * (1.0 + dir_sign * self._grid_spacing * (s.level + 1))
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
            # 记录本次 TP 利润（格宽倍数），供 _adaptive_trail_trigger 使用
            if self._grid_spacing > 0 and self._vwap > 0:
                spacing_abs = self._grid_spacing * self._vwap
                profit_spacings = abs(fill_px - self._vwap) / spacing_abs
                self._tp_fill_profits.append(profit_spacings)
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
                "?instId=ETH-USDT-SWAP&bar=15m&limit=16", timeout=5
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
                subprocess.run(
                    ["sed", "-i.tmp",
                     "s/^GRID_LEVELS=.*/GRID_LEVELS=5/",
                     "/root/okx_eth_bot/.env"],
                    check=False,
                )
                subprocess.run(
                    ["rm", "-f", "/root/okx_eth_bot/.env.tmp"],
                    check=False,
                )
                # 移除 phase4 标记，记录降级时间
                subprocess.run(
                    ["rm", "-f", "/root/okx_eth_bot/data/.phase4_applied"],
                    check=False,
                )
                with open("/root/okx_eth_bot/data/.p4_downgraded", "w") as f:
                    f.write(f"降级时间 {time.strftime('%Y-%m-%d %H:%M:%S')} delta_4h={delta_pct:.2f}%")
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
            if est_entry > 0:
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
                if elapsed > 60.0 or unrealized < -1.5:
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
            _TP_AGING_SEC = 600.0 if regime == favorable_trend else 480.0
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
            _SLOW_BLEED_AGING_SEC = 1800.0  # 30 分钟
            _SLOW_BLEED_LOSS_USDT = 0.30
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

        # ── 10d2. 浮亏保护 gate（问题 4 加强，2026-04-22 主人紧急回退）
        # 情景：持仓已经浮亏 > $0.30，但 strategy 想继续开同向新仓（摊平）
        # → 死扛越陷越深（昨日 -$8.28 就是典型 → 4 笔 sz=1.0 全亏）
        # 规则：任一 HOLDING 槽位浮亏 > $0.3 → 本轮拒绝开新格/补仓（等 TP 或止损先处理完）
        if not self._grid_active:
            _has_bleeding = False
            _pnl_sign = self._pnl_sign()
            for s in self._slots:
                if s.state != _S.HOLDING or s.fill_sz <= 0 or s.fill_price <= 0:
                    continue
                slot_upl = (mid - s.fill_price) * s.fill_sz * self._ct_val * _pnl_sign
                if slot_upl < -0.30:
                    _has_bleeding = True
                    break
            if _has_bleeding:
                log.info("[grid][bleed-guard] 持仓浮亏 > $0.3，拒绝开新格（避免摊平）")
                return None

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

        # S8（权重 0.15，2026-04-22 新增）：链上净流入信号（真 alpha —— 散户看不到）
        # 由 quant.tools.onchain_signal 后台进程每 10min 刷新，strategy 读缓存
        try:
            from quant.tools.onchain_signal import read_signal_cached
            _onchain = read_signal_cached()
            if _onchain and "signal" in _onchain:
                # signal 已归一化 [-1, +1]
                _dir_score += float(_onchain["signal"]) * 0.15
        except Exception:
            pass  # 链上信号失败不影响主策略

        # S7（权重 0.20，2026-04-22 新增）：价格位置因子
        # 问题：13:31 / 13:52 两笔 sz=1.0 都在 ETH 刚破新高时买入，1-2min 回落被砸
        # 逻辑：靠近近 1h 高点不利做多（追顶），靠近低点不利做空（追底）
        # 每 5 min 更新 1h 高低缓存（15m × 4 根 = 1h）
        if now - self._price_1h_cache["ts"] > 300:
            try:
                import urllib.request as _ur, json as _json
                with _ur.urlopen(
                    "https://www.okx.com/api/v5/market/candles"
                    "?instId=ETH-USDT-SWAP&bar=15m&limit=4", timeout=5
                ) as r:
                    _c = _json.loads(r.read())["data"]
                    self._price_1h_cache["hi"] = max(float(c[2]) for c in _c)
                    self._price_1h_cache["lo"] = min(float(c[3]) for c in _c)
                    self._price_1h_cache["ts"] = now
            except Exception:
                pass
        _hi_1h = self._price_1h_cache["hi"]
        _lo_1h = self._price_1h_cache["lo"]
        if _hi_1h > 0 and _lo_1h > 0:
            # 距高 / 距低（bps）
            _dist_hi = (_hi_1h - mid) / mid * 10000 if _hi_1h > mid else 0
            _dist_lo = (mid - _lo_1h) / mid * 10000 if mid > _lo_1h else 0
            # 距高 < 10bps（≈0.1%）→ 追顶风险，贡献 -0.5 对 long
            # 距低 < 10bps → 追底风险，贡献 +0.5（反向）对 short
            _pos_signal = 0.0
            if _dist_hi < 10 and _dist_hi > 0:  # 非常接近高点
                _pos_signal = -(1.0 - _dist_hi / 10)  # 距高 0bps → -1，距高 10bps → 0
            elif _dist_lo < 10 and _dist_lo > 0:
                _pos_signal = (1.0 - _dist_lo / 10)   # 距低 0bps → +1
            # 权重 0.20 是最大的单权重，确保追顶追底时此因子能主导
            _dir_score += _pos_signal * 0.20

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
        # 补档比开格宽容些：|score| > 0.30 且反向才阻止
        _fill_ok_dir = not (abs(_dir_score) > 0.30 and my_dir_sign_fill * _dir_score < 0)
        if self._grid_active and market_ok and _fill_ok_dir:
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
                        1.0 + dir_sign * self._grid_spacing * (s.level + 1)
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
            "tp_mult":       self._tp_mult,
            "liq_price":     round(self._liq_price(), 2),
            "daily_pnl":     round(self._pnl.realized, 4),
            "profit_protect": self._pnl.profit_protect_mode(),
            "funding_rate":  self._funding_rate,
            "book_imb_ema":  round(self._ema_book_imb.value or 0.0, 3),
            "session":       self._tracker.session_summary(),
        }
