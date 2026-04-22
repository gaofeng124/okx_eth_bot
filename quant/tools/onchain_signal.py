"""
链上信号供给器（缓存版，供 strategy 高频读取）

问题：onchain.py 的 API 调用慢（每次 2-5 秒）且有配额（100k/日）。
       strategy on_tick 每秒多次，不能直接调 API。

方案：后台进程每 10 分钟刷一次主流交易所净流入，写入 JSON 文件。
       strategy 读文件即可（微秒级）。

Alpha 逻辑：
  - 8 个主流交易所近 24h 净流入总和
  - 净流入 > +5000 ETH（约 1200 万美元）= 卖压大，long 不利 → 信号 -1
  - 净流出 > +5000 ETH = 囤币信号，long 有利 → 信号 +1
  - 中间值线性映射

用法：
  python -m quant.tools.onchain_signal           # 单次刷
  python -m quant.tools.onchain_signal --daemon  # 每 10min
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")

SIGNAL_FILE = PROJ / "data" / ".onchain_signal.json"
REFRESH_INTERVAL = 600  # 10 min
# 阈值（ETH）
FULL_BEARISH_NETFLOW = 5000   # 净流入 >= 此值 → 信号 -1（对 long）
FULL_BULLISH_NETFLOW = -5000  # 净流出 >= 此值 → 信号 +1


def compute_signal():
    """聚合 8 大交易所净流入，返回归一化信号 [-1, +1]"""
    from quant.tools.onchain import EXCHANGE_WALLETS, exchange_flow_recent

    total_net = 0.0  # 正 = 净流入（卖压），负 = 净流出（囤币）
    per_exchange = {}
    for name in EXCHANGE_WALLETS.keys():
        try:
            # block_window = 7200（≈24h @ 12s/block）
            data = exchange_flow_recent(name, block_window=7200)
            net = float(data.get("net_flow_eth", 0))
            per_exchange[name] = round(net, 2)
            total_net += net
        except Exception as e:
            per_exchange[name] = f"err: {e}"
        time.sleep(0.25)  # 避免 5 req/s 超限

    # 归一化到 [-1, +1]
    if total_net >= FULL_BEARISH_NETFLOW:
        signal = -1.0
    elif total_net <= FULL_BULLISH_NETFLOW:
        signal = 1.0
    else:
        # 线性：total_net 0 → 0；5000 → -1；-5000 → +1
        signal = -total_net / FULL_BEARISH_NETFLOW
        signal = max(-1.0, min(1.0, signal))

    return {
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST"),
        "ts_unix": time.time(),
        "total_net_flow_eth_24h": round(total_net, 2),
        "per_exchange": per_exchange,
        "signal": round(signal, 3),
        "interpretation": (
            "重卖压" if signal < -0.5 else
            "轻卖压" if signal < -0.15 else
            "中性" if abs(signal) <= 0.15 else
            "轻囤币" if signal < 0.5 else
            "重囤币"
        ),
    }


def write_signal(sig_data):
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(json.dumps(sig_data, indent=2, ensure_ascii=False))


def read_signal_cached():
    """strategy 调用此函数读缓存。过期返回 None。"""
    if not SIGNAL_FILE.exists():
        return None
    try:
        data = json.loads(SIGNAL_FILE.read_text())
        age = time.time() - data.get("ts_unix", 0)
        if age > 1800:  # 30min 过期
            return None
        return data
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", action="store_true")
    args = p.parse_args()

    while True:
        try:
            sig = compute_signal()
            write_signal(sig)
            print(f"[{sig['ts']}] netflow_24h={sig['total_net_flow_eth_24h']:+.0f} ETH "
                  f"signal={sig['signal']:+.2f} ({sig['interpretation']})")
        except Exception as e:
            print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] ERROR: {e}")

        if not args.daemon:
            break
        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
