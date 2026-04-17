"""
技术指标库（向量化，基于 pandas/numpy）
=========================================
全部使用 pandas EWM / rolling 实现，避免引入 ta-lib 依赖。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """指数移动平均（adjust=False，与 TradingView 一致）。"""
    return series.ewm(span=period, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder ATR（真实波动范围均值）。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder 平滑：第一个值用 simple mean，之后 EWM alpha=1/period
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.DataFrame:
    """
    Wilder ADX + ±DI。
    返回 DataFrame，列：adx, plus_di, minus_di
    """
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    plus_dm  = high - prev_high
    minus_dm = prev_low - low
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr_val = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    alpha = 1.0 / period
    atr_s    = tr_val.ewm(alpha=alpha, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9))
    adx_s = dx.ewm(alpha=alpha, adjust=False).mean()

    return pd.DataFrame({"adx": adx_s, "plus_di": plus_di, "minus_di": minus_di},
                        index=high.index)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI（Wilder 平滑）。"""
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - 100 / (1 + rs)


def compute_all(
    df: pd.DataFrame,
    ema_fast: int = 9,
    ema_slow: int = 21,
    ema_trend: int = 55,
    atr_period: int = 14,
    adx_period: int = 14,
) -> pd.DataFrame:
    """
    一次性计算所有策略所需指标，追加列到 df 副本。
    输入 df 需要列：open, high, low, close, volume
    """
    df = df.copy()
    df["ema_fast"]  = ema(df["close"], ema_fast)
    df["ema_slow"]  = ema(df["close"], ema_slow)
    df["ema_trend"] = ema(df["close"], ema_trend)
    df["atr"]       = atr(df["high"], df["low"], df["close"], atr_period)

    adx_df = adx(df["high"], df["low"], df["close"], adx_period)
    df["adx"]      = adx_df["adx"]
    df["plus_di"]  = adx_df["plus_di"]
    df["minus_di"] = adx_df["minus_di"]

    # 成交量比率（当前 / 20根均值）——辅助指标，非入场条件
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    return df
