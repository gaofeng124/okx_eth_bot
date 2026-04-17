"""Merged analysis package."""
from __future__ import annotations


# ----- candle_features.py -----

"""
基于 REST 拉取的 OHLCV（多根 K）计算增强特征，供策略写入 features_json / 事后分析。

与 tick 内 StreamingEMA/RollingWindow 互补：这里刻画**更大窗口**的波动与短期趋势。
"""

import statistics
from typing import Any


def _parse_candles_chrono(rows: list[list[str]]) -> tuple[list[float], list[float], list[float], list[float]]:
    """OKX 返回最新在前 → 转为时间正序 [旧→新]。"""
    ohlc: list[tuple[float, float, float, float]] = []
    for row in rows:
        if len(row) < 5:
            continue
        o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        ohlc.append((o, h, l, c))
    ohlc.reverse()
    if not ohlc:
        return [], [], [], []
    os, hs, ls, cs = zip(*ohlc)
    return list(os), list(hs), list(ls), list(cs)


def compute_candle_features(rows: list[list[str]], *, max_ret_window: int = 60) -> dict[str, Any]:
    """
    从 OKX candles 原始行计算特征。

    - realized_vol: 最近窗口内简单收益率的样本标准差
    - hl_range_mean_pct: 每根 (high-low)/close 的均值，刻画日内振幅
    - ret_sum_short: 最近若干根累计收益（close 变化）
    - close_slope_norm: 对 close 做简单线性回归，斜率按价格尺度归一
    """
    _, hs, ls, cs = _parse_candles_chrono(rows)
    n = len(cs)
    if n < 3:
        return {"bars": n, "insufficient": True}

    rets: list[float] = []
    for i in range(1, n):
        if cs[i - 1] != 0:
            rets.append(cs[i] / cs[i - 1] - 1.0)
    w = min(max_ret_window, len(rets))
    tail_rets = rets[-w:] if w else rets
    rv = float(statistics.pstdev(tail_rets)) if len(tail_rets) >= 2 else 0.0

    hl_pct: list[float] = []
    for i in range(n):
        den = cs[i] if cs[i] else 1e-12
        hl_pct.append((hs[i] - ls[i]) / den)

    short = min(12, n)
    r_short = (cs[-1] / cs[-short] - 1.0) if cs[-short] != 0 else 0.0

    k = min(30, n)
    y = cs[-k:]
    m = len(y)
    mx = (m - 1) / 2.0
    my = sum(y) / m
    num = sum((i - mx) * (y[i] - my) for i in range(m))
    den = sum((i - mx) ** 2 for i in range(m)) or 1e-12
    slope = num / den
    mid = y[-1] if y[-1] else 1e-12
    slope_norm = slope / mid

    return {
        "bars": n,
        "realized_vol": round(rv, 8),
        "hl_range_mean_pct": round(float(statistics.mean(hl_pct[-min(20, n) :])), 8),
        "ret_sum_short": round(r_short, 8),
        "close_slope_norm": round(slope_norm, 10),
        "last_close": round(cs[-1], 8),
    }

# ----- indicators.py -----

"""
流式指标：适用于 WS tick 级更新，避免全量历史重算。
"""

import math
from collections import deque


class StreamingEMA:
    def __init__(self, alpha: float) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0,1]")
        self._alpha = alpha
        self._v: float | None = None

    @property
    def value(self) -> float | None:
        return self._v

    def update(self, x: float) -> float:
        if self._v is None:
            self._v = x
        else:
            self._v = self._alpha * x + (1.0 - self._alpha) * self._v
        return self._v


class RollingWindow:
    """固定长度窗口上的均值与样本标准差（用于 Z-score / 波动刻画）。"""

    def __init__(self, maxlen: int) -> None:
        if maxlen < 2:
            raise ValueError("maxlen must be >= 2")
        self._dq: deque[float] = deque(maxlen=maxlen)

    def push(self, x: float) -> None:
        self._dq.append(x)

    def __len__(self) -> int:
        return len(self._dq)

    @property
    def capacity(self) -> int:
        return int(self._dq.maxlen or 0)

    def mean_std(self) -> tuple[float, float] | None:
        n = len(self._dq)
        if n < 2:
            return None
        xs = self._dq
        s = sum(xs)
        m = s / n
        if n == 1:
            return m, 0.0
        var = sum((xi - m) ** 2 for xi in xs) / (n - 1)
        return m, math.sqrt(var)

# ----- market_context.py -----

"""组装「REST 快照」：ticker 价格 + K 线增强特征，供策略与审计。"""

from typing import Any

from quant.exchange import fetch_ticker_and_candles
from quant.settings import OKX_REST_URL, REST_HTTP_TIMEOUT_SEC, REST_ORDER_BOOK_SZ


