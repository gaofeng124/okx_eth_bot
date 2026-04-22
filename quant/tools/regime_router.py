"""
Regime Router —— 自动切换 GRID_DIRECTION（long / short）

解决 Q2：昨日 22:00-今日 03:00 ETH 先反弹 0.65% 再回落 0.95%，
静态 `GRID_DIRECTION=short` 在反弹段被砸 -$1.95。

设计：
  每 5 分钟评估 3 个信号，**多数投票**决定方向：
  1. 4h K 线 delta：close_now vs close_4h_ago（>+0.3% = 偏多，<-0.3% = 偏空，中间 = 震荡）
  2. Funding rate：> +0.01% = 多头过热偏空，< -0.01% = 空头过热偏多
  3. EMA 对比：15m EMA(20) vs EMA(50)（快>慢 = 多，快<慢 = 空）

  如果 3 信号里 ≥ 2 指向同向，且当前 direction 相反 → 切换：
    - 先检查有无持仓：有 → 等持仓自然关闭（或手动强平）后切换
    - 无持仓 → 立刻 sed .env + pkill 触发 watchdog 重启

  防抖：30 分钟内只允许切换 1 次（避免震荡市来回切）。

用法：
  python -m quant.tools.regime_router           # 单次评估 + 自动执行
  python -m quant.tools.regime_router --dry-run # 只输出决策，不切换
  python -m quant.tools.regime_router --daemon  # 常驻，每 5 min 评估一次
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
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
if not os.environ.get("OKX_API_KEY"):
    load_dotenv("/Users/gaofeng/Documents/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")

MARKER = PROJ / "data" / ".regime_last_switch"


def _sign(ts: str, m: str, p: str, body: str = "") -> str:
    secret = os.environ["OKX_SECRET_KEY"]
    return base64.b64encode(
        hmac.new(secret.encode(), f"{ts}{m}{p}{body}".encode(), hashlib.sha256).digest()
    ).decode()


def _okx_public(path: str) -> dict:
    return httpx.get("https://www.okx.com" + path, timeout=15).json()


def _okx_private(path: str) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    h = {
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": _sign(ts, "GET", path),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "Content-Type": "application/json",
    }
    return httpx.get("https://www.okx.com" + path, headers=h, timeout=15).json()


def evaluate_regime() -> dict[str, Any]:
    """返回投票结果 + 建议方向。"""
    # 信号 1：4h K 线 delta（15m × 16）
    k = _okx_public("/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=15m&limit=16")
    candles = k.get("data", [])
    if len(candles) < 16:
        return {"error": "insufficient_candles"}
    close_now = float(candles[0][4])
    close_4h = float(candles[-1][4])
    delta_4h_pct = (close_now - close_4h) / close_4h * 100
    if delta_4h_pct > 0.3:
        vote_delta = "long"
    elif delta_4h_pct < -0.3:
        vote_delta = "short"
    else:
        vote_delta = "neutral"

    # 信号 2：Funding rate
    fr = _okx_public("/api/v5/public/funding-rate?instId=ETH-USDT-SWAP")
    funding = float(fr["data"][0].get("fundingRate") or 0)
    if funding > 0.0001:
        vote_fr = "short"   # 多头贵，反手做空
    elif funding < -0.0001:
        vote_fr = "long"    # 空头贵，反手做多
    else:
        vote_fr = "neutral"

    # 信号 3：EMA 对比（15m 20周期 vs 50周期）
    k_long = _okx_public("/api/v5/market/candles?instId=ETH-USDT-SWAP&bar=15m&limit=50")
    closes = [float(c[4]) for c in k_long.get("data", [])]
    if len(closes) >= 50:
        closes.reverse()  # 最旧在前
        # 简单 EMA
        def _ema(values, period):
            k = 2 / (period + 1)
            ema = values[0]
            for v in values[1:]:
                ema = v * k + ema * (1 - k)
            return ema
        ema20 = _ema(closes[-30:], 20)
        ema50 = _ema(closes, 50)
        if ema20 > ema50 * 1.001:
            vote_ema = "long"
        elif ema20 < ema50 * 0.999:
            vote_ema = "short"
        else:
            vote_ema = "neutral"
    else:
        vote_ema = "neutral"
        ema20 = ema50 = 0

    votes = [vote_delta, vote_fr, vote_ema]
    long_n = votes.count("long")
    short_n = votes.count("short")

    if long_n >= 2:
        recommended = "long"
    elif short_n >= 2:
        recommended = "short"
    else:
        recommended = "neutral"   # 不切换，保持当前

    return {
        "ts_cst": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "signals": {
            "delta_4h_pct": round(delta_4h_pct, 3),
            "vote_delta": vote_delta,
            "funding_rate": round(funding * 100, 4),
            "vote_fr": vote_fr,
            "ema_20": round(ema20, 2),
            "ema_50": round(ema50, 2),
            "vote_ema": vote_ema,
        },
        "votes": {"long": long_n, "short": short_n, "neutral": votes.count("neutral")},
        "recommended": recommended,
    }


def get_current_direction() -> str:
    """从 .env 读 GRID_DIRECTION。"""
    env_path = PROJ / ".env"
    if not env_path.exists():
        return "unknown"
    for line in env_path.read_text().splitlines():
        if line.startswith("GRID_DIRECTION="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "long"  # 默认


def get_current_position() -> float:
    """查当前实盘持仓。"""
    try:
        r = _okx_private("/api/v5/account/positions?instId=ETH-USDT-SWAP")
        for x in r.get("data", []):
            pos = float(x.get("pos") or 0)
            if pos:
                return pos
    except Exception:
        pass
    return 0.0


def can_switch_now() -> tuple[bool, str]:
    """防抖：30 分钟内只允许切换 1 次。"""
    if not MARKER.exists():
        return True, "no_previous_switch"
    age_sec = time.time() - MARKER.stat().st_mtime
    if age_sec < 1800:
        return False, f"last_switch_{age_sec/60:.1f}min_ago (cooldown 30min)"
    return True, f"last_switch_{age_sec/60:.0f}min_ago"


def apply_switch(new_direction: str, reason: str) -> bool:
    """执行切换：sed .env + 写 marker + pkill。"""
    env_path = PROJ / ".env"
    if not env_path.exists():
        return False
    # sed
    ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
    subprocess.run(
        ["cp", str(env_path), str(env_path) + f".before_regime_switch_{ts}"],
        check=False,
    )
    subprocess.run(
        ["sed", "-i.tmp",
         f"s/^GRID_DIRECTION=.*/GRID_DIRECTION={new_direction}/",
         str(env_path)],
        check=False,
    )
    (PROJ / ".env.tmp").unlink(missing_ok=True)
    # marker
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(f"{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} | {new_direction} | {reason}")
    # pkill（watchdog 5 min 内拉起）
    subprocess.run(["pkill", "-f", "run_strategy.py"], check=False)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只评估不执行")
    parser.add_argument("--daemon", action="store_true", help="常驻每 5 min 评估")
    args = parser.parse_args()

    while True:
        result = evaluate_regime()
        current_dir = get_current_direction()
        pos = get_current_position()
        can_sw, cooldown_reason = can_switch_now()

        print(f"\n=== Regime Router @ {result.get('ts_cst','?')} ===")
        print(f"当前 direction: {current_dir}  当前持仓: {pos}")
        print(f"信号: {json.dumps(result.get('signals',{}), ensure_ascii=False, indent=2)}")
        print(f"投票: {result.get('votes',{})}  建议: {result.get('recommended')}")
        print(f"冷却: {cooldown_reason}")

        if "error" in result:
            print(f"⚠️ {result['error']}")
        else:
            recommended = result["recommended"]
            if recommended == "neutral":
                print(f"→ 保持当前 {current_dir}（无明确信号）")
            elif recommended == current_dir:
                print(f"→ 当前 {current_dir} 符合建议，不切换")
            elif not can_sw:
                print(f"→ 建议切 {recommended} 但冷却中（防抖）")
            elif abs(pos) > 0.01:
                print(f"→ 建议切 {recommended} 但有持仓 {pos}，等持仓自然关闭后再切")
            elif args.dry_run:
                print(f"→ [DRY RUN] 会切换 {current_dir} → {recommended}")
            else:
                reason = f"votes {result['votes']} | {result['signals']}"
                ok = apply_switch(recommended, reason)
                if ok:
                    print(f"✅ 已切换 {current_dir} → {recommended}")
                else:
                    print(f"❌ 切换失败")

        if not args.daemon:
            break
        time.sleep(300)


if __name__ == "__main__":
    main()
