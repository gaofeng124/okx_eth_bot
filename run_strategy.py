#!/usr/bin/env python3
"""本地策略循环入口。

用法：
  python run_strategy.py              # 直接启动（请自行确认 .env）
  python run_strategy.py --live-check  # 启动前校验 STRAT_LIVE / RISK / 密钥等
"""
from __future__ import annotations

import atexit
import fcntl
import os
import sys


def _live_check() -> None:
    from quant.settings import (
        INST_ID,
        OKX_API_KEY,
        OKX_PASSPHRASE,
        OKX_SECRET_KEY,
        RISK_ENABLED,
        RISK_MAX_NOTIONAL_USDT,
        RISK_MAX_ORDER_BASE,
        RISK_SWAP_IGNORE_MAX_CAPS,
        STRAT_LIVE,
    )

    missing: list[str] = []
    if not str(INST_ID).upper().endswith("-SWAP"):
        missing.append("INST_ID=…-SWAP（仅线性永续）")
    if not STRAT_LIVE:
        missing.append("STRAT_LIVE=1")
    if not RISK_ENABLED:
        missing.append("RISK_ENABLED=1")
    is_swap = str(INST_ID).upper().endswith("-SWAP")
    caps_required = not (is_swap and bool(RISK_SWAP_IGNORE_MAX_CAPS))
    if caps_required:
        if not RISK_MAX_NOTIONAL_USDT.strip():
            missing.append("RISK_MAX_NOTIONAL_USDT=数字（限价单笔名义上限，USDT）")
        if not RISK_MAX_ORDER_BASE.strip():
            missing.append(
                "RISK_MAX_ORDER_BASE=数字（单笔 base 上限；永续=张数×ctVal）"
            )
    if not (OKX_API_KEY and OKX_SECRET_KEY and OKX_PASSPHRASE):
        missing.append("OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE")
    if missing:
        print("请在项目目录的 .env 中配置：")
        for line in missing:
            print(" ", line)
        print("\n示例见 .env.example 中「策略下单」一节。")
        sys.exit(1)


def main() -> None:
    if "--live-check" in sys.argv:
        _live_check()
    lock_path = "/Users/gaofeng/Documents/okx_eth_bot/data/logs/run_strategy.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("检测到已有 run_strategy.py 进程在运行，拒绝重复启动。")
        sys.exit(1)

    lock_fp.write(str(os.getpid()))
    lock_fp.flush()

    def _release_lock() -> None:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass

    atexit.register(_release_lock)
    from quant.app.runner import main as run_loop

    run_loop()


if __name__ == "__main__":
    main()