def parse_ticker_prices(t: dict[str, Any]) -> tuple[float, float, float] | None:
    try:
        last = float(t["last"])
        bid = float(t["bidPx"])
        ask = float(t["askPx"])
        return last, bid, ask
    except (KeyError, TypeError, ValueError):
        return None


def fetch_rest_snapshot(
    inst_id: str,
    *,
    bar: str,
    limit: int,
    base_url: str | None = None,
    http_timeout_sec: float | None = None,
) -> tuple[tuple[float, float, float], dict[str, Any]]:
    """
    同步拉取：ticker + candles → (last,bid,ask) 与可 JSON 化的 market_context。

    供 asyncio.to_thread 在轮询循环中调用。
    """
    bu = base_url or OKX_REST_URL
    tout = http_timeout_sec if http_timeout_sec is not None else REST_HTTP_TIMEOUT_SEC
    t, candles, books = fetch_ticker_and_candles(
        inst_id,
        bar=bar,
        limit=limit,
        base_url=bu,
        timeout=tout,
        book_sz=REST_ORDER_BOOK_SZ,
    )
    p = parse_ticker_prices(t)
    if p is None:
        raise RuntimeError("ticker 行缺少 last/bidPx/askPx")
    feats = compute_candle_features(candles)
    ctx: dict[str, Any] = {
        "source": "rest",
        "instId": inst_id,
        "bar": bar,
        "candle_features": feats,
        "ticker_ts": t.get("ts"),
    }
    if books:
        ctx["order_book"] = books
    for k in ("open24h", "high24h", "low24h", "vol24h", "sodUtc8"):
        if k in t:
            ctx[k] = t[k]
    return p, ctx

# ----- quote_health.py -----

"""
行情健康度：报价年龄、点差、盘口深度摘要（A/H — 数据可信与特征完整性）。
"""

import math
import time
from typing import Any


def quote_age_ms_from_ticker(ticker_ts: Any, recv_wall_ms: int | None = None) -> float | None:
    """
    交易所 ticker.ts（毫秒）相对本地的年龄；无 ts 时返回 None。
    recv_wall_ms 默认用当前墙钟毫秒。
    """
    if ticker_ts is None:
        return None
    try:
        ts = int(float(ticker_ts))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    now = int(time.time() * 1000) if recv_wall_ms is None else int(recv_wall_ms)
    return max(0.0, float(now - ts))


def spread_bps(bid: float, ask: float, mid: float) -> float:
    if not math.isfinite(bid) or not math.isfinite(ask) or mid <= 0:
        return float("nan")
    return (ask - bid) / mid * 10000.0


def book_depth_summary(order_book: dict[str, Any] | None, *, levels: int = 5) -> dict[str, Any] | None:
    """顶层买卖量与不平衡（用于冲击与流动性粗判）。"""
    if not isinstance(order_book, dict):
        return None
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not isinstance(bids, list) or not isinstance(asks, list):
        return None
    bsum = 0.0
    asum = 0.0
    for x in bids[:levels]:
        if isinstance(x, (list, tuple)) and len(x) >= 2:
            try:
                bsum += float(x[1])
            except (TypeError, ValueError):
                pass
    for x in asks[:levels]:
        if isinstance(x, (list, tuple)) and len(x) >= 2:
            try:
                asum += float(x[1])
            except (TypeError, ValueError):
                pass
    den = bsum + asum
    imb = (bsum - asum) / den if den > 1e-18 else 0.0
    return {
        "bid_sz_top5": round(bsum, 6),
        "ask_sz_top5": round(asum, 6),
        "depth_imbalance": round(imb, 6),
    }


def enrich_quote_context(
    *,
    bid: float,
    ask: float,
    mid: float,
    ticker_ts: Any,
    order_book: dict[str, Any] | None = None,
    candle_snapshot_wall_ts: float | None = None,
) -> dict[str, Any]:
    """合并进 market_context，供策略门控与审计。"""
    recv_ms = int(time.time() * 1000)
    qa = quote_age_ms_from_ticker(ticker_ts, recv_ms)
    candle_age_ms = 0.0
    if candle_snapshot_wall_ts is not None:
        try:
            candle_age_ms = max(0.0, time.time() - float(candle_snapshot_wall_ts)) * 1000.0
        except (TypeError, ValueError):
            candle_age_ms = 0.0
    # 无 ticker.ts 时用 K 线快照墙钟年龄兜底（REST 慢路径常见）
    if qa is not None:
        eff_age = max(qa, candle_age_ms)
        src = "ticker"
    elif candle_age_ms > 0:
        eff_age = candle_age_ms
        src = "candle_wall"
    else:
        eff_age = None
        src = None
    out: dict[str, Any] = {
        "snapshot_recv_ms": recv_ms,
        "quote_age_ms": eff_age,
        "spread_bps": round(spread_bps(bid, ask, mid), 4),
    }
    if src:
        out["quote_age_source"] = src
    if qa is not None:
        out["quote_age_ticker_ms"] = qa
    if candle_snapshot_wall_ts is not None:
        try:
            out["candle_snapshot_age_sec"] = round(max(0.0, time.time() - float(candle_snapshot_wall_ts)), 3)
        except (TypeError, ValueError):
            pass
    bd = book_depth_summary(order_book)
    if bd:
        out["book_depth"] = bd
    return out

