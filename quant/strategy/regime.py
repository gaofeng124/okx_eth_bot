"""
市场状态机（Market Regime Detector）
=====================================

核心设计理念
-------------
量化策略亏损的最常见根因不是参数不对，而是在错误的市场状态下使用了错误的策略。
ETH 永续合约存在三种基本状态：
  - TRENDING_UP：价格持续上涨，适合回撤做多、动量追多
  - RANGING：价格横盘震荡，适合均值回归，不适合趋势策略
  - TRENDING_DOWN：价格持续下跌，多头策略全部停止（长期单向做多必亏）
  - VOLATILE：剧烈波动，所有策略均缩量，等待市场回稳
  - WARMUP：数据不足，不开仓

状态转换逻辑
-------------
使用三层信号融合，避免单一指标的噪声：
  1. 宏观 EMA 偏差（5分钟级别）— 判断方向
  2. 趋势强度（快/慢 EMA 差值）— 判断力度
  3. 近期价格序列（10 个 tick 的方向分布）— 判断持续性

通道许可规则
-------------
每种 Regime 下只允许特定通道入场，其余全部屏蔽：
  TRENDING_UP:   pullback + momentum（顺势，禁 MR）
  RANGING:       meanrev only（震荡中的超卖反弹，禁趋势追多）
  TRENDING_DOWN: 全部禁止（多头专一策略，下跌无机会）
  VOLATILE:      pullback only，且仓位减半（等待回稳）
  WARMUP:        全部禁止

性能指标
---------
每种 Regime 下追踪胜率与平均 PnL，用于验证分类有效性。
"""
from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Any

from quant.logging_config import get_logger

log = get_logger(__name__)


class Regime(str, Enum):
    WARMUP = "warmup"              # 数据不足，不开仓
    TRENDING_UP = "trending_up"    # 上升趋势，做多友好
    RANGING = "ranging"            # 横盘震荡，MR 友好
    TRENDING_DOWN = "trending_down"  # 下降趋势，所有多头停止
    VOLATILE = "volatile"          # 极端波动，缩量观望


# 每种状态允许的通道集合（空集 = 完全停止交易）
REGIME_ALLOWED_CHANNELS: dict[Regime, set[str]] = {
    Regime.WARMUP:         set(),
    Regime.TRENDING_UP:    {"pullback", "momentum"},   # 顺势策略，禁 MR（避免逆势）
    Regime.RANGING:        {"meanrev"},                 # 震荡只做 MR
    Regime.TRENDING_DOWN:  set(),                       # 全停（多头专一模式下无做空）
    Regime.VOLATILE:       {"pullback"},                # 仅最保守的回撤信号，且要求更强条件
}

# 每种状态下的仓位系数（1.0 = 标准仓位）
REGIME_SIZE_FACTOR: dict[Regime, float] = {
    Regime.WARMUP:         0.0,
    Regime.TRENDING_UP:    1.0,
    Regime.RANGING:        0.7,   # MR 胜率不稳定，轻仓
    Regime.TRENDING_DOWN:  0.0,
    Regime.VOLATILE:       0.5,   # 高波动缩仓
}


