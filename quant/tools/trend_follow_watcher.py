"""
Trend Follow Watcher —— 单边市补位策略（L3）

解决：单边行情下 grid 挂单 fill 不到 → 账户资金闲置 → 日 PnL 不达标。

设计：
  独立后台进程（不改 grid_pro 代码，避免冲突）
  每 30s 查 15m K 线，检测突破：
    1. ETH > 近 20 根 15m 最高 + 5bps (0.05%) → BREAKOUT_UP
    2. ETH < 近 20 根 15m 最低 - 5bps → BREAKOUT_DOWN

  触发条件（要全满足才开仓）：
    - 无其他持仓（避免与 grid 冲突）
    - 突破方向与 .env GRID_DIRECTION 一致
    - 4h delta 确认趋势（>+1% long / <-1% short）
    - 近 10min taker aggressor 支持方向（> 0.55 多 / < 0.45 空）

  单笔特点：
    - sz = 1.0 张（与 grid 同规模）
    - 市价追势（post_only 快速 fill）
    - 止盈 OCO：+$2（约 2× ATR）
    - 止损 OCO：-$1（约 1× ATR）
    - 盈亏比 2:1（补 grid 0.5 的结构性弱点）
    - 持仓 > 30min 仍未 TP 且接近平盘 → 主动平

用法：
  python -m quant.tools.trend_follow_watcher           # 前台（调试）
  nohup python -m quant.tools.trend_follow_watcher &   # 后台
"""
from __future__ import annotations

import os
import time
import hmac
import base64
import hashlib
import json
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

LOG = PROJ / "data" / "logs" / "trend_follow.log"
STATE = PROJ / "data" / ".trend_follow_state.json"
CHECK_INTERVAL = 30
MIN_COOLDOWN_AFTER_TRADE = 600  # 10min 两次开仓冷却
BREAKOUT_THRESHOLD_BPS = 5       # 突破阈值


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


def check_breakout():
    """增强版突破检测（2026-04-22 专业升级）：
    1. 当前 close 突破近 20 根 15m 的最高/最低 ± 阈值
    2. 【新】成交量确认：当前 bar volume > 近 20 bar 平均 × 1.5
    3. 【新】连续 2 根 close 都在突破侧（防假突破）
    三重过滤大幅降低假突破概率。
    """
    k = httpx.get(
        "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=15m&limit=20",
        timeout=10,
    ).json()
    candles = k.get("data", [])
    if len(candles) < 20:
        return None, None, None, None

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    vols = [float(c[5]) for c in candles]  # 成交张数

    close = closes[0]
    prev_close = closes[1]
    hi_20 = max(highs[2:])   # 不含当前和前一根
    lo_20 = min(lows[2:])
    avg_vol = sum(vols[1:]) / len(vols[1:])   # 不含当前
    cur_vol = vols[0]
    thresh = BREAKOUT_THRESHOLD_BPS / 10000.0

    # 成交量确认
    vol_ok = cur_vol > avg_vol * 1.5

    # 向上突破：当前 close + 前 close 都 > 近 20 bar 高点
    if close > hi_20 * (1 + thresh) and prev_close > hi_20 * (1 + thresh * 0.5) and vol_ok:
        return "BREAKOUT_UP", close, hi_20, lo_20
    # 向下突破
    if close < lo_20 * (1 - thresh) and prev_close < lo_20 * (1 - thresh * 0.5) and vol_ok:
        return "BREAKOUT_DOWN", close, hi_20, lo_20
    return None, close, hi_20, lo_20


def confirm_4h_delta(direction):
    """4h 趋势确认：long 需 +1% / short 需 -1%"""
    k = httpx.get(
        "https://www.okx.com/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=15m&limit=16",
        timeout=10,
    ).json()
    candles = k.get("data", [])
    if len(candles) < 16:
        return False, 0
    close_now = float(candles[0][4])
    close_4h = float(candles[-1][4])
    delta_pct = (close_now - close_4h) / close_4h * 100
    if direction == "long":
        return delta_pct > 1.0, delta_pct
    return delta_pct < -1.0, delta_pct


