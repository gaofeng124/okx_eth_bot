"""
Circuit Breaker —— 动态熔断器（主人 2026-04-22 15:20 CST 要求）

唯一任务：别让主人亏损太大，即时止损。

多级熔断（由轻到重）：
  Level 1 (warn):  近 30min 亏 > $1.5   → 邮件 [预警]
  Level 2 (pause): 近 1h   亏 > $2.5    → STRAT_LIVE=0（暂停 30min）
  Level 3 (halt):  近 2h   亏 > $4.0    → STRAT_LIVE=0（暂停 2h）
  Level 4 (kill):  今日    亏 > $6.5    → 完全暂停 + 清空挂单 + 等主人

每级都独立计算，互不抵消。

与现有 per_slot_stop / whole_stop / daily_stop 的区别：
  - 现有：单笔级 / 整体浮亏级 / 单日累计
  - 本熔断：**速率级**（短时间内速度过快 → 立刻停）
  - 防御"连续亏损螺旋"（今早 13:18-13:32 的连开 3 笔就是典型）

用法：
  python -m quant.tools.circuit_breaker            # 单次评估
  python -m quant.tools.circuit_breaker --daemon   # 常驻每 60s
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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

STATE = PROJ / "data" / ".circuit_breaker.json"
LOG = PROJ / "data" / "logs" / "circuit_breaker.log"
CHECK_INTERVAL = 60

# 熔断阈值
LEVELS = [
    {"name": "WARN",  "window_sec": 1800,  "loss_threshold": 1.5, "pause_sec": 0},
    {"name": "PAUSE", "window_sec": 3600,  "loss_threshold": 2.5, "pause_sec": 1800},
    {"name": "HALT",  "window_sec": 7200,  "loss_threshold": 4.0, "pause_sec": 7200},
    {"name": "KILL",  "window_sec": 86400, "loss_threshold": 6.5, "pause_sec": 999999},
]


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
        return {"last_trigger_ts": 0, "last_level": "NONE", "pause_until": 0}


def write_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def compute_window_pnl(fills, window_sec):
    cutoff_ms = int((time.time() - window_sec) * 1000)
    in_window = [f for f in fills if int(f.get("ts", 0)) >= cutoff_ms]
    return sum(float(f.get("fillPnl") or 0) + float(f.get("fee") or 0) for f in in_window), len(in_window)


def check_breakers():
    r = _api("GET", "/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100")
    fills = r.get("data", [])

    triggered = None
    for lvl in LEVELS:
        pnl, n = compute_window_pnl(fills, lvl["window_sec"])
        if pnl < -lvl["loss_threshold"]:
            triggered = {**lvl, "window_pnl": pnl, "fills_count": n}
            break  # 最严重的（最靠前的）先处理
    return triggered


def emergency_pause(level_name, pause_sec):
    """暂停 strategy：pkill run_strategy + 写 marker 防 watchdog 立即拉起。"""
    state = read_state()
    state["last_trigger_ts"] = time.time()
    state["last_level"] = level_name
    state["pause_until"] = time.time() + pause_sec
    write_state(state)

    # pkill（watchdog 会重启，但 grid_pro 读 circuit_breaker 状态会暂停交易）
    subprocess.run(["pkill", "-f", "run_strategy.py"], check=False)
    log(f"🚨 {level_name} 触发 → pkill run_strategy + 暂停 {pause_sec}s")


def emergency_cancel_orders():
    """撤销所有挂单。"""
    try:
        o = _api("GET", "/api/v5/trade/orders-pending?instType=SWAP&instId=ETH-USDT-SWAP")
        for od in o.get("data", []):
            body = json.dumps({"instId": "ETH-USDT-SWAP", "ordId": od["ordId"]})
            _api("POST", "/api/v5/trade/cancel-order", body)
        log(f"已撤销 {len(o.get('data', []))} 个挂单")
    except Exception as e:
        log(f"撤单异常: {e}")


def emergency_close():
    """平仓。"""
    try:
        body = json.dumps({"instId": "ETH-USDT-SWAP", "mgnMode": "isolated", "ccy": "USDT", "autoCxl": True})
        r = _api("POST", "/api/v5/trade/close-position", body)
        log(f"强制平仓: {r}")
    except Exception as e:
        log(f"平仓异常: {e}")


def should_trading_be_blocked():
    """供 grid_pro on_tick 调用：判断当前是否应暂停交易。
    返回 (blocked, reason)"""
    state = read_state()
    pause_until = state.get("pause_until", 0)
    if time.time() < pause_until:
        remaining = (pause_until - time.time()) / 60
        return True, f"circuit_breaker_{state.get('last_level')}_剩余{remaining:.0f}min"
    return False, None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    log("=== Circuit Breaker 启动 ===")

    while True:
        try:
            # 已在暂停期 → 等待
            state = read_state()
            if time.time() < state.get("pause_until", 0):
                remaining = (state["pause_until"] - time.time()) / 60
                log(f"暂停中（{state.get('last_level')}），剩余 {remaining:.1f} min")
            else:
                triggered = check_breakers()
                if triggered:
                    log(f"🚨 触发 {triggered['name']}: 近 {triggered['window_sec']//60}min 亏 ${triggered['window_pnl']:+.3f} > 阈值 ${triggered['loss_threshold']}")
                    if triggered["name"] == "KILL":
                        # 最严重：撤单 + 平仓 + 长期暂停
                        emergency_cancel_orders()
                        emergency_close()
                        emergency_pause(triggered["name"], triggered["pause_sec"])
                    elif triggered["name"] in ("PAUSE", "HALT"):
                        emergency_pause(triggered["name"], triggered["pause_sec"])
                    else:  # WARN
                        log("仅预警，未采取动作")
        except Exception as e:
            log(f"ERROR: {e}")

        if not args.daemon:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
