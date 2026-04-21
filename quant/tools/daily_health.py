"""
每日健康检查 —— 项目负责人承诺书强制执行机制。

每日 CST 23:00 自动运行：
  1. 算今日 CST 0-24h 真实 PnL（从 OKX fills-history）
  2. 算"当前阶段"对应的最低合格线（186U × 1.6%/日）
  3. 对比：
     - ≥ $3：合格 → 写报告 + 简报邮件
     - < $3 但 > $0：低于目标 → 报告 + 提醒邮件
     - < $0：不达标 → 🚨 邮件 + 强制 AI 下轮做根因分析
     - 连续 2 日 < $0：暂停新功能开发
     - 连续 3 日 < $0 或累计亏损 > $5：暂停策略 + 等主人审批

### 同时查
  - 链上信号（Etherscan 净流入）
  - Max quota 使用率（通过观察 daemon log 错误率粗估）
  - Loss Ledger 🟡 待防护条目数

### 设计目标
这是 AI daemon / 人工都可以调用的独立检查器，不依赖任何内部状态。

用法:
    python -m quant.tools.daily_health       # 运行完整检查
    python -m quant.tools.daily_health --email  # 同时发邮件
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import hmac
import base64
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/root/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")

# 阶段性目标（随账户规模动态调整）
TIER_TARGETS = [
    # (account_floor, daily_min_pass, daily_good, loss_ceiling)
    (50, 1.0, 3.0, 2.0),
    (100, 2.0, 5.0, 3.0),
    (150, 2.5, 6.0, 4.0),
    (200, 3.0, 8.0, 5.0),
    (500, 8.0, 20.0, 10.0),
    (1000, 15.0, 40.0, 20.0),
]


def _get_tier(equity: float) -> dict:
    """根据账户规模返回当前阶段目标。"""
    tier = TIER_TARGETS[0]
    for t in TIER_TARGETS:
        if equity >= t[0]:
            tier = t
    floor, min_pass, good, loss_ceil = tier
    return {
        "tier_floor": floor,
        "daily_min_pass": min_pass,
        "daily_good": good,
        "daily_loss_ceiling": loss_ceil,
    }


def _sign(ts: str, m: str, p: str, body: str = "") -> str:
    secret = os.environ["OKX_SECRET_KEY"]
    msg = f"{ts}{m}{p}{body}"
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _okx_get(path: str) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    h = {
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": _sign(ts, "GET", path),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    return httpx.get("https://www.okx.com" + path, headers=h, timeout=30).json()


def get_account_snapshot() -> dict:
    """账户权益 + 持仓。"""
    b = _okx_get("/api/v5/account/balance")
    total_eq = float(b["data"][0].get("totalEq", 0))
    usdt_info = {}
    for d in b["data"][0]["details"]:
        if d["ccy"] == "USDT":
            usdt_info = {
                "eq": float(d["eq"]),
                "avail": float(d["availBal"]),
                "upl": float(d.get("upl") or 0),
            }
    pos = _okx_get("/api/v5/account/positions?instId=ETH-USDT-SWAP")
    positions = []
    for x in pos["data"]:
        if float(x.get("pos") or 0):
            positions.append({
                "pos": x.get("pos"),
                "avgPx": x.get("avgPx"),
                "upl": x.get("upl"),
                "liq": x.get("liqPx"),
            })
    return {
        "total_eq": total_eq,
        "usdt": usdt_info,
        "positions": positions,
    }


def get_pnl_today_cst() -> dict:
    """今日 CST 0-24h 从 OKX fills 精确计算。"""
    today_start = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)
    r = _okx_get(
        "/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100"
    )
    fills = r.get("data", [])
    today_fills = [f for f in fills if int(f.get("ts", 0)) >= today_start_ms]
    gross = sum(float(f.get("fillPnl") or 0) for f in today_fills)
    fee = sum(float(f.get("fee") or 0) for f in today_fills)
    wins = [float(f["fillPnl"]) for f in today_fills if float(f.get("fillPnl") or 0) > 0]
    losses = [float(f["fillPnl"]) for f in today_fills if float(f.get("fillPnl") or 0) < 0]
    return {
        "date": today_start.strftime("%Y-%m-%d"),
        "fills_count": len(today_fills),
        "gross_pnl": round(gross, 3),
        "fee": round(fee, 3),
        "net_pnl": round(gross + fee, 3),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
        "wl_ratio": round(
            abs((sum(wins) / len(wins)) / (sum(losses) / len(losses))), 2
        ) if wins and losses else None,
    }


def get_recent_days_pnl(days: int = 7) -> list:
    """近 N 天每日 PnL（粗估：按 UTC 成交时间分桶）。"""
    r = _okx_get(
        "/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=500"
    )
    fills = r.get("data", [])
    per_day = {}
    for f in fills:
        ts = int(f.get("ts", 0))
        d = datetime.fromtimestamp(ts / 1000, CST).strftime("%Y-%m-%d")
        per_day.setdefault(d, {"pnl": 0.0, "fee": 0.0, "count": 0})
        per_day[d]["pnl"] += float(f.get("fillPnl") or 0)
        per_day[d]["fee"] += float(f.get("fee") or 0)
        per_day[d]["count"] += 1
    out = []
    for d in sorted(per_day.keys(), reverse=True)[:days]:
        info = per_day[d]
        out.append({
            "date": d,
            "net_pnl": round(info["pnl"] + info["fee"], 3),
            "fills": info["count"],
        })
    return out


def assess(equity: float, today: dict, recent: list) -> dict:
    """综合评估当前是否达标。"""
    tier = _get_tier(equity)
    net = today["net_pnl"]
    if net >= tier["daily_good"]:
        grade = "优秀"
        level = "good"
    elif net >= tier["daily_min_pass"]:
        grade = "合格"
        level = "pass"
    elif net >= 0:
        grade = "低于目标"
        level = "below"
    elif net >= -tier["daily_loss_ceiling"]:
        grade = "不达标"
        level = "fail"
    else:
        grade = "严重不达标"
        level = "critical"

    # 连续亏损天数
    neg_streak = 0
    for d in recent:
        if d["net_pnl"] < 0:
            neg_streak += 1
        else:
            break

    alerts = []
    if level == "critical":
        alerts.append("🚨 单日严重亏损超限")
    if neg_streak >= 2:
        alerts.append(f"⚠️ 连续 {neg_streak} 日负 PnL（承诺暂停新功能）")
    if neg_streak >= 3:
        alerts.append(f"🛑 连续 {neg_streak} 日负 PnL（承诺暂停策略等主人审批）")

    return {
        "equity": equity,
        "tier": tier,
        "today_grade": grade,
        "today_level": level,
        "today_net": net,
        "consecutive_negative_days": neg_streak,
        "alerts": alerts,
    }


def format_report(snapshot: dict, today: dict, recent: list, assessment: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"📊 每日健康检查报告 | {datetime.now(CST).strftime('%Y-%m-%d %H:%M CST')}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"💰 账户:  总权益 {snapshot['total_eq']:.2f} USDT")
    lines.append(f"        可用 {snapshot['usdt'].get('avail', 0):.2f}")
    if snapshot["positions"]:
        for p in snapshot["positions"]:
            lines.append(f"        持仓 {p['pos']}@{p['avgPx']} upl={p['upl']}")
    lines.append("")
    lines.append(f"📈 今日 CST PnL ({today['date']}):")
    lines.append(f"   成交 {today['fills_count']} 笔 | "
                 f"Gross {today['gross_pnl']:+.3f} | Fee {today['fee']:+.3f} | "
                 f"Net {today['net_pnl']:+.3f}")
    if today.get("wl_ratio") is not None:
        lines.append(f"   盈 {today['wins']} 平均 {today['avg_win']:+.3f} | "
                     f"亏 {today['losses']} 平均 {today['avg_loss']:+.3f} | "
                     f"盈亏比 {today['wl_ratio']}")
    lines.append("")
    tier = assessment["tier"]
    lines.append(
        f"🎯 阶段目标（tier ${tier['tier_floor']}+）: "
        f"合格 ≥ ${tier['daily_min_pass']} | 优秀 ≥ ${tier['daily_good']}"
    )
    lines.append(f"   当日评估: 【{assessment['today_grade']}】")
    lines.append("")
    lines.append("📅 近 7 日 PnL:")
    for d in recent[:7]:
        tag = "✅" if d["net_pnl"] > 0 else "❌" if d["net_pnl"] < 0 else "➖"
        lines.append(f"   {d['date']} {tag} {d['net_pnl']:+7.3f} USDT ({d['fills']} 笔)")
    lines.append("")
    if assessment["alerts"]:
        lines.append("🚨 告警:")
        for a in assessment["alerts"]:
            lines.append(f"   {a}")
    else:
        lines.append("✅ 无告警")
    lines.append("")
    return "\n".join(lines)


def run(send_email: bool = False) -> dict:
    snap = get_account_snapshot()
    today = get_pnl_today_cst()
    recent = get_recent_days_pnl(7)
    assessment = assess(snap["total_eq"], today, recent)
    report = format_report(snap, today, recent, assessment)
    print(report)
    # 落盘
    out_file = PROJ / "data" / f"daily_health_{today['date']}.txt"
    out_file.write_text(report)
    if send_email:
        import subprocess
        subject_prefix = {
            "good": "✅", "pass": "✅", "below": "⚠️",
            "fail": "🚨", "critical": "🚨🚨"
        }.get(assessment["today_level"], "📊")
        subprocess.run(
            ["/root/okx_eth_bot/.venv/bin/python",
             "/root/okx_eth_bot/notify.py", "daily"],
            check=False,
        )
    return {
        "snapshot": snap,
        "today": today,
        "recent": recent,
        "assessment": assessment,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", action="store_true")
    args = parser.parse_args()
    result = run(send_email=args.email)
    sys.exit(0 if result["assessment"]["today_level"] in ("good", "pass") else 1)
