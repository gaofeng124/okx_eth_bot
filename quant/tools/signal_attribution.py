"""
信号贡献度分析（Information Coefficient）—— 用数据砍掉噪声

核心问题："我们加了 11 维信号，到底哪些真赚钱？"

方法：
  对近 N 笔 fills，每笔关联当时的各信号值，计算：
  - IC（信息系数）= corr(signal, forward_return)
  - IC > 0.1 = 有效 alpha（应保留且加权）
  - 0.05 < IC < 0.1 = 边际有效（保留但小权重）
  - IC < 0.05 = 噪声（删掉）

简化版：因为历史信号值不存档，用"**当前信号快照 + 后续 K 线走势**"近似。
每 15s 记录一份信号快照 → 15/30/60 分钟后对比走势 → 算 IC。

用法：
  python -m quant.tools.signal_attribution --sample      # 记录一次快照
  python -m quant.tools.signal_attribution --analyze     # 分析已有快照
  python -m quant.tools.signal_attribution --daemon      # 常驻，每 15s 采样

输出：data/signal_ic_report.json（IC 按信号排序）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
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

SNAPS = PROJ / "data" / "signal_snapshots.jsonl"
REPORT = PROJ / "data" / "signal_ic_report.json"


def collect_snapshot():
    """收集当前所有信号 + 当前 ETH 价格。"""
    snap = {
        "ts": time.time(),
        "ts_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        tkr = httpx.get("https://www.okx.com/api/v5/market/ticker?instId=ETH-USDT-SWAP", timeout=10).json()
        snap["px"] = float(tkr["data"][0]["last"])
    except Exception as e:
        snap["px_err"] = str(e)
        return snap

    # 各信号文件
    signal_files = {
        "onchain": "data/.onchain_signal.json",
        "funding_arb": "data/.funding_arb_signal.json",
        "orderbook": "data/.orderbook_signal.json",
        "cross_asset": "data/.cross_asset_signal.json",
        "strategy_pool": "data/.strategy_pool.json",
    }
    for name, path in signal_files.items():
        p = PROJ / path
        if p.exists():
            try:
                d = json.loads(p.read_text())
                snap[name] = d.get("signal") or d.get("time_weighted_signal")
            except Exception:
                pass

    # 盘口 book_imbalance 即时（用 OKX books5）
    try:
        r = httpx.get("https://www.okx.com/api/v5/market/books?instId=ETH-USDT-SWAP&sz=5", timeout=5).json()
        d = r["data"][0]
        bid_v = sum(float(b[1]) for b in d["bids"])
        ask_v = sum(float(a[1]) for a in d["asks"])
        if bid_v + ask_v > 0:
            snap["book_imb"] = (bid_v - ask_v) / (bid_v + ask_v)
    except Exception:
        pass

    return snap


def save_snapshot(snap):
    SNAPS.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPS, "a") as f:
        f.write(json.dumps(snap) + "\n")


def load_snapshots(max_age_hours=24):
    if not SNAPS.exists():
        return []
    cutoff = time.time() - max_age_hours * 3600
    out = []
    with open(SNAPS) as f:
        for line in f:
            try:
                d = json.loads(line)
                if d.get("ts", 0) >= cutoff:
                    out.append(d)
            except Exception:
                continue
    return out


def compute_ic(snapshots, horizon_sec=900):
    """计算每个信号对 forward return（horizon 内价格变化）的 Pearson 相关性。"""
    # 配对：每个 snapshot → 找 horizon 秒后的 snapshot，算 return
    pairs = []
    by_ts = sorted(snapshots, key=lambda x: x["ts"])
    for i, s1 in enumerate(by_ts):
        # 找 horizon 秒后的 snapshot
        target_ts = s1["ts"] + horizon_sec
        for j in range(i + 1, len(by_ts)):
            if by_ts[j]["ts"] >= target_ts:
                s2 = by_ts[j]
                if s1.get("px") and s2.get("px"):
                    fwd_ret = (s2["px"] - s1["px"]) / s1["px"]
                    pairs.append((s1, fwd_ret))
                break

    if not pairs:
        return {}

    # 收集每个信号值 + 对应的 forward return
    signal_names = ["book_imb", "onchain", "funding_arb", "orderbook", "cross_asset"]
    result = {}
    for sig_name in signal_names:
        xs = []
        ys = []
        for snap, fwd_ret in pairs:
            v = snap.get(sig_name)
            if isinstance(v, (int, float)):
                xs.append(v)
                ys.append(fwd_ret)
        if len(xs) < 10:
            result[sig_name] = {"n": len(xs), "ic": None, "note": "样本不足"}
            continue
        # Pearson
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if dx == 0 or dy == 0:
            result[sig_name] = {"n": len(xs), "ic": None, "note": "方差为 0"}
            continue
        ic = num / (dx * dy)
        # 评级
        abs_ic = abs(ic)
        if abs_ic > 0.1:
            grade = "✅ 有效 alpha（保留 + 加权）"
        elif abs_ic > 0.05:
            grade = "⚠️ 边际（保留 + 小权重）"
        else:
            grade = "❌ 噪声（建议删除）"
        result[sig_name] = {
            "n": len(xs),
            "ic": round(ic, 4),
            "abs_ic": round(abs_ic, 4),
            "grade": grade,
        }
    return result


def analyze():
    snaps = load_snapshots(24)
    if len(snaps) < 20:
        print(f"❌ 样本不足（{len(snaps)}）。建议先用 --daemon 采样 1-2 小时后再分析")
        return
    print(f"\n=== 信号贡献度分析 ({len(snaps)} 个 snapshot) ===")
    for horizon_min in [15, 30, 60]:
        print(f"\n【预测窗口 {horizon_min} 分钟】")
        ics = compute_ic(snaps, horizon_min * 60)
        # 按 abs_ic 降序
        sorted_sigs = sorted(
            ics.items(),
            key=lambda kv: (kv[1].get("abs_ic") or 0),
            reverse=True,
        )
        for name, info in sorted_sigs:
            ic = info.get("ic")
            if ic is None:
                print(f"  {name:<15} n={info['n']:<4} {info.get('note')}")
            else:
                print(f"  {name:<15} n={info['n']:<4} IC={ic:+.4f}  {info['grade']}")

    # 写报告
    report = {
        "ts_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "n_snapshots": len(snaps),
        "by_horizon": {
            "15min": compute_ic(snaps, 900),
            "30min": compute_ic(snaps, 1800),
            "60min": compute_ic(snaps, 3600),
        },
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n报告已写 {REPORT}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", action="store_true", help="单次采样")
    p.add_argument("--analyze", action="store_true", help="分析已有数据")
    p.add_argument("--daemon", action="store_true", help="常驻每 15s 采样")
    args = p.parse_args()

    if args.analyze:
        analyze()
        return

    if args.sample or args.daemon:
        while True:
            try:
                s = collect_snapshot()
                save_snapshot(s)
                print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] snap: px={s.get('px')} "
                      f"book_imb={s.get('book_imb')} onchain={s.get('onchain')} "
                      f"orderbook={s.get('orderbook')} cross_asset={s.get('cross_asset')}")
            except Exception as e:
                print(f"ERROR: {e}")
            if not args.daemon:
                break
            time.sleep(15)


if __name__ == "__main__":
    main()
