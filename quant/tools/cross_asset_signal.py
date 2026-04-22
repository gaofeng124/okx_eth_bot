"""
Cross-Asset Signal —— 跨资产联动信号

BTC 是 crypto 市场的 beta，ETH 相关性约 0.85。
当 BTC 和 ETH 走势背离时，常是 ETH 补涨/补跌的信号。

信号逻辑：
  btc_delta_1h - eth_delta_1h 的差值：
    - +0.5%+ = BTC 强 ETH 弱 → ETH 有补涨空间（多信号）
    - -0.5%+ = BTC 弱 ETH 强 → ETH 回调风险（空信号）

  绝对 BTC 趋势：
    - BTC 1h 涨 > +1% → crypto 整体多头强
    - BTC 1h 跌 > -1% → crypto 整体空头强

  相关性健康度：
    - 近 2h 5min bars 相关系数 < 0.5 → 背离市场，此信号可信度下降

用法：
  python -m quant.tools.cross_asset_signal --daemon
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")

OUT = PROJ / "data" / ".cross_asset_signal.json"
CHECK_INTERVAL = 300


def _get_candles(inst_id, bar="5m", limit=24):
    r = httpx.get(
        f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}",
        timeout=10,
    ).json()
    return [(int(c[0]), float(c[4])) for c in r.get("data", [])]


def _corr(a, b):
    if len(a) < 5 or len(a) != len(b):
        return None
    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    num = sum((ai - mean_a) * (bi - mean_b) for ai, bi in zip(a, b))
    den_a = math.sqrt(sum((ai - mean_a) ** 2 for ai in a))
    den_b = math.sqrt(sum((bi - mean_b) ** 2 for bi in b))
    if den_a == 0 or den_b == 0:
        return None
    return num / (den_a * den_b)


def evaluate():
    eth = _get_candles("ETH-USDT-SWAP", "5m", 24)
    btc = _get_candles("BTC-USDT-SWAP", "5m", 24)
    if len(eth) < 24 or len(btc) < 24:
        return None

    # 对齐时间戳（OKX 数据倒序，最新在前）
    eth_sorted = sorted(eth)
    btc_sorted = sorted(btc)
    # 截取共同区间
    eth_prices = [p for _, p in eth_sorted]
    btc_prices = [p for _, p in btc_sorted]

    # 1h delta（前 12 根到最新 = 1h）
    # 因为是 5m×24=2h，前 12 根 ≈ 1h
    eth_1h_ago = eth_prices[-12]
    btc_1h_ago = btc_prices[-12]
    eth_now = eth_prices[-1]
    btc_now = btc_prices[-1]
    eth_delta_1h = (eth_now - eth_1h_ago) / eth_1h_ago * 100
    btc_delta_1h = (btc_now - btc_1h_ago) / btc_1h_ago * 100
    divergence = btc_delta_1h - eth_delta_1h

    # 相关性（近 24 根 return）
    eth_returns = [(eth_prices[i] - eth_prices[i-1]) / eth_prices[i-1] for i in range(1, len(eth_prices))]
    btc_returns = [(btc_prices[i] - btc_prices[i-1]) / btc_prices[i-1] for i in range(1, len(btc_prices))]
    corr = _corr(eth_returns, btc_returns)

    # 信号构造：
    # divergence +0.5% → +1 (ETH 补涨)；-0.5% → -1
    # + BTC 绝对趋势修正：BTC 大涨强化 long，BTC 大跌强化 short
    div_signal = max(-1.0, min(1.0, divergence / 0.5))
    btc_trend_signal = max(-1.0, min(1.0, btc_delta_1h / 1.0))
    # 相关性低时减少权重
    reliability = abs(corr) if corr is not None else 0.5

    combined = (div_signal * 0.4 + btc_trend_signal * 0.6) * reliability

    return {
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ts_unix": time.time(),
        "eth_price": round(eth_now, 2),
        "btc_price": round(btc_now, 2),
        "eth_delta_1h_pct": round(eth_delta_1h, 3),
        "btc_delta_1h_pct": round(btc_delta_1h, 3),
        "divergence_pct": round(divergence, 3),
        "correlation_2h": round(corr, 3) if corr else None,
        "div_signal": round(div_signal, 3),
        "btc_trend_signal": round(btc_trend_signal, 3),
        "signal": round(combined, 3),
        "interpretation": (
            "BTC拖多" if combined > 0.4 else
            "BTC偏多" if combined > 0.1 else
            "中性" if abs(combined) <= 0.1 else
            "BTC偏空" if combined > -0.4 else
            "BTC拖空"
        ),
    }


def read_cached():
    if not OUT.exists():
        return None
    try:
        d = json.loads(OUT.read_text())
        if time.time() - d.get("ts_unix", 0) > 900:
            return None
        return d
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    while True:
        try:
            d = evaluate()
            if d:
                OUT.parent.mkdir(parents=True, exist_ok=True)
                OUT.write_text(json.dumps(d, indent=2, ensure_ascii=False))
                print(f"[{d['ts']}] BTC={d['btc_delta_1h_pct']:+.2f}% ETH={d['eth_delta_1h_pct']:+.2f}% "
                      f"div={d['divergence_pct']:+.2f}% corr={d['correlation_2h']} "
                      f"signal={d['signal']:+.2f} ({d['interpretation']})")
        except Exception as e:
            print(f"ERROR: {e}")
        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
