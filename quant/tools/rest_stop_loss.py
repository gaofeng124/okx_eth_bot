"""
REST 止损兜底（Q1 / E3 修复）

解决：per_slot_stop=$1.5 被击穿至 -$1.95（超 30%）—— WS tick 延迟 / 跳空漏掉触发。

设计：
  独立后台进程，每 10 秒通过 REST API 查持仓 upl。
  若 upl < -per_slot_stop × 1.0（不是 1.2，更严格）→ 立即市价平仓。
  不依赖 strategy 的 tick 回调，**双层保险**。

用法：
  python -m quant.tools.rest_stop_loss           # 前台跑（调试）
  nohup python -m quant.tools.rest_stop_loss &   # 后台跑

  或加 systemd service 自动启动。
"""
from __future__ import annotations

import os
import time
import hmac
import base64
import hashlib
import json
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv("/root/okx_eth_bot/.env")
if not os.environ.get("OKX_API_KEY"):
    load_dotenv("/Users/gaofeng/Documents/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))
CHECK_INTERVAL_SEC = 10
STOP_LOSS_USDT = float(os.getenv("GRID_PER_SLOT_STOP_USDT", "1.5"))
# 兜底更严：比 per_slot_stop 早 20% 触发（防 tick 延迟漏掉）
TRIGGER_UPL = -STOP_LOSS_USDT * 0.9
LOG_PATH = "/root/okx_eth_bot/data/logs/rest_stop_loss.log"


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
        return httpx.get("https://www.okx.com" + path, headers=h, timeout=10).json()
    return httpx.post("https://www.okx.com" + path, headers=h, content=body, timeout=10).json()


def log(msg):
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def emergency_close():
    """市价平 ETH-USDT-SWAP 仓位。"""
    body = json.dumps({
        "instId": "ETH-USDT-SWAP",
        "mgnMode": "isolated",
        "ccy": "USDT",
        "autoCxl": True,
    })
    r = _api("POST", "/api/v5/trade/close-position", body)
    log(f"EMERGENCY CLOSE 结果: {r}")
    return r


def check_once():
    """单次检查：若 upl < TRIGGER_UPL 则平仓。"""
    try:
        r = _api("GET", "/api/v5/account/positions?instId=ETH-USDT-SWAP")
        for x in r.get("data", []):
            pos = float(x.get("pos") or 0)
            if abs(pos) < 0.001:
                continue
            upl = float(x.get("upl") or 0)
            avg_px = x.get("avgPx")
            if upl < TRIGGER_UPL:
                log(f"🚨 REST 兜底触发: upl={upl:.3f} < {TRIGGER_UPL:.3f} "
                    f"(pos={pos} avgPx={avg_px}) → 市价平仓")
                emergency_close()
                return True
    except Exception as e:
        log(f"检查异常: {e}")
    return False


def main():
    log(f"=== REST 止损兜底启动 触发阈值 upl < {TRIGGER_UPL} USDT 查询间隔 {CHECK_INTERVAL_SEC}s ===")
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
