"""
执行质量采集（Fill Quality Logger）

目标：为明天的白皮书收集真实执行数据。

每 60s 拉 fills-history 对比 orders-history，产出每笔：
  - intended_px（订单价）
  - fill_px（成交价）
  - slippage_bps = (fill_px - intended_px) / intended_px × 10000
  - time_placed → time_filled
  - order_type（limit/market/post_only）
  - reject_count（post_only 被 reject 的次数）

数据写 data/fill_quality.jsonl，明天统计。

用法：
  python -m quant.tools.fill_quality_logger --daemon
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

LOG = PROJ / "data" / "fill_quality.jsonl"
CURSOR = PROJ / "data" / ".fill_quality_cursor"
CHECK_INTERVAL = 60


def _sign(ts, m, p):
    secret = os.environ["OKX_SECRET_KEY"]
    return base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{m}{p}".encode(), hashlib.sha256).digest()
    ).decode()


def _okx(path):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    h = {
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": _sign(ts, "GET", path),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    return httpx.get("https://www.okx.com" + path, headers=h, timeout=15).json()


def get_orders_history_2d():
    """拉近 2 天订单历史（含 reject 的）。"""
    return _okx("/api/v5/trade/orders-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100")


def get_fills_recent():
    return _okx("/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=50")


def read_cursor():
    try:
        return CURSOR.read_text().strip()
    except Exception:
        return ""


def write_cursor(c):
    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    CURSOR.write_text(c)


def collect():
    orders = get_orders_history_2d().get("data", [])
    fills = get_fills_recent().get("data", [])

    # 订单 lookup：ordId → 订单详情
    order_by_id = {o.get("ordId"): o for o in orders}

    # 按 billId 升序处理
    fills.sort(key=lambda x: x.get("billId", ""))
    last_cursor = read_cursor()

    new_records = []
    new_cursor = last_cursor

    for f in fills:
        bid = f.get("billId", "")
        if last_cursor and bid <= last_cursor:
            continue

        ord_id = f.get("ordId", "")
        od = order_by_id.get(ord_id, {})

        intended_px = float(od.get("px") or 0)
        fill_px = float(f.get("fillPx") or 0)
        ord_type = od.get("ordType", "unknown")
        t_placed = int(od.get("cTime", 0))
        t_filled = int(f.get("ts", 0))
        latency_ms = t_filled - t_placed if t_placed and t_filled else None

        # slippage（仅限价单有意义）
        slippage_bps = None
        if intended_px > 0 and fill_px > 0:
            slippage_bps = (fill_px - intended_px) / intended_px * 10000
            if f.get("side") == "buy":
                slippage_bps = slippage_bps  # buy 越高越差
            else:
                slippage_bps = -slippage_bps  # sell 越低越差

        rec = {
            "ts_ms": t_filled,
            "ts_cst": datetime.fromtimestamp(t_filled / 1000, CST).strftime("%Y-%m-%d %H:%M:%S") if t_filled else None,
            "ord_id": ord_id,
            "side": f.get("side"),
            "ord_type": ord_type,
            "intended_px": intended_px if intended_px > 0 else None,
            "fill_px": fill_px,
            "fill_sz": float(f.get("fillSz") or 0),
            "fee": float(f.get("fee") or 0),
            "fill_pnl": float(f.get("fillPnl") or 0),
            "slippage_bps": round(slippage_bps, 2) if slippage_bps is not None else None,
            "latency_ms": latency_ms,
        }
        new_records.append(rec)
        if bid > new_cursor:
            new_cursor = bid

    # 写 JSONL
    if new_records:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            for r in new_records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        write_cursor(new_cursor)

    return len(new_records)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    while True:
        try:
            n = collect()
            if n:
                print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 新记录 {n} 笔")
        except Exception as e:
            print(f"ERROR: {e}")
        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
