"""
多策略协调器（Strategy Pool）

设计：不改 runner.py（风险大），而是用"active flag"文件协调多个独立 watcher。

架构：
  - grid_pro：主策略（runner.py 跑）—— 震荡市
  - trend_follow_watcher：趋势市
  - mean_reversion_watcher：均值回归（新）
  - funding_arb_watcher：资金费率套利（新）

每 5 分钟评估市场 regime，决定哪些策略激活：
  - RANGE（震荡）: grid=ON, trend=OFF, mean_rev=ON
  - TRENDING_UP/DOWN（趋势）: grid=OFF, trend=ON, mean_rev=OFF
  - VOLATILE（高波动）: 所有 OFF（观察）

激活状态写到 data/.strategy_pool.json，各 watcher 读此文件判断是否工作。
grid_pro（runner.py）读此文件判断要不要开新格（暂停但不平仓）。

用法：
  python -m quant.tools.strategy_pool           # 单次
  python -m quant.tools.strategy_pool --daemon  # 每 5min
"""
from __future__ import annotations

import argparse
import json
import os
import time
import hmac
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/root/okx_eth_bot/.env")
if not os.environ.get("OKX_API_KEY"):
    load_dotenv("/Users/gaofeng/Documents/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")

OUT = PROJ / "data" / ".strategy_pool.json"
CHECK_INTERVAL = 300


def load_active():
    """各 watcher 调用，读当前激活状态。"""
    try:
        return json.loads(OUT.read_text())
    except Exception:
        return {"grid": True, "trend_follow": True, "mean_reversion": False, "funding_arb": False, "regime": "UNKNOWN"}


def evaluate_regime():
    """评估市场 regime。"""
    # 4h delta + ATR
    k = httpx.get(
        "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=15m&limit=20",
        timeout=10,
    ).json()
    candles = k.get("data", [])
    if len(candles) < 16:
        return "UNKNOWN", None

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]

    # 4h delta
    delta_4h = (closes[0] - closes[-1]) / closes[-1] * 100
    # 15m 平均 range (ATR proxy)
    ranges_bps = [(h - l) / c * 10000 for h, l, c in zip(highs, lows, closes)]
    atr_bps = sum(ranges_bps) / len(ranges_bps)

    return _classify(delta_4h, atr_bps), {"delta_4h": delta_4h, "atr_bps": atr_bps}


def _classify(delta_4h, atr_bps):
    if atr_bps > 80:
        return "VOLATILE"
    if delta_4h > 1.5:
        return "TRENDING_UP"
    if delta_4h < -1.5:
        return "TRENDING_DOWN"
    return "RANGE"


def decide_active(regime):
    """根据 regime 返回各策略的激活状态。"""
    rules = {
        "RANGE":          {"grid": True,  "trend_follow": False, "mean_reversion": True,  "funding_arb": True},
        "TRENDING_UP":    {"grid": False, "trend_follow": True,  "mean_reversion": False, "funding_arb": True},
        "TRENDING_DOWN":  {"grid": False, "trend_follow": True,  "mean_reversion": False, "funding_arb": True},
        "VOLATILE":       {"grid": False, "trend_follow": False, "mean_reversion": False, "funding_arb": False},
        "UNKNOWN":        {"grid": True,  "trend_follow": True,  "mean_reversion": False, "funding_arb": True},
    }
    return rules.get(regime, rules["UNKNOWN"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    while True:
        try:
            regime, meta = evaluate_regime()
            active = decide_active(regime)
            active["regime"] = regime
            active["meta"] = meta
            active["ts"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(active, indent=2, ensure_ascii=False))
            on = [k for k, v in active.items() if v is True]
            print(f"[{active['ts']}] regime={regime} meta={meta} active={on}")
        except Exception as e:
            print(f"ERROR: {e}")
        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
