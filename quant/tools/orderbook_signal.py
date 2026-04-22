"""
Order Book 微观结构信号

挖掘的 alpha：
  1. **大单墙**（Wall detection）：某价位挂单量 > 其余价位平均 × 5
     - 下方大买墙 → 支撑（多头信号）
     - 上方大卖墙 → 阻力（空头信号）
  2. **整数关口堆叠**：$2400 / $2500 等整数关口挂单堆积
     - 整数关口上方 = 阻力（市场心理锚点）
  3. **Book Imbalance 原始值**（非 EMA 平滑）：
     - bid_volume / (bid_volume + ask_volume)
     - > 0.6 = 买压主导
  4. **Spread 异常**：
     - spread > 2× 均值 → 流动性差，暂停开仓

每 15s 刷新（OKX books5/20 频率高但 REST 配额充足）。

用法：
  python -m quant.tools.orderbook_signal --daemon
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")

OUT = PROJ / "data" / ".orderbook_signal.json"
CHECK_INTERVAL = 15


def _is_round_price(px, tolerance_bps=5):
    """检查价位是否靠近整数关口（如 2400 / 2450 / 2500）"""
    for step in [100, 50, 25]:
        rounded = round(px / step) * step
        if abs(px - rounded) / px * 10000 < tolerance_bps:
            return rounded
    return None


def evaluate():
    # 拉 books20 深度
    r = httpx.get(
        "https://www.okx.com/api/v5/market/books?instId=ETH-USDT-SWAP&sz=20", timeout=10
    ).json()
    d = r["data"][0]
    bids = [(float(b[0]), float(b[1])) for b in d["bids"]]
    asks = [(float(a[0]), float(a[1])) for a in d["asks"]]

    if not bids or not asks:
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 10000

    # Book imbalance (depth 10)
    bid_vol_10 = sum(b[1] for b in bids[:10])
    ask_vol_10 = sum(a[1] for a in asks[:10])
    book_imb = (bid_vol_10 - ask_vol_10) / (bid_vol_10 + ask_vol_10) if (bid_vol_10 + ask_vol_10) > 0 else 0

    # 大单墙检测
    avg_bid_size = bid_vol_10 / 10
    avg_ask_size = ask_vol_10 / 10
    bid_walls = [(px, sz) for px, sz in bids if sz > avg_bid_size * 5]
    ask_walls = [(px, sz) for px, sz in asks if sz > avg_ask_size * 5]

    # 整数关口
    round_walls_bid = [(w[0], w[1], _is_round_price(w[0])) for w in bid_walls if _is_round_price(w[0])]
    round_walls_ask = [(w[0], w[1], _is_round_price(w[0])) for w in ask_walls if _is_round_price(w[0])]

    # 信号（归一化 [-1, +1]）：
    # book_imb 已接近 [-1, +1]
    # 下方大墙 → 多信号；上方大墙 → 空信号（反向）
    wall_signal = 0.0
    if bid_walls:
        wall_signal += 0.3 * len(bid_walls)
    if ask_walls:
        wall_signal -= 0.3 * len(ask_walls)
    wall_signal = max(-1.0, min(1.0, wall_signal))

    # 整数关口加权
    round_bonus = 0.0
    if round_walls_bid:
        round_bonus += 0.2
    if round_walls_ask:
        round_bonus -= 0.2

    final_signal = max(-1.0, min(1.0, (book_imb + wall_signal + round_bonus) / 3))

    # spread 异常
    spread_alert = spread_bps > 3  # > 3bps 不正常

    return {
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "ts_unix": time.time(),
        "mid": round(mid, 2),
        "spread_bps": round(spread_bps, 2),
        "spread_alert": spread_alert,
        "book_imbalance": round(book_imb, 3),
        "bid_walls_count": len(bid_walls),
        "ask_walls_count": len(ask_walls),
        "round_walls_bid": [(w[0], w[1], w[2]) for w in round_walls_bid],
        "round_walls_ask": [(w[0], w[1], w[2]) for w in round_walls_ask],
        "signal": round(final_signal, 3),
    }


def read_cached():
    if not OUT.exists():
        return None
    try:
        d = json.loads(OUT.read_text())
        if time.time() - d.get("ts_unix", 0) > 60:
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
                print(f"[{d['ts']}] mid={d['mid']} imb={d['book_imbalance']:+.2f} "
                      f"walls bid={d['bid_walls_count']}/ask={d['ask_walls_count']} "
                      f"signal={d['signal']:+.2f}")
        except Exception as e:
            print(f"ERROR: {e}")
        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
