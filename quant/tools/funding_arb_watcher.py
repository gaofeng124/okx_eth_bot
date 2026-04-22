"""
Funding Rate Arbitrage Watcher

原理：永续合约每 8 小时结算 funding rate。
  - funding > 0：多头付给空头 → 做空有利
  - funding < 0：空头付给多头 → 做多有利

极端 funding（>0.05%/8h 或 <-0.05%/8h）历史上常伴随快速回归。

简化实现（单交易所）：
  - 监控 OKX ETH-USDT-SWAP funding rate
  - 距下次结算 < 1h 时：
    * funding > 0.05% → 强空信号（权重 0.30 写入 .funding_arb_signal.json）
    * funding < -0.05% → 强多信号
  - strategy 读缓存作为辅助信号

Cross-exchange arb 需额外交易所 API，留待下次。

用法：
  python -m quant.tools.funding_arb_watcher --daemon
"""
from __future__ import annotations

import argparse
import json
import os
import time
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

OUT = PROJ / "data" / ".funding_arb_signal.json"
CHECK_INTERVAL = 300


def evaluate():
    r = httpx.get(
        "https://www.okx.com/api/v5/public/funding-rate?instId=ETH-USDT-SWAP", timeout=10
    ).json()
    d = r["data"][0]
    funding = float(d.get("fundingRate") or 0)
    next_ms = int(d.get("nextFundingTime") or 0)
    time_to_settle_min = (next_ms - int(time.time() * 1000)) / 60000

    # 归一化信号：funding 0.05% = full signal（反向，多头贵→做空）
    # 即 funding > 0 → signal < 0；funding < 0 → signal > 0
    max_fr = 0.0005  # 0.05% = extreme
    signal = -max(-1.0, min(1.0, funding / max_fr))

    # 仅当 funding 显著时输出强信号；接近结算时权重加倍
    strength = abs(signal)
    time_weight = 1.0
    if time_to_settle_min < 60:
        time_weight = 1.5  # 1h 内结算的信号更值得信

    effective = signal * time_weight

    return {
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ts_unix": time.time(),
        "funding_rate_pct": round(funding * 100, 5),
        "minutes_to_settle": round(time_to_settle_min, 1),
        "raw_signal": round(signal, 3),
        "time_weighted_signal": round(effective, 3),
        "interpretation": (
            "极空" if effective < -0.7 else
            "偏空" if effective < -0.2 else
            "中性" if abs(effective) <= 0.2 else
            "偏多" if effective < 0.7 else
            "极多"
        ),
    }


def read_cached():
    if not OUT.exists():
        return None
    try:
        d = json.loads(OUT.read_text())
        if time.time() - d.get("ts_unix", 0) > 900:  # 15min 过期
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
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(d, indent=2, ensure_ascii=False))
            print(f"[{d['ts']}] funding={d['funding_rate_pct']:+.4f}% "
                  f"→结算 {d['minutes_to_settle']:.0f}min signal={d['time_weighted_signal']:+.2f} ({d['interpretation']})")
        except Exception as e:
            print(f"ERROR: {e}")
        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