# ----- regime_filter.py -----

"""
轻量级市场状态：最近 60 根 1m K + ADX(14)。每 5 分钟重算一次（由 refresh 内节流）。
"""

import time
from typing import Any, Literal

from quant.exchange import get_candles
from quant.settings import OKX_REST_URL, REST_HTTP_TIMEOUT_SEC

# ADX < 12 极度震荡（停手）；12–18 震荡（缩仓+抬阈值）；18–25 过渡；>25 趋势
ADX_HARD_RANGING_MAX = 12.0
ADX_RANGING_MAX = 18.0
ADX_NEUTRAL_MAX = 25.0
REGIME_CANDLE_BAR = "1m"
REGIME_CANDLE_LIMIT = 60
REGIME_REFRESH_SEC = 300.0


def _parse_ohlc_chrono(rows: list[list[str]]) -> tuple[list[float], list[float], list[float]]:
    """OKX 最新在前 → 时间正序 [旧→新]。"""
    hs: list[float] = []
    ls: list[float] = []
    cs: list[float] = []
    for row in rows:
        if len(row) < 5:
            continue
        hs.append(float(row[2]))
        ls.append(float(row[3]))
        cs.append(float(row[4]))
    hs.reverse()
    ls.reverse()
    cs.reverse()
    return hs, ls, cs


