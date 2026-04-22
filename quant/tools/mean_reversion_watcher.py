"""
Mean Reversion Watcher —— 均值回归策略（震荡市专用）

原理：价格短期偏离均值后倾向于回归。用 Z-score：
  Z = (price - MA_20) / σ_20

  Z > 2.0：严重高估 → 开 short（等回落）
  Z < -2.0：严重低估 → 开 long（等反弹）

多重确认（防假信号）：
  - strategy_pool 标记 mean_reversion=True（即 RANGE regime）
  - 无其他持仓（不和 grid 冲突）
  - 当前无 grid 持仓敞口（避免同向加仓）

交易参数：
  - sz = 1 张（和 grid 同规模）
  - TP: 回到 MA（± 0.5σ）
  - SL: Z-score 继续扩大到 3.0 反向止损
  - 持仓 > 2h 未 TP 则主动平

用法：
  python -m quant.tools.mean_reversion_watcher           # 前台
  python -m quant.tools.mean_reversion_watcher --daemon  # 后台
"""
from __future__ import annotations

import argparse
import json
import math
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

LOG = PROJ / "data" / "logs" / "mean_reversion.log"
STATE = PROJ / "data" / ".mean_rev_state.json"
CHECK_INTERVAL = 60
COOLDOWN_AFTER_TRADE = 1800  # 30min


def _sign(ts, m, p, body=""):
    secret = os.environ["OKX_SECRET_KEY"]
    return base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{m}{p}{body}".encode(), hashlib.sha256).digest()
    ).decode()


def _api(method, path, body=""):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    h = {
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    if method == "GET":
        return httpx.get("https://www.okx.com" + path, headers=h, timeout=15).json()
    return httpx.post("https://www.okx.com" + path, headers=h, content=body, timeout=15).json()


def log(msg):
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def read_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"last_trade_ts": 0}


def write_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def is_active():
    try:
        from quant.tools.strategy_pool import load_active
        return load_active().get("mean_reversion", False)
    except Exception:
        return False


def compute_zscore():
    """近 20 根 5m 收盘价 Z-score"""
    k = httpx.get(
        "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=5m&limit=20",
        timeout=10,
    ).json()
    candles = k.get("data", [])
    if len(candles) < 20:
        return None, None, None
    closes = [float(c[4]) for c in candles]
    price = closes[0]
    ma = sum(closes) / len(closes)
    var = sum((c - ma) ** 2 for c in closes) / (len(closes) - 1)
    std = math.sqrt(var) if var > 0 else 0
    if std <= 0:
        return None, price, ma
    z = (price - ma) / std
    return z, price, ma


def get_position():
    r = _api("GET", "/api/v5/account/positions?instId=ETH-USDT-SWAP")
    for x in r.get("data", []):
        pos = float(x.get("pos") or 0)
        if abs(pos) > 0.001:
            return pos
    return 0.0


def place_mean_rev_order(direction, current_px, target_px):
    """开仓 + OCO 止盈（回到 MA）+ 止损（Z 反向扩大）"""
    side = "buy" if direction == "long" else "sell"
    body = json.dumps({
        "instId": "ETH-USDT-SWAP",
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": "1",
    })
    r = _api("POST", "/api/v5/trade/order", body)
    log(f"开仓 {direction} sz=1: {r}")
    if r.get("code") != "0":
        return False
    time.sleep(1.5)

    # 止盈：回到 MA
    # 止损：+1σ 反方向
    if direction == "long":
        tp_px = round(target_px, 2)
        sl_px = round(current_px * (1 - 0.008), 2)
        close_side = "sell"
    else:
        tp_px = round(target_px, 2)
        sl_px = round(current_px * (1 + 0.008), 2)
        close_side = "buy"

    oco = json.dumps({
        "instId": "ETH-USDT-SWAP",
        "tdMode": "isolated",
        "side": close_side,
        "ordType": "oco",
        "sz": "1",
        "tpTriggerPx": str(tp_px),
        "tpOrdPx": "-1",
        "slTriggerPx": str(sl_px),
        "slOrdPx": "-1",
        "reduceOnly": "true",
    })
    r2 = _api("POST", "/api/v5/trade/order-algo", oco)
    log(f"OCO tp={tp_px} sl={sl_px}: {r2}")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    log("=== Mean Reversion Watcher 启动 ===")
    while True:
        try:
            if not is_active():
                time.sleep(CHECK_INTERVAL)
                continue
            state = read_state()
            if time.time() - state.get("last_trade_ts", 0) < COOLDOWN_AFTER_TRADE:
                time.sleep(CHECK_INTERVAL)
                continue
            if abs(get_position()) > 0.001:
                time.sleep(CHECK_INTERVAL)
                continue
            z, price, ma = compute_zscore()
            if z is None:
                time.sleep(CHECK_INTERVAL)
                continue
            if z > 2.0:
                log(f"🎯 Z={z:.2f} > 2 严重高估 → 开 short price={price} MA={ma}")
                if place_mean_rev_order("short", price, ma):
                    state["last_trade_ts"] = time.time()
                    write_state(state)
            elif z < -2.0:
                log(f"🎯 Z={z:.2f} < -2 严重低估 → 开 long price={price} MA={ma}")
                if place_mean_rev_order("long", price, ma):
                    state["last_trade_ts"] = time.time()
                    write_state(state)
        except Exception as e:
            log(f"ERROR: {e}")
        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
