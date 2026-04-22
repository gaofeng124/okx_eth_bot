"""
时段 alpha 分析（Time-of-day bias）

找出哪些小时/session 策略表现最好、最差。专业量化都有这个：
  - 亚洲 session（CST 09-16 / UTC 01-08）
  - 欧洲 session（CST 16-22 / UTC 08-14）
  - 美国 session（CST 22-05 / UTC 14-21）

每个时段有不同的波动率、流动性、方向偏好。

用 100 笔实盘数据分组统计：
  - 每个 UTC 小时的胜率 / 平均净利 / 笔数
  - 找出 Top 3 / Bottom 3 小时
  - 建议：关闭 Bottom 3 小时的交易

用法：
  python -m quant.tools.time_of_day_analysis
"""
from __future__ import annotations

import os
import time
import hmac
import base64
import hashlib
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv("/root/okx_eth_bot/.env")
if not os.environ.get("OKX_API_KEY"):
    load_dotenv("/Users/gaofeng/Documents/okx_eth_bot/.env")

import httpx

CST = timezone(timedelta(hours=8))
UTC = timezone.utc


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


def session_label(utc_hour):
    if 1 <= utc_hour <= 7:
        return "亚洲"
    if 8 <= utc_hour <= 13:
        return "欧洲"
    return "美国"


def analyze():
    r = _okx("/api/v5/trade/fills-history?instType=SWAP&instId=ETH-USDT-SWAP&limit=100")
    fills = r.get("data", [])

    by_hour = defaultdict(list)
    by_session = defaultdict(list)
    for f in fills:
        ts_ms = int(f.get("ts", 0))
        dt_utc = datetime.fromtimestamp(ts_ms / 1000, UTC)
        hour = dt_utc.hour
        net = float(f.get("fillPnl") or 0) + float(f.get("fee") or 0)
        by_hour[hour].append(net)
        by_session[session_label(hour)].append(net)

    print(f"=== Time-of-Day 分析（100 笔）===\n")

    print("【按 Session】")
    print(f"{'Session':<8} {'笔数':<6} {'胜率':<8} {'总净利':<12} {'avg/笔':<10}")
    for sess in ["亚洲", "欧洲", "美国"]:
        pnls = by_session.get(sess, [])
        if not pnls:
            continue
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls)
        avg = total / len(pnls)
        tag = "✅" if total > 0 else "❌"
        print(f"  {tag} {sess:<6} {len(pnls):<6} {win_rate*100:<7.1f}% {total:+.3f}      {avg:+.4f}")

    print(f"\n【按 UTC 小时】")
    print(f"{'UTC 时':<8} {'CST 时':<8} {'笔数':<6} {'胜率':<8} {'总净利':<12} {'avg/笔':<10}")
    # 排序：按 total PnL 降序
    rows = []
    for hour in sorted(by_hour.keys()):
        pnls = by_hour[hour]
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / len(pnls) if pnls else 0
        avg = total / len(pnls) if pnls else 0
        cst_h = (hour + 8) % 24
        rows.append((hour, cst_h, len(pnls), win_rate, total, avg))

    for hour, cst_h, n, wr, total, avg in rows:
        tag = "✅" if total > 0 else "❌"
        print(f"  {tag} {hour:02d}:00    {cst_h:02d}:00    {n:<6} {wr*100:<7.1f}% {total:+.3f}      {avg:+.4f}")

    # Top/Bottom 3
    sorted_by_total = sorted(rows, key=lambda x: x[4])
    print(f"\n【最差 3 个 UTC 小时】（建议关闭交易）")
    for hour, cst_h, n, wr, total, avg in sorted_by_total[:3]:
        print(f"  UTC {hour:02d}（CST {cst_h:02d}）: {n} 笔 胜率{wr*100:.0f}% 总 {total:+.3f}")
    print(f"\n【最佳 3 个 UTC 小时】")
    for hour, cst_h, n, wr, total, avg in sorted_by_total[-3:][::-1]:
        print(f"  UTC {hour:02d}（CST {cst_h:02d}）: {n} 笔 胜率{wr*100:.0f}% 总 {total:+.3f}")

    # 建议
    bad_hours = [r[0] for r in sorted_by_total if r[4] < 0 and r[2] >= 3]
    if bad_hours:
        print(f"\n🔧 建议：设 DISABLED_UTC_HOURS = {bad_hours}")
        print(f"   这些时段策略显著亏钱，应跳过开仓")


if __name__ == "__main__":
    analyze()