class RegimeDetector:
    """
    三层融合的市场状态检测器。

    设计为与 ScalpProStrategy 解耦：只读取 feat 字典，返回 Regime 枚举。
    不持有仓位状态，不发出交易信号，职责单一。
    """

    # ── 状态判断阈值 ──────────────────────────────────────────────
    # 宏观偏差（macro_bias = (mid - ema_macro) / ema_macro）
    _MACRO_UP_STRONG  = +0.0015   # 强上涨：价格高于5分钟均线 0.15%
    _MACRO_UP_WEAK    = +0.0003   # 弱上涨：轻微偏多
    _MACRO_DOWN_STOP  = -0.0020   # 下跌警戒：动量/回撤受限
    _MACRO_TICK_UP_MIN = -0.0010  # tick分类中 TRENDING_UP 的最低宏观偏差门槛（介于DOWN_STOP与UP_WEAK之间）
    _MACRO_DOWN_KILL  = -0.0030   # 下跌全停：所有多头禁止

    # 趋势强度（trend_strength = (ema_f - ema_s) / ema_s）
    _TS_STRONG_UP     = +0.00050  # 强上升趋势
    _TS_WEAK_UP       = +0.00030  # 弱上升趋势（已在 scalp_pro 中要求此值）
    _TS_DOWN          = -0.00030  # 下降趋势

    # 相对波动率
    _VOL_HIGH         = 0.0032    # 极端波动阈值（当前 SP_VOL_CEIL = 0.0028）
    _VOL_RANGING      = 0.0008    # 极低波动（低于此值 = 可能横盘）

    # 方向得分：近 N tick 中涨跌 tick 的比例
    _TICK_WINDOW      = 12        # 12 tick ≈ 2.4s（WS 200ms/tick）
    _TREND_TICK_FRAC  = 0.70      # 70% 同向 tick 才算趋势

    # 状态保持最小时长（秒）：防止状态频繁抖动
    # 日志回顾（2026-04-09）：trending_up→ranging 在 8s 内完成，导致 pullback/momentum
    # 信号来不及触发就被禁止。提高至 20s 让有效 Regime 持续足够长。
    # 危险状态（TRENDING_DOWN/VOLATILE）仍可立即切换（不受此限制）。
    _MIN_HOLD_SEC     = 20.0

    def __init__(self, warmup_ticks: int = 200) -> None:
        self._warmup_needed = warmup_ticks
        self._tick_prices: deque[float] = deque(maxlen=self._TICK_WINDOW)
        self._current: Regime = Regime.WARMUP
        self._current_since: float = 0.0
        self._tick_count: int = 0

        # 每种状态下的历史表现追踪（用于验证分类有效性）
        self._regime_stats: dict[Regime, dict] = {
            r: {"trades": 0, "wins": 0, "total_pnl": 0.0}
            for r in Regime
        }

    @property
    def current(self) -> Regime:
        return self._current

    @property
    def allowed_channels(self) -> set[str]:
        return REGIME_ALLOWED_CHANNELS[self._current]

    @property
    def size_factor(self) -> float:
        return REGIME_SIZE_FACTOR[self._current]

    def update(self, feat: dict[str, Any], now: float) -> Regime:
        """
        每个 tick 调用一次，更新内部状态并返回当前 Regime。

        feat 必须包含：
          mid, macro_bias, trend_strength, rel_vol, trend_up, trend_down
        """
        self._tick_count += 1
        mid = feat.get("mid", 0.0)
        if mid > 0:
            self._tick_prices.append(mid)

        # ── 热身期 ──
        if self._tick_count < self._warmup_needed:
            if self._current != Regime.WARMUP:
                self._transition(Regime.WARMUP, now, reason="warmup")
            return self._current

        macro_bias   = feat.get("macro_bias", 0.0)
        ts           = feat.get("trend_strength", 0.0)
        rel_vol      = feat.get("rel_vol", 0.0)
        trend_up     = feat.get("trend_up", False)
        trend_down   = feat.get("trend_down", False)

        # ── 层 1：极端波动 ──（优先级最高）
        if rel_vol >= self._VOL_HIGH:
            candidate = Regime.VOLATILE
        # ── 层 2：宏观方向判断 ──
        elif macro_bias <= self._MACRO_DOWN_KILL:
            # 价格低于5分钟均线 0.3%+ → 下跌全停
            candidate = Regime.TRENDING_DOWN
        elif macro_bias >= self._MACRO_UP_STRONG and ts >= self._TS_STRONG_UP:
            # 宏观明显偏多（>+0.15%）+ 快线强力领先 → 强上升趋势
            # round50: 原 _MACRO_DOWN_STOP(-0.002) 太宽松，价格低于均线0.2%也触发TRENDING_UP
            # 改用 _MACRO_UP_STRONG(+0.0015)：真正上涨才做上升趋势处理
            candidate = Regime.TRENDING_UP
        elif macro_bias >= self._MACRO_UP_WEAK and ts >= self._TS_WEAK_UP:
            # 宏观轻微偏多（>+0.03%）+ 快线领先 → 弱上升趋势
            # round50: 原 _MACRO_DOWN_STOP(-0.002)→_MACRO_UP_WEAK(+0.0003)
            # 消除 [-0.002, +0.0003) 偏空区间的误判 TRENDING_UP
            candidate = Regime.TRENDING_UP
        elif macro_bias < self._MACRO_DOWN_STOP and macro_bias > self._MACRO_DOWN_KILL:
            # 宏观偏空（-0.003 ~ -0.002）→ 下跌警戒，停止交易
            candidate = Regime.TRENDING_DOWN
        elif trend_down and ts <= self._TS_DOWN:
            # 短期趋势向下 → 下跌
            candidate = Regime.TRENDING_DOWN
        elif rel_vol <= self._VOL_RANGING:
            # 极低波动 → 横盘
            candidate = Regime.RANGING
        else:
            # ── 层 3：近期 tick 方向分布 ──
            candidate = self._classify_by_ticks(macro_bias, trend_up, trend_down)

        # ── 状态保持：防止抖动 ──
        if candidate != self._current:
            elapsed = now - self._current_since
            if elapsed < self._MIN_HOLD_SEC:
                # 当前状态保持不足 _MIN_HOLD_SEC，不切换（危险状态可立即切换）
                if candidate not in (Regime.TRENDING_DOWN, Regime.VOLATILE):
                    return self._current
            self._transition(candidate, now, reason=f"mb={macro_bias:.4f} ts={ts:.5f} vol={rel_vol:.5f}")

        return self._current

    def _classify_by_ticks(
        self,
        macro_bias: float,
        trend_up: bool,
        trend_down: bool,
    ) -> Regime:
        """用近期 tick 价格序列辅助判断状态。"""
        prices = list(self._tick_prices)
        if len(prices) < self._TICK_WINDOW // 2:
            return Regime.RANGING  # 数据不足，保守假设横盘

        up_ticks = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i - 1])
        total = len(prices) - 1
        if total <= 0:
            return Regime.RANGING

        up_frac = up_ticks / total

        if up_frac >= self._TREND_TICK_FRAC and macro_bias > self._MACRO_TICK_UP_MIN:
            return Regime.TRENDING_UP
        if up_frac <= (1 - self._TREND_TICK_FRAC):
            return Regime.TRENDING_DOWN
        return Regime.RANGING

    def _transition(self, new: Regime, now: float, reason: str) -> None:
        """执行状态转换，记录日志。"""
        old = self._current
        if old != new:
            log.info(
                "[regime] %s → %s | %s | allowed=%s",
                old.value, new.value, reason,
                ",".join(sorted(REGIME_ALLOWED_CHANNELS[new])) or "NONE",
            )
        self._current = new
        self._current_since = now

    def record_trade(self, regime: Regime, pnl: float) -> None:
        """记录交易结果，用于验证 Regime 分类有效性。"""
        stats = self._regime_stats[regime]
        stats["trades"] += 1
        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1

    def stats_summary(self) -> dict[str, Any]:
        """返回各 Regime 下的交易统计，供日志输出。"""
        out = {}
        for r, s in self._regime_stats.items():
            if s["trades"] == 0:
                continue
            wr = s["wins"] / s["trades"]
            avg_pnl = s["total_pnl"] / s["trades"]
            out[r.value] = {
                "trades": s["trades"],
                "win_rate": round(wr, 3),
                "avg_pnl": round(avg_pnl, 5),
                "total_pnl": round(s["total_pnl"], 5),
            }
        return out
