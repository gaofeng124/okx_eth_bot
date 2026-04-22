"""
Phase 升级数据驱动监控（只建议，不动手）

解决：之前我"凭直觉放大"失败 3 次。改为：用数据证明可放大才 email 建议。

决策矩阵（基于近 50 笔连续样本）：
  Phase 1 → Phase 2: EV ≥ +$0.03 且 WL ≥ 0.45 且 fee/gross ≤ 45%
  Phase 2 → Phase 3: EV ≥ +$0.05 且 WL ≥ 0.55 且 fee/gross ≤ 40%
  Phase 3 → Phase 4: EV ≥ +$0.08 且 WL ≥ 0.65 且 fee/gross ≤ 35%

反向（降级）：
  任何 Phase → 下一级：EV ≤ -$0.02 持续 20 笔 → 邮件建议降级

用法：
  python -m quant.tools.phase_monitor            # 单次评估
  python -m quant.tools.phase_monitor --daemon   # 常驻每 30min
"""
from __future__ import annotations

import argparse
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

UPGRADE_THRESHOLDS = {
    "to_phase2": {"ev": 0.03, "wl": 0.45, "fee_ratio": 0.45, "levels": 4, "cps": 1.0},
    "to_phase3": {"ev": 0.05, "wl": 0.55, "fee_ratio": 0.40, "levels": 5, "cps": 1.2},
    "to_phase4": {"ev": 0.08, "wl": 0.65, "fee_ratio": 0.35, "levels": 6, "cps": 1.2},
}


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


def read_current_phase():
    env = PROJ / ".env"
    if not env.exists():
        return 1, 3, 0.3
    levels = 3
    cps = 0.3
    for line in env.read_text().splitlines():
        if line.startswith("GRID_LEVELS="):
            try: levels = int(line.split("=", 1)[1].strip())
            except: pass
        if line.startswith("GRID_CONTRACTS_PER_SLOT="):
            try: cps = float(line.split("=", 1)[1].strip())
            except: pass
    # 推断 phase
    if cps >= 1.2 and levels >= 6:
        return 4, levels, cps
    if cps >= 1.2 and levels >= 5:
        return 3, levels, cps
    if cps >= 1.0 and levels >= 5:
        return 2, levels, cps
    if cps >= 1.0 and levels >= 3:
        return 1, levels, cps
    return 0, levels, cps


def evaluate():
    """评估近 50 笔 → 返回建议"""
    r = _okx("/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=50")
    fills = r.get("data", [])
    if len(fills) < 20:
        return {"error": "insufficient_fills", "count": len(fills)}

    gross = sum(float(f.get("fillPnl") or 0) for f in fills)
    fee = sum(float(f.get("fee") or 0) for f in fills)
    net = gross + fee
    wins = [float(f["fillPnl"]) for f in fills if float(f.get("fillPnl") or 0) > 0]
    losses = [float(f["fillPnl"]) for f in fills if float(f.get("fillPnl") or 0) < 0]
    ev = net / len(fills)
    wl = abs((sum(wins)/len(wins)) / (sum(losses)/len(losses))) if wins and losses else None
    fee_ratio = abs(fee / gross) if gross > 0 else 1.0

    cur_phase, cur_levels, cur_cps = read_current_phase()

    result = {
        "ts_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "current_phase": cur_phase,
        "current_levels": cur_levels,
        "current_cps": cur_cps,
        "sample_size": len(fills),
        "ev_per_trade": round(ev, 4),
        "wl_ratio": round(wl, 3) if wl else None,
        "fee_over_gross": round(fee_ratio, 3),
        "net_pnl": round(net, 3),
        "recommendation": None,
        "reason": None,
    }

    # 降级优先判定
    if ev < -0.02:
        result["recommendation"] = "DOWNGRADE"
        result["reason"] = f"近 {len(fills)} 笔 EV={ev:.4f} 持续为负，建议降级 Phase"
        return result

    # 升级判定
    next_map = {0: "to_phase1", 1: "to_phase2", 2: "to_phase3", 3: "to_phase4"}
    if cur_phase < 4 and cur_phase in next_map:
        target = next_map[cur_phase]
        if target == "to_phase1":
            t = {"ev": 0.01, "wl": 0.40, "fee_ratio": 0.50, "levels": 3, "cps": 1.0}
        else:
            t = UPGRADE_THRESHOLDS[target]
        if ev >= t["ev"] and (wl is None or wl >= t["wl"]) and fee_ratio <= t["fee_ratio"]:
            result["recommendation"] = "UPGRADE"
            result["target"] = target
            result["target_levels"] = t["levels"]
            result["target_cps"] = t["cps"]
            result["reason"] = (
                f"近 {len(fills)} 笔达标: EV={ev:.4f}≥{t['ev']} "
                f"WL={wl}≥{t['wl']} fee_ratio={fee_ratio:.2f}≤{t['fee_ratio']}"
            )
        else:
            result["recommendation"] = "HOLD"
            result["reason"] = f"未达升级门槛：EV {ev:.4f} / WL {wl} / fee_ratio {fee_ratio:.2f}"
    else:
        result["recommendation"] = "HOLD"
        result["reason"] = "已 Phase 4 最高级"

    return result


def send_email_if_actionable(result):
    """只在 UPGRADE/DOWNGRADE 时写建议文件（邮件由 daemon 决定是否发）"""
    if result.get("recommendation") in ("UPGRADE", "DOWNGRADE"):
        advise = PROJ / "data" / ".phase_advice.json"
        advise.parent.mkdir(parents=True, exist_ok=True)
        advise.write_text(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()
    while True:
        r = evaluate()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        send_email_if_actionable(r)
        if not args.daemon:
            break
        time.sleep(1800)


if __name__ == "__main__":
    main()
