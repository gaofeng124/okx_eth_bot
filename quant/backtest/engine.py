"""
Backtest 引擎骨架（Phase I 基础设施）

目标：任何策略 / 参数改动 → 先在历史数据上跑 → 看 Sharpe / PnL 曲线 → 合格才 deploy。

设计哲学：
  - **不** 重新发明轮子：复用 strategy code（同一套 on_tick 逻辑）
  - 输入：历史 15m/1m K 线 + 模拟 OCO 撮合
  - 输出：每笔 fills + 累计 PnL 曲线 + Sharpe / MDD / Kelly

版本 1（这个文件）：skeleton
  - load_candles(instId, days)：从 OKX 拉历史 K 线
  - SimulatedExchange：模拟撮合（post_only 能否 fill / OCO 触发）
  - run_backtest(strategy, candles)：跑一遍
  - compute_report(fills)：报告

版本 2（下周）：
  - 细化撮合模型（slippage / 部分成交）
  - 多策略并行
  - 参数扫描（parameter sweep）

用法：
  python -m quant.backtest.engine --strategy grid_pro --days 7
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx

CST = timezone(timedelta(hours=8))
PROJ = Path("/root/okx_eth_bot")
if not PROJ.exists():
    PROJ = Path("/Users/gaofeng/Documents/okx_eth_bot/.claude/worktrees/eager-varahamihira-9717cc")


@dataclass
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SimFill:
    ts_ms: int
    side: str  # buy / sell
    px: float
    sz: float
    pnl: float = 0.0  # 平仓时赋值
    fee: float = 0.0


def load_candles(inst_id: str = "ETH-USDT-SWAP", bar: str = "15m", limit: int = 300) -> list[Candle]:
    """从 OKX 拉历史 K 线（OKX REST 支持最多 300 根/请求）。"""
    url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst_id}&bar={bar}&limit={limit}"
    r = httpx.get(url, timeout=15).json()
    candles = []
    for c in r.get("data", []):
        candles.append(Candle(
            ts_ms=int(c[0]),
            open=float(c[1]),
            high=float(c[2]),
            low=float(c[3]),
            close=float(c[4]),
            volume=float(c[5]),
        ))
    # 升序（老在前）
    candles.sort(key=lambda x: x.ts_ms)
    return candles


class SimExchange:
    """简化撮合引擎。
    post_only 挂单：
      - buy: 当 bar.low <= px 时 fill
      - sell: 当 bar.high >= px 时 fill
    OCO 止盈止损：
      - tp 触发：价格达 tp_px
      - sl 触发：价格达 sl_px
    两者互斥，谁先谁赢。
    假设：maker 手续费 2bps（-0.0002 × notional）
    """
    MAKER_FEE_RATE = 0.0002

    def __init__(self, ct_val: float = 0.1):
        self.ct_val = ct_val
        self.pending_orders: list[dict] = []
        self.fills: list[SimFill] = []
        self.position: float = 0.0     # 正多负空
        self.position_avg: float = 0.0

    def place_order(self, side: str, px: float, sz: float, tag: str = ""):
        self.pending_orders.append({"side": side, "px": px, "sz": sz, "tag": tag})

    def on_candle(self, candle: Candle):
        """处理一个 K 线：检查挂单是否 fill。"""
        filled = []
        remaining = []
        for od in self.pending_orders:
            filled_ok = False
            if od["side"] == "buy" and candle.low <= od["px"]:
                filled_ok = True
            elif od["side"] == "sell" and candle.high >= od["px"]:
                filled_ok = True
            if filled_ok:
                self._apply_fill(candle.ts_ms, od["side"], od["px"], od["sz"])
                filled.append(od)
            else:
                remaining.append(od)
        self.pending_orders = remaining
        return filled

    def _apply_fill(self, ts_ms: int, side: str, px: float, sz: float):
        notional = sz * self.ct_val * px
        fee = -abs(notional * self.MAKER_FEE_RATE)
        pnl = 0.0
        delta = sz if side == "buy" else -sz
        # 持仓平仓判断
        if (self.position > 0 and delta < 0) or (self.position < 0 and delta > 0):
            # 平仓：算 pnl
            close_sz = min(abs(delta), abs(self.position))
            pnl_px_diff = (px - self.position_avg) if self.position > 0 else (self.position_avg - px)
            pnl = pnl_px_diff * close_sz * self.ct_val
        self.position += delta
        if self.position == 0:
            self.position_avg = 0
        elif (self.position > 0 and delta > 0) or (self.position < 0 and delta < 0):
            # 加仓：重算 avg
            self.position_avg = (self.position_avg * (self.position - delta) + px * delta) / self.position if self.position else px
        self.fills.append(SimFill(ts_ms=ts_ms, side=side, px=px, sz=sz, pnl=pnl, fee=fee))


def simple_grid_strategy(candles: list[Candle], spacing_pct: float = 0.003, tp_mult: float = 1.5, sz: float = 1.0) -> list[SimFill]:
    """简化版 grid 策略用于 backtest 测试。"""
    ex = SimExchange()
    # 每 4 根 15m K 线重新 center
    center = candles[0].close
    recenter_every = 4
    ctr = 0
    for i, c in enumerate(candles):
        # 每 4 根重 center（若无持仓）
        if ctr % recenter_every == 0 and ex.position == 0:
            center = c.close
            # 清旧挂单
            ex.pending_orders = []
            # 挂 3 档 buy
            for lvl in range(1, 4):
                buy_px = center * (1 - spacing_pct * lvl)
                ex.place_order("buy", buy_px, sz, f"L{lvl}")
        # 处理 K 线
        filled = ex.on_candle(c)
        # fill 后挂 TP
        for od in filled:
            if od["side"] == "buy" and ex.position > 0:
                tp_px = od["px"] * (1 + spacing_pct * tp_mult)
                ex.place_order("sell", tp_px, od["sz"], "TP")
        ctr += 1
    return ex.fills


def compute_report(fills: list[SimFill]) -> dict:
    """算 Sharpe / MDD / PnL 曲线等。"""
    if not fills:
        return {"error": "no_fills"}
    nets = [f.pnl + f.fee for f in fills]
    cum = []
    run = 0
    for p in nets:
        run += p
        cum.append(run)
    peak = cum[0]
    mdd = 0
    for v in cum:
        if v > peak:
            peak = v
        if peak - v > mdd:
            mdd = peak - v
    mean = sum(nets) / len(nets)
    import math
    std = math.sqrt(sum((p - mean) ** 2 for p in nets) / max(len(nets) - 1, 1)) if len(nets) > 1 else 0
    sharpe = mean / std if std > 0 else 0
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p < 0]
    return {
        "fills": len(fills),
        "total_pnl": round(run, 3),
        "sharpe_per_trade": round(sharpe, 3),
        "max_drawdown": round(mdd, 3),
        "win_rate": round(len(wins) / len(nets), 3),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bar", default="15m")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--spacing", type=float, default=0.003)
    p.add_argument("--tp_mult", type=float, default=1.5)
    p.add_argument("--sz", type=float, default=1.0)
    args = p.parse_args()

    print(f"=== Backtest 骨架 ===")
    print(f"拉取 {args.limit} 根 {args.bar} 历史 K 线...")
    candles = load_candles(bar=args.bar, limit=args.limit)
    if not candles:
        print("❌ 无数据")
        return
    first_t = datetime.fromtimestamp(candles[0].ts_ms / 1000, CST).strftime("%Y-%m-%d %H:%M")
    last_t = datetime.fromtimestamp(candles[-1].ts_ms / 1000, CST).strftime("%Y-%m-%d %H:%M")
    print(f"数据范围: {first_t} 到 {last_t}（{len(candles)} 根）")

    print(f"\n跑 simple_grid 策略（spacing={args.spacing*10000:.0f}bps TP×{args.tp_mult} sz={args.sz}）...")
    fills = simple_grid_strategy(candles, args.spacing, args.tp_mult, args.sz)
    report = compute_report(fills)
    print(f"\n=== 报告 ===")
    for k, v in report.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