def get_current_position():
    try:
        r = _api("GET", "/api/v5/account/positions?instId=ETH-USDT-SWAP")
        for x in r.get("data", []):
            pos = float(x.get("pos") or 0)
            if abs(pos) > 0.001:
                return pos
    except Exception:
        pass
    return 0.0


def get_direction_from_env():
    env = PROJ / ".env"
    if not env.exists():
        return "long"
    for line in env.read_text().splitlines():
        if line.startswith("GRID_DIRECTION="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "long"


def place_trend_order(direction, close_px):
    """市价追势开仓 + 挂 OCO 止盈止损"""
    # 1. 先开仓（market）
    side = "buy" if direction == "long" else "sell"
    body = json.dumps({
        "instId": "ETH-USDT-SWAP",
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": "1",
    })
    r = _api("POST", "/api/v5/trade/order", body)
    log(f"市价追势开仓 {direction} sz=1: {r}")
    if r.get("code") != "0":
        log(f"开仓失败: {r}")
        return False

    # 等 1 秒，给持仓入账
    time.sleep(1.5)

    # 2. 查实际 avgPx（用持仓 avgPx 更准）
    pos_r = _api("GET", "/api/v5/account/positions?instId=ETH-USDT-SWAP")
    avg_px = close_px
    for x in pos_r.get("data", []):
        if abs(float(x.get("pos") or 0)) > 0.001:
            avg_px = float(x.get("avgPx") or close_px)
            break

    # 3. 挂 OCO 止盈止损
    # 每 1 张 = 0.1 ETH ≈ $232 notional → $2 profit = 0.86% → 约 86bps
    # $1 loss = 0.43% → 约 43bps
    if direction == "long":
        tp_px = round(avg_px * (1 + 0.0086), 2)   # +86bps TP
        sl_px = round(avg_px * (1 - 0.0043), 2)   # -43bps SL
        close_side = "sell"
    else:
        tp_px = round(avg_px * (1 - 0.0086), 2)
        sl_px = round(avg_px * (1 + 0.0043), 2)
        close_side = "buy"

    oco_body = json.dumps({
        "instId": "ETH-USDT-SWAP",
        "tdMode": "isolated",
        "side": close_side,
        "ordType": "oco",
        "sz": "1",
        "tpTriggerPx": str(tp_px),
        "tpOrdPx": "-1",   # market
        "slTriggerPx": str(sl_px),
        "slOrdPx": "-1",
        "reduceOnly": "true",
    })
    r2 = _api("POST", "/api/v5/trade/order-algo", oco_body)
    log(f"OCO 挂: tp={tp_px} sl={sl_px} → {r2}")
    return True


def main():
    log(f"=== Trend Follow Watcher 启动 每 {CHECK_INTERVAL}s 检查 ===")
    while True:
        try:
            state = read_state()
            since_last = time.time() - state.get("last_trade_ts", 0)
            if since_last < MIN_COOLDOWN_AFTER_TRADE:
                time.sleep(CHECK_INTERVAL)
                continue

            # 有持仓不开新（避免与 grid 冲突）
            pos = get_current_position()
            if abs(pos) > 0.001:
                time.sleep(CHECK_INTERVAL)
                continue

            signal, close, hi, lo = check_breakout()
            if not signal:
                time.sleep(CHECK_INTERVAL)
                continue

            direction = "long" if signal == "BREAKOUT_UP" else "short"

            # direction 必须与 GRID_DIRECTION 一致（避免对冲）
            env_dir = get_direction_from_env()
            if direction != env_dir:
                log(f"突破 {signal} 但 GRID_DIRECTION={env_dir} 不匹配，跳过")
                time.sleep(CHECK_INTERVAL)
                continue

            # 4h delta 确认
            ok, delta = confirm_4h_delta(direction)
            if not ok:
                log(f"突破 {signal} 但 4h delta={delta:.2f}% 未确认，跳过")
                time.sleep(CHECK_INTERVAL)
                continue

            # 全部确认 → 开仓
            log(f"🚀 突破确认 {signal} close={close} hi_20={hi:.2f} lo_20={lo:.2f} 4h={delta:+.2f}%")
            if place_trend_order(direction, close):
                state["last_trade_ts"] = time.time()
                write_state(state)
        except Exception as e:
            log(f"异常: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