def compute_adx14(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """
    Wilder ADX(period)。至少需要约 period*3 根 K 才有稳定末值。
    """
    n = len(closes)
    if n < period * 3 or period < 2:
        return None
    tr: list[float] = [0.0] * n
    p_dm: list[float] = [0.0] * n
    m_dm: list[float] = [0.0] * n
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr[i] = max(h - lows[i], abs(h - pc), abs(lows[i] - pc))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        p_dm[i] = up if up > dn and up > 0 else 0.0
        m_dm[i] = dn if dn > up and dn > 0 else 0.0

    def _wilder_smooth(src: list[float], p: int) -> list[float]:
        """Wilder / RMA: 首项为前 p 项和；之后 S[i] = S[i-1] - S[i-1]/p + src[i]/p。"""
        out = [0.0] * n
        first = sum(src[1 : p + 1]) / float(p)
        out[p] = first
        for i in range(p + 1, n):
            out[i] = out[i - 1] - out[i - 1] / float(p) + src[i] / float(p)
        return out

    tr_s = _wilder_smooth(tr, period)
    p_s = _wilder_smooth(p_dm, period)
    m_s = _wilder_smooth(m_dm, period)
    dx: list[float] = [0.0] * n
    for i in range(period, n):
        atr = tr_s[i]
        if atr <= 1e-18:
            continue
        pdi = 100.0 * (p_s[i] / atr)
        mdi = 100.0 * (m_s[i] / atr)
        den = pdi + mdi
        if den <= 1e-18:
            dx[i] = 0.0
        else:
            dx[i] = 100.0 * abs(pdi - mdi) / den
    dx_s = _wilder_smooth(dx, period)
    adx = dx_s[-1]
    if not (adx == adx) or adx < 0:
        return None
    return float(adx)


def classify_regime(adx: float | None, prev: str) -> str:
    if adx is None:
        return prev if prev in ("trending", "ranging", "ranging_hard", "neutral", "warming_up") else "warming_up"
    if adx < ADX_HARD_RANGING_MAX:
        return "ranging_hard"
    if adx < ADX_RANGING_MAX:
        return "ranging"
    if adx <= ADX_NEUTRAL_MAX:
        return "neutral"
    return "trending"


def apply_regime_to_lev5_runtime(lev5_runtime: dict[str, Any], *, regime: str) -> None:
    lev5_runtime["regime"] = regime
    if regime == "warming_up":
        lev5_runtime["regime_z_need_mul"] = 1.0
        lev5_runtime["regime_open_size_mul"] = 1.0
        lev5_runtime["regime_disable_continuation"] = False
        lev5_runtime["regime_disable_vol_break"] = False
        lev5_runtime["regime_block_entries"] = False
    elif regime == "ranging_hard":
        lev5_runtime["regime_z_need_mul"] = 1.0
        lev5_runtime["regime_open_size_mul"] = 1.0
        lev5_runtime["regime_disable_continuation"] = False
        lev5_runtime["regime_disable_vol_break"] = False
        lev5_runtime["regime_block_entries"] = True
    elif regime == "ranging":
        lev5_runtime["regime_z_need_mul"] = 1.2
        lev5_runtime["regime_open_size_mul"] = 0.5
        lev5_runtime["regime_disable_continuation"] = False
        lev5_runtime["regime_disable_vol_break"] = False
        lev5_runtime["regime_block_entries"] = False
    elif regime == "neutral":
        lev5_runtime["regime_z_need_mul"] = 1.3
        lev5_runtime["regime_open_size_mul"] = 0.6
        lev5_runtime["regime_disable_continuation"] = False
        lev5_runtime["regime_disable_vol_break"] = False
        lev5_runtime["regime_block_entries"] = False
    else:
        lev5_runtime["regime_z_need_mul"] = 1.0
        lev5_runtime["regime_open_size_mul"] = 1.0
        lev5_runtime["regime_disable_continuation"] = False
        lev5_runtime["regime_disable_vol_break"] = False
        lev5_runtime["regime_block_entries"] = False


def refresh_lev5_regime(lev5_runtime: dict[str, Any], inst_id: str) -> None:
    """
    拉取最近 60 根 1m K，计算 ADX(14)；同一进程内最多每 REGIME_REFRESH_SEC 秒拉一次 REST。
    """
    now_m = time.monotonic()
    last = float(lev5_runtime.get("_regime_refresh_monotonic") or 0.0)
    if (now_m - last) < REGIME_REFRESH_SEC:
        return

    prev = str(lev5_runtime.get("regime") or "warming_up")
    if prev not in ("trending", "ranging", "neutral", "warming_up"):
        prev = "warming_up"
    try:
        rows = get_candles(
            inst_id,
            bar=REGIME_CANDLE_BAR,
            limit=REGIME_CANDLE_LIMIT,
            base_url=OKX_REST_URL,
            timeout=REST_HTTP_TIMEOUT_SEC,
        )
    except Exception:
        lev5_runtime["_regime_refresh_monotonic"] = now_m
        lev5_runtime["regime_adx"] = lev5_runtime.get("regime_adx")
        apply_regime_to_lev5_runtime(lev5_runtime, regime=prev)
        return
    if not rows or len(rows) < 30:
        lev5_runtime["_regime_refresh_monotonic"] = now_m
        lev5_runtime["regime_adx"] = None
        apply_regime_to_lev5_runtime(lev5_runtime, regime="warming_up")
        return
    hs, ls, cs = _parse_ohlc_chrono(rows)
    if len(cs) < 30:
        lev5_runtime["_regime_refresh_monotonic"] = now_m
        lev5_runtime["regime_adx"] = None
        apply_regime_to_lev5_runtime(lev5_runtime, regime="warming_up")
        return
    adx = compute_adx14(hs, ls, cs, period=14)
    lev5_runtime["regime_adx"] = round(float(adx), 4) if adx is not None else None
    regime = classify_regime(adx, prev)
    lev5_runtime["_regime_refresh_monotonic"] = now_m
    apply_regime_to_lev5_runtime(lev5_runtime, regime=regime)


# --- 原 regime.py：K 线特征门控（pro_mr 用；合并以减少文件）---
Side = Literal["buy", "sell"]


def candle_trend_gate(
    market_context: dict[str, Any] | None,
    *,
    side: Side,
    slope_threshold: float,
) -> tuple[bool, str]:
    """
    返回 (允许下单, 说明)。
    - 买：若近期 close 斜率过负（急跌），暂缓「抄底」信号。
    - 卖：若斜率过正（急涨），暂缓「摸顶」信号。
    """
    if not market_context:
        return True, ""
    cf = market_context.get("candle_features")
    if not isinstance(cf, dict):
        return True, ""
    if cf.get("insufficient"):
        return True, ""
    slope = cf.get("close_slope_norm")
    if slope is None:
        return True, ""
    try:
        s = float(slope)
    except (TypeError, ValueError):
        return True, ""
    t = float(slope_threshold)
    if side == "buy" and s < -t:
        return (
            False,
            f"K线 close_slope_norm={s:.6f} < -{t}（短期下行偏强，均值回归买暂缓）",
        )
    if side == "sell" and s > t:
        return (
            False,
            f"K线 close_slope_norm={s:.6f} > +{t}（短期上行偏强，均值回归卖暂缓）",
        )
    return True, ""
