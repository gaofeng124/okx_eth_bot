"""
亏损自动登记器（Q5 修复）

每次新亏损 > $0.3 → 自动写入 data/loss_ledger.md，避免主人每天手动复盘。

每 15 分钟扫描一次 OKX fills-history，发现新亏损自动登记：
  - 检查 data/.loss_logger_cursor 记录最后处理的 billId
  - 只处理 cursor 之后的新 fills
  - 匹配历史条目做根因对照（grep）
  - append 到 loss_ledger.md 末尾 + 更新 cursor

用法：
  python -m quant.tools.loss_auto_logger           # 单次扫描
  python -m quant.tools.loss_auto_logger --daemon  # 常驻每 15 min
"""
from __future__ import annotations

import argparse
import os
import time
import hmac
import base64
import hashlib
import re
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

LEDGER = PROJ / "data" / "loss_ledger.md"
CURSOR = PROJ / "data" / ".loss_logger_cursor"
LOSS_THRESHOLD = -0.30


def _sign(ts, m, p, body=""):
    secret = os.environ["OKX_SECRET_KEY"]
    return base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{m}{p}{body}".encode(), hashlib.sha256).digest()
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


def scan_once():
    """扫描 fills-history，记录新亏损。"""
    last_billid = ""
    if CURSOR.exists():
        last_billid = CURSOR.read_text().strip()

    r = _okx("/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100")
    fills = r.get("data", [])
    if not fills:
        return 0

    # 按 ts 升序处理
    fills.sort(key=lambda x: int(x.get("ts", 0)))

    new_losses = []
    new_cursor = last_billid
    for f in fills:
        bid = f.get("billId", "")
        if last_billid and bid <= last_billid:
            continue
        pnl = float(f.get("fillPnl") or 0)
        if pnl <= LOSS_THRESHOLD:
            new_losses.append(f)
        if bid > new_cursor:
            new_cursor = bid

    if new_losses:
        append_to_ledger(new_losses)

    # 更新 cursor
    if new_cursor != last_billid:
        CURSOR.parent.mkdir(parents=True, exist_ok=True)
        CURSOR.write_text(new_cursor)

    return len(new_losses)


def classify(fill, ledger_text):
    """粗略分类：搜 ledger 已有条目找同根因。"""
    sz = float(fill.get("fillSz") or 0)
    pnl = float(fill.get("fillPnl") or 0)
    t = datetime.fromtimestamp(int(fill["ts"]) / 1000, CST)
    side = fill.get("side", "")

    # 简单分类
    if sz >= 0.8:
        category = "L4 大仓位"
    elif pnl <= -1.0:
        category = "L3/L5 方向反向 + 止损超限"
    elif pnl <= -0.5:
        category = "L3 方向反向"
    else:
        category = "L9 手续费 / 滑点"

    return category, t, side, sz, pnl


def append_to_ledger(losses):
    """追加到 ledger.md 末尾。"""
    if not LEDGER.exists():
        return
    text = LEDGER.read_text()

    lines = []
    for f in losses:
        cat, t, side, sz, pnl = classify(f, text)
        fee = float(f.get("fee") or 0)
        net = pnl + fee
        entry = f"""
### 🤖 [自动登记] {t.strftime('%Y-%m-%d %H:%M:%S CST')} | {side} sz={sz} | PnL {pnl:+.3f} fee {fee:+.3f} net {net:+.3f}

**分类猜测**：{cat}

**原始数据**：fillPx={f.get('fillPx')} billId={f.get('billId')}

**根因分析**（daemon 待人工或下轮 AI 补充）：
- 当时 regime？
- 当时 direction？
- 持仓时长？
- 是否有同根因历史条目？

**状态**：🔴 待根因分析
"""
        lines.append(entry)

    with open(LEDGER, "a") as fp:
        fp.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args()

    while True:
        n = scan_once()
        if n:
            print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 登记了 {n} 笔新亏损")
        else:
            print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] 无新亏损")

        if not args.daemon:
            break
        time.sleep(900)  # 15 min


if __name__ == "__main__":
    main()
