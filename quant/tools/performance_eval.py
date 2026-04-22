"""
专业评估体系：Sharpe / MDD / Kelly / Profit Factor

替代之前"只看日 PnL"的幼稚评估。成熟量化必备指标：

- **Sharpe Ratio** = 收益均值 / 收益标准差（年化）
  - > 1.0 合格，> 2.0 优秀，> 3.0 世界级
- **Max Drawdown (MDD)** = 最大连续回撤百分比
  - 小账户 < 10% 可接受
- **Calmar Ratio** = 年化收益 / MDD
  - > 1.0 合格
- **Profit Factor** = 总盈利 / |总亏损|
  - > 1.5 合格
- **Kelly Fraction** = (胜率 × 盈亏比 - 亏率) / 盈亏比
  - 最优仓位比例。负值 = 不该交易
  - 实际用 Quarter Kelly（× 0.25）更保守

每小时跑一次，输出到 data/perf_eval.json。

用法：
  python -m quant.tools.performance_eval             # 单次
  python -m quant.tools.performance_eval --daemon    # 每 1h
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

OUT = PROJ / "data" / "perf_eval.json"


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


def compute_metrics(fills):
    """从 fills 列表算所有指标。"""
    if len(fills) < 10:
        return {"error": "insufficient_fills", "count": len(fills)}

    # 按时间升序（OKX 默认倒序）
    fills_sorted = sorted(fills, key=lambda x: int(x["ts"]))

    # 每笔净利（fillPnl + fee）
    net_pnls = [float(f.get("fillPnl") or 0) + float(f.get("fee") or 0) for f in fills_sorted]

    # 累计 PnL 曲线
    cumpnl = []
    run = 0.0
    for p in net_pnls:
        run += p
        cumpnl.append(run)

    # Max Drawdown
    peak = cumpnl[0]
    mdd = 0.0
    mdd_start = 0
    for i, v in enumerate(cumpnl):
        if v > peak:
            peak = v
        dd = peak - v
        if dd > mdd:
            mdd = dd
            mdd_start = i

    # Sharpe（按笔分布，非年化）：mean / std
    mean_r = sum(net_pnls) / len(net_pnls)
    var_r = sum((p - mean_r) ** 2 for p in net_pnls) / max(len(net_pnls) - 1, 1)
    std_r = math.sqrt(var_r) if var_r > 0 else 0
    sharpe_per_trade = mean_r / std_r if std_r > 0 else 0
    # 年化（假设 50 笔/日 × 365 天）
    trades_per_year = 50 * 365
    sharpe_annualized = sharpe_per_trade * math.sqrt(trades_per_year)

    # Profit Factor
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")

    # Win Rate + Avg Win/Loss
    win_rate = len(wins) / len(net_pnls)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    wl_ratio = abs(avg_win / avg_loss) if avg_loss else None

    # Kelly Fraction
    # f* = (p × b - q) / b
    # p = 胜率, q = 败率, b = 盈亏比
    kelly = None
    if wl_ratio and wl_ratio > 0:
        p_rate = win_rate
        q_rate = 1 - p_rate
        kelly = (p_rate * wl_ratio - q_rate) / wl_ratio

    # Calmar（年化 / MDD）
    total_pnl = cumpnl[-1]
    # 时间跨度
    span_sec = (int(fills_sorted[-1]["ts"]) - int(fills_sorted[0]["ts"])) / 1000
    span_days = span_sec / 86400 if span_sec > 0 else 1
    annualized_pnl = total_pnl / span_days * 365
    calmar = annualized_pnl / mdd if mdd > 0 else None

    return {
        "sample_size": len(net_pnls),
        "span_days": round(span_days, 2),
        "total_pnl": round(total_pnl, 3),
        "mean_per_trade": round(mean_r, 4),
        "std_per_trade": round(std_r, 4),
        "sharpe_per_trade": round(sharpe_per_trade, 3),
        "sharpe_annualized": round(sharpe_annualized, 2),
        "max_drawdown": round(mdd, 3),
        "calmar_ratio": round(calmar, 2) if calmar else None,
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "wl_ratio": round(wl_ratio, 3) if wl_ratio else None,
        "kelly_fraction": round(kelly, 3) if kelly is not None else None,
        "quarter_kelly": round(kelly * 0.25, 3) if kelly is not None else None,
    }


def evaluate():
    r = _okx("/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100")
    fills = r.get("data", [])

    m = compute_metrics(fills)
    m["ts_cst"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    # 诊断标签
    m["verdict"] = []
    if m.get("kelly_fraction") is not None:
        k = m["kelly_fraction"]
        if k <= 0:
            m["verdict"].append("❌ Kelly ≤ 0：当前策略数学上不该交易，立即降仓或暂停")
        elif k < 0.05:
            m["verdict"].append("⚠️ Kelly < 5%：仓位应极小，当前放大是赌博")
        else:
            m["verdict"].append(f"✅ Kelly = {k*100:.1f}%（Quarter = {k*25:.1f}%）")
    if m.get("sharpe_annualized"):
        s = m["sharpe_annualized"]
        if s < 0.5:
            m["verdict"].append(f"❌ Sharpe {s} < 0.5：策略基本无 alpha")
        elif s < 1.0:
            m["verdict"].append(f"⚠️ Sharpe {s} < 1.0：不合格")
        elif s < 2.0:
            m["verdict"].append(f"✅ Sharpe {s} 合格")
        else:
            m["verdict"].append(f"🏆 Sharpe {s} 优秀")
    if m.get("profit_factor") and m["profit_factor"] != "inf":
        pf = m["profit_factor"]
        if pf < 1.0:
            m["verdict"].append(f"❌ Profit Factor {pf}：亏大于赚")
        elif pf < 1.5:
            m["verdict"].append(f"⚠️ Profit Factor {pf}：刚平衡")
        else:
            m["verdict"].append(f"✅ Profit Factor {pf}")

    return m


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()

    while True:
        try:
            m = evaluate()
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(m, indent=2, ensure_ascii=False))
            print(f"\n=== 绩效评估 {m.get('ts_cst')} ===")
            for k in ("sample_size", "total_pnl", "sharpe_annualized", "max_drawdown",
                      "profit_factor", "win_rate", "wl_ratio", "kelly_fraction", "quarter_kelly"):
                print(f"  {k:<22} {m.get(k)}")
            print("  诊断:")
            for v in m.get("verdict", []):
                print(f"    {v}")
        except Exception as e:
            print(f"ERROR: {e}")

        if not args.daemon:
            break
        time.sleep(3600)  # 1h


if __name__ == "__main__":
    main()
