"""
系统运行态健康扫描（L10-001 永久性防护）

设计目标：解决"daemon 读了日志但没主动发现市场-策略不匹配"的学习循环缺口。

每轮 AI daemon 必调用，输出结构化健康度快照 + 异常信号 + 推荐动作。

用法：
    python -m quant.tools.system_health           # 人可读输出
    python -m quant.tools.system_health --json    # JSON 输出（daemon 消化用）

异常信号（daemon 看到必介入）：
    STALL_NO_FILLS          近 30min 零成交（市场活跃时）
    LOW_CAPITAL_USAGE       资金利用率 < 15%（与 Phase 目标不符）
    MISSING_GRID_LEVELS     挂单数 < 预期（设计 max_levels 但实际更少）
    SPACING_TOO_WIDE        spacing > 当前 ATR（挂单够不着市场）
    FEE_GROSS_RATIO_HIGH    近 20 笔 fee/gross > 40%（被手续费吃掉）
    UNREALIZED_BLEEDING     持仓浮亏 > $1（接近 per_slot_stop）
    DAEMON_INACTIVE         近 10min daemon 无 agent_report 更新（AI 侧挂了）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import hmac
import base64
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv("/root/okx_eth_bot/.env")
# Mac 本地兜底（worktree 开发时）
if not os.environ.get("OKX_API_KEY"):
    load_dotenv("/Users/gaofeng/Documents/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")


def _sign(ts: str, m: str, p: str, body: str = "") -> str:
    secret = os.environ["OKX_SECRET_KEY"]
    return base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{m}{p}{body}".encode(), hashlib.sha256).digest()
    ).decode()


def _okx(path: str) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    h = {
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": _sign(ts, "GET", path),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    return httpx.get("https://www.okx.com" + path, headers=h, timeout=20).json()


def scan() -> dict[str, Any]:
    """返回结构化健康快照 + 异常信号 + 推荐动作。"""
    now = datetime.now(CST)
    now_ms = int(now.timestamp() * 1000)
    out: dict[str, Any] = {
        "ts_cst": now.strftime("%Y-%m-%d %H:%M:%S"),
        "anomalies": [],
        "recommended_actions": [],
    }

    # ── 1. 账户 + 持仓 + 资金利用率 ───────────────────────────────
    try:
        b = _okx("/api/v5/account/balance")
        eq = float(b["data"][0].get("totalEq", 0))
        usdt = next((d for d in b["data"][0]["details"] if d["ccy"] == "USDT"), {})
        avail = float(usdt.get("availBal", 0) or 0)
        upl = float(usdt.get("upl", 0) or 0)
        margin_used = eq - avail
        capital_usage_pct = margin_used / eq * 100 if eq > 0 else 0
        out["account"] = {
            "equity_usdt": round(eq, 3),
            "avail_usdt": round(avail, 3),
            "upl_usdt": round(upl, 3),
            "margin_used_usdt": round(margin_used, 3),
            "capital_usage_pct": round(capital_usage_pct, 2),
        }
        if capital_usage_pct < 15:
            out["anomalies"].append("LOW_CAPITAL_USAGE")
            out["recommended_actions"].append(
                f"资金利用率 {capital_usage_pct:.1f}% < 15% → 检查 Phase 是否卡住、挂单数是否足量"
            )
    except Exception as e:
        out["account_error"] = str(e)

    # ── 2. 持仓 + 浮亏状态 ────────────────────────────────────────
    try:
        p = _okx("/api/v5/account/positions?instId=ETH-USDT-SWAP")
        positions = []
        for x in p.get("data", []):
            if float(x.get("pos") or 0):
                positions.append({
                    "pos": float(x["pos"]),
                    "avgPx": float(x["avgPx"]),
                    "upl": float(x.get("upl") or 0),
                    "uplRatio": float(x.get("uplRatio") or 0),
                    "notionalUsd": float(x.get("notionalUsd") or 0),
                    "liqPx": float(x.get("liqPx") or 0),
                })
                if float(x.get("upl") or 0) < -1.0:
                    out["anomalies"].append("UNREALIZED_BLEEDING")
                    out["recommended_actions"].append(
                        f"持仓浮亏 {x['upl']} USDT 接近 per_slot_stop（$1.5）→ 考虑主动平或等 aging"
                    )
        out["positions"] = positions
    except Exception as e:
        out["positions_error"] = str(e)

    # ── 3. 挂单数 vs 预期 ─────────────────────────────────────────
    try:
        o = _okx("/api/v5/trade/orders-pending?instType=SWAP&instId=ETH-USDT-SWAP")
        pending = o.get("data", [])
        expected = int(os.getenv("GRID_LEVELS", "4"))
        # 扣除 TP 挂单（近价的平仓单）
        # 粗略判断：如果有持仓，其中 1 单是 TP；如果无持仓，所有都是 entry
        tp_offset = 1 if out.get("positions") else 0
        entry_count = max(0, len(pending) - tp_offset)
        out["orders"] = {
            "total_pending": len(pending),
            "est_entry_orders": entry_count,
            "expected_entry_orders": expected,
            "details": [
                {
                    "side": x["side"],
                    "sz": float(x["sz"]),
                    "px": float(x["px"]),
                    "age_min": round((now_ms - int(x.get("cTime", 0))) / 60000, 1),
                }
                for x in pending
            ],
        }
        if entry_count < expected * 0.5:
            out["anomalies"].append("MISSING_GRID_LEVELS")
            out["recommended_actions"].append(
                f"挂单仅 {entry_count}/{expected} 档 → 检查 vol_regime / US session cap / gate 是否过严"
            )
    except Exception as e:
        out["orders_error"] = str(e)

    # ── 4. 近 30/60min 成交频率 ────────────────────────────────────
    try:
        r = _okx("/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100")
        fills = r.get("data", [])
        t30 = now_ms - 30 * 60000
        t60 = now_ms - 60 * 60000
        fills_30 = [f for f in fills if int(f["ts"]) >= t30]
        fills_60 = [f for f in fills if int(f["ts"]) >= t60]
        last_fill_ts = max([int(f["ts"]) for f in fills]) if fills else 0
        idle_min = (now_ms - last_fill_ts) / 60000 if last_fill_ts else 999
        out["fills"] = {
            "count_30min": len(fills_30),
            "count_60min": len(fills_60),
            "idle_min_since_last": round(idle_min, 1),
        }
        if idle_min > 30 and len(fills_30) == 0:
            out["anomalies"].append("STALL_NO_FILLS")
            out["recommended_actions"].append(
                f"近 {idle_min:.0f}min 零成交 → 检查 spacing vs ATR / 挂单距离 / gate 阻挡"
            )
    except Exception as e:
        out["fills_error"] = str(e)

    # ── 5. 近 20 笔 EV / 盈亏比 / fee 占比 ─────────────────────────
    if fills:
        recent20 = fills[:20]
        gross = sum(float(f.get("fillPnl") or 0) for f in recent20)
        fee = sum(float(f.get("fee") or 0) for f in recent20)
        wins = [float(f["fillPnl"]) for f in recent20 if float(f.get("fillPnl") or 0) > 0]
        losses = [float(f["fillPnl"]) for f in recent20 if float(f.get("fillPnl") or 0) < 0]
        ev = (gross + fee) / len(recent20) if recent20 else 0
        wl = (
            abs((sum(wins) / len(wins)) / (sum(losses) / len(losses)))
            if wins and losses else None
        )
        fee_ratio = abs(fee / gross) * 100 if gross > 0 else 0
        out["ev_stats"] = {
            "n": len(recent20),
            "gross_pnl": round(gross, 3),
            "fee": round(fee, 3),
            "net_pnl": round(gross + fee, 3),
            "ev_per_trade": round(ev, 4),
            "wins": len(wins),
            "losses": len(losses),
            "wl_ratio": round(wl, 2) if wl else None,
            "fee_over_gross_pct": round(fee_ratio, 1),
        }
        if fee_ratio > 40:
            out["anomalies"].append("FEE_GROSS_RATIO_HIGH")
            out["recommended_actions"].append(
                f"fee/gross {fee_ratio:.1f}% > 40% → spacing 太窄或 TP 倍数过小，拉宽 spacing"
            )

    # ── 6. 市场 ATR vs spacing ─────────────────────────────────────
    try:
        k = _okx("/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=1m&limit=30")
        if k.get("data"):
            candles = k["data"]
            closes = [float(c[4]) for c in candles]
            highs = [float(c[2]) for c in candles]
            lows = [float(c[3]) for c in candles]
            ranges_bps = [(h - l) / c * 10000 for h, l, c in zip(highs, lows, closes)]
            last = closes[0]
            atr_30min_bps = sum(ranges_bps) / len(ranges_bps)
            range_30_bps = (max(highs) - min(lows)) / last * 10000
            # 当前 spacing 从 .env 读
            env_spacing = float(os.getenv("GRID_MIN_SPACING_PCT", "0.0020")) * 10000
            out["market"] = {
                "last": round(last, 2),
                "atr_30min_bps_avg": round(atr_30min_bps, 1),
                "range_30min_bps": round(range_30_bps, 1),
                "spacing_min_env_bps": round(env_spacing, 1),
            }
            if env_spacing > atr_30min_bps * 2 and atr_30min_bps > 0:
                out["anomalies"].append("SPACING_TOO_WIDE")
                out["recommended_actions"].append(
                    f"spacing {env_spacing:.1f}bps > 2× ATR({atr_30min_bps:.1f}bps) → 静市挂单够不着"
                )
    except Exception as e:
        out["market_error"] = str(e)

    # ── 7. Daemon 活跃度 ──────────────────────────────────────────
    try:
        report_path = PROJ / "data" / "agent_report.json"
        if report_path.exists():
            age_sec = time.time() - report_path.stat().st_mtime
            out["daemon"] = {
                "agent_report_age_min": round(age_sec / 60, 1),
                "healthy": age_sec < 1800,  # 30min 内有更新
            }
            if age_sec > 1800:
                out["anomalies"].append("DAEMON_INACTIVE")
                out["recommended_actions"].append(
                    f"agent_report.json {age_sec/60:.0f}min 未更新 → daemon 可能挂了，检查 systemctl status ai-brain"
                )
    except Exception as e:
        out["daemon_error"] = str(e)

    # ── 8. 今日 PnL 对比 tier 目标 ─────────────────────────────────
    try:
        today_0_ms = int(
            datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        )
        today_fills = [f for f in fills if int(f["ts"]) >= today_0_ms]
        today_pnl = sum(
            float(f.get("fillPnl") or 0) + float(f.get("fee") or 0) for f in today_fills
        )
        hours_passed = (now_ms - today_0_ms) / 3600000
        target_pass = 3.0  # 186U 阶段合格线
        expected_now = target_pass * (hours_passed / 24)
        out["today_pnl"] = {
            "net_usdt": round(today_pnl, 3),
            "fills": len(today_fills),
            "hours_passed": round(hours_passed, 1),
            "tier_target_pass_full_day": target_pass,
            "expected_by_now": round(expected_now, 3),
            "on_track": today_pnl >= expected_now * 0.8,  # 允许 20% 宽容
        }
    except Exception:
        pass

    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = scan()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 人可读输出
    print("=" * 60)
    print(f"系统健康快照 @ {result['ts_cst']}")
    print("=" * 60)
    if acct := result.get("account"):
        print(f"\n💰 账户：权益 ${acct['equity_usdt']:.2f}  保证金利用 {acct['capital_usage_pct']:.1f}%")
    if pos := result.get("positions"):
        for p in pos:
            print(f"   持仓 {p['pos']:+.2f} @ ${p['avgPx']:.2f}  upl={p['upl']:+.3f}")
    if od := result.get("orders"):
        print(f"\n📋 挂单：{od['total_pending']} 单（入场预计 {od['est_entry_orders']}/{od['expected_entry_orders']}）")
    if fl := result.get("fills"):
        print(f"\n🔄 成交：30min={fl['count_30min']}  60min={fl['count_60min']}  距上笔 {fl['idle_min_since_last']:.0f}min")
    if ev := result.get("ev_stats"):
        print(f"\n📊 近 {ev['n']} 笔：net {ev['net_pnl']:+.3f} EV {ev['ev_per_trade']:+.4f} WL {ev['wl_ratio']} fee/gross {ev['fee_over_gross_pct']:.1f}%")
    if m := result.get("market"):
        print(f"\n🌊 市场：${m['last']:.2f}  ATR30m {m['atr_30min_bps_avg']:.0f}bps  spacing下限 {m['spacing_min_env_bps']:.0f}bps")
    if d := result.get("daemon"):
        flag = "✅" if d["healthy"] else "🚨"
        print(f"\n🤖 Daemon：{flag} agent_report 距今 {d['agent_report_age_min']:.0f}min")
    if tp := result.get("today_pnl"):
        flag = "✅" if tp["on_track"] else "⚠️"
        print(f"\n🎯 今日：{flag} net ${tp['net_usdt']:.2f}  ({tp['hours_passed']:.1f}h / 期望 ${tp['expected_by_now']:.2f})")
    if result["anomalies"]:
        print("\n🚨 异常信号：")
        for a in result["anomalies"]:
            print(f"   ❗ {a}")
        print("\n🔧 推荐动作：")
        for a in result["recommended_actions"]:
            print(f"   → {a}")
    else:
        print("\n✅ 无异常")
    print()


if __name__ == "__main__":
    main()
