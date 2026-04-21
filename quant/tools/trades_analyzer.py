"""
Taker Flow 分析器 —— 真正的量化 alpha 因子。

订阅 OKX WS `trades` channel，实时聚合：
  1. Aggressor Ratio: 主动买量 / (主动买量 + 主动卖量) over N 秒窗口
     - > 0.6 多头压倒性进攻 → 做多有利
     - < 0.4 空头压倒性进攻 → 做空有利
     - ~0.5 均衡
  2. Large Trade Tracking: 检测 > 10 ETH 的单笔成交
     - 累计 60s 内的大单买/卖 → 鲸鱼活动
  3. CVD (Cumulative Volume Delta): 累计买卖差

这些是**信息优势信号**：散户看不到主动方向，但我们可以。

用法：
    analyzer = TakerFlowAnalyzer(instId="ETH-USDT-SWAP")
    await analyzer.start()  # 后台协程订阅 WS
    ar = analyzer.aggressor_ratio(window_sec=60)
    whales = analyzer.large_trades_recent(window_sec=60, min_eth=10)
    cvd = analyzer.cvd
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import websockets


@dataclass
class Trade:
    ts_ms: int
    side: str       # "buy" = 主动买（吃 ask），"sell" = 主动卖（吃 bid）
    sz: float       # 成交张数
    px: float       # 成交价
    eth_vol: float  # 换算为 ETH（OKX ETH-USDT-SWAP ctVal=0.1）

    @property
    def notional(self) -> float:
        return self.eth_vol * self.px


class TakerFlowAnalyzer:
    """实时订阅 OKX public `trades` 频道，维护滚动统计。"""

    DEFAULT_WINDOW = 300   # 保留 5 分钟成交流
    DEFAULT_WHALE_ETH = 10.0   # >10 ETH 算鲸鱼

    def __init__(
        self,
        inst_id: str = "ETH-USDT-SWAP",
        ct_val: float = 0.1,
        ws_url: str = "wss://ws.okx.com:443/ws/v5/public",
        window_sec: int = DEFAULT_WINDOW,
        whale_eth: float = DEFAULT_WHALE_ETH,
    ):
        self.inst_id = inst_id
        self.ct_val = ct_val
        self.ws_url = ws_url
        self.window_sec = window_sec
        self.whale_eth = whale_eth
        # 滚动窗口
        self._trades: deque[Trade] = deque(maxlen=50000)  # 限流 50k 避免爆内存
        self._cvd: float = 0.0  # 累计（重启归零）
        self._last_msg_ts: float = 0.0
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None

    # ══════════════════════════════════════════════════════════════════
    # WS 订阅
    # ══════════════════════════════════════════════════════════════════

    async def _ws_loop(self) -> None:
        """主循环，断线自动重连。"""
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    sub = json.dumps({
                        "op": "subscribe",
                        "args": [{"channel": "trades", "instId": self.inst_id}],
                    })
                    await ws.send(sub)
                    async for msg in ws:
                        if not self._running:
                            break
                        self._on_msg(msg)
            except Exception:
                await asyncio.sleep(3)  # 重连退避

    def _on_msg(self, msg: str) -> None:
        try:
            d = json.loads(msg)
            data = d.get("data")
            if not data:
                return
            for item in data:
                side_raw = item.get("side", "").lower()  # "buy" | "sell"
                ts_ms = int(item.get("ts", 0))
                sz = float(item.get("sz") or 0)      # 张数
                px = float(item.get("px") or 0)
                if sz <= 0 or px <= 0:
                    continue
                eth = sz * self.ct_val
                tr = Trade(ts_ms=ts_ms, side=side_raw, sz=sz, px=px, eth_vol=eth)
                self._trades.append(tr)
                # CVD 累加：buy=+，sell=-
                if side_raw == "buy":
                    self._cvd += eth
                elif side_raw == "sell":
                    self._cvd -= eth
                self._last_msg_ts = time.time()
            # 清掉窗口外的
            self._prune()
        except Exception:
            pass  # WS 偶尔发 pong 等非数据包，静默忽略

    def _prune(self) -> None:
        cutoff_ms = int((time.time() - self.window_sec) * 1000)
        while self._trades and self._trades[0].ts_ms < cutoff_ms:
            self._trades.popleft()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    # ══════════════════════════════════════════════════════════════════
    # 查询 API（grid_pro 用）
    # ══════════════════════════════════════════════════════════════════

    def aggressor_ratio(self, window_sec: int = 60) -> Optional[float]:
        """
        滚动窗口的主动买比例。
        返回 [0, 1] 或 None（窗口内无数据）。
        > 0.6 多头强；< 0.4 空头强；~0.5 均衡。
        """
        cutoff = int((time.time() - window_sec) * 1000)
        buy_vol = 0.0
        sell_vol = 0.0
        for tr in reversed(self._trades):
            if tr.ts_ms < cutoff:
                break
            if tr.side == "buy":
                buy_vol += tr.eth_vol
            elif tr.side == "sell":
                sell_vol += tr.eth_vol
        total = buy_vol + sell_vol
        if total <= 0:
            return None
        return buy_vol / total

    def large_trades_recent(
        self, window_sec: int = 60, min_eth: Optional[float] = None,
    ) -> dict:
        """
        滚动窗口的大单统计。
        返回 {buy_count, sell_count, buy_eth, sell_eth, net_eth}
        """
        min_eth = min_eth or self.whale_eth
        cutoff = int((time.time() - window_sec) * 1000)
        stats = {"buy_count": 0, "sell_count": 0, "buy_eth": 0.0, "sell_eth": 0.0}
        for tr in reversed(self._trades):
            if tr.ts_ms < cutoff:
                break
            if tr.eth_vol < min_eth:
                continue
            if tr.side == "buy":
                stats["buy_count"] += 1
                stats["buy_eth"] += tr.eth_vol
            elif tr.side == "sell":
                stats["sell_count"] += 1
                stats["sell_eth"] += tr.eth_vol
        stats["net_eth"] = stats["buy_eth"] - stats["sell_eth"]
        return stats

    @property
    def cvd(self) -> float:
        """Cumulative Volume Delta（会话内累计买卖净差，单位 ETH）。"""
        return self._cvd

    def cvd_recent(self, window_sec: int = 300) -> float:
        """滚动窗口 CVD，不累计会话。"""
        cutoff = int((time.time() - window_sec) * 1000)
        delta = 0.0
        for tr in reversed(self._trades):
            if tr.ts_ms < cutoff:
                break
            if tr.side == "buy":
                delta += tr.eth_vol
            elif tr.side == "sell":
                delta -= tr.eth_vol
        return delta

    @property
    def health(self) -> dict:
        """连接健康 —— grid_pro 用此判断数据是否可信。"""
        age = time.time() - self._last_msg_ts if self._last_msg_ts else 999
        return {
            "trades_buffered": len(self._trades),
            "last_msg_age_sec": round(age, 1),
            "healthy": age < 10,  # 10s 内有数据才算健康
        }


# ══════════════════════════════════════════════════════════════════
# 独立测试入口
# ══════════════════════════════════════════════════════════════════

async def _test() -> None:
    """cd /root/okx_eth_bot && .venv/bin/python -m quant.tools.trades_analyzer"""
    a = TakerFlowAnalyzer()
    await a.start()
    for i in range(6):
        await asyncio.sleep(10)
        ar60 = a.aggressor_ratio(60)
        ar10 = a.aggressor_ratio(10)
        w = a.large_trades_recent(60)
        print(
            f"[{i*10}s] aggressor_60s={ar60} 10s={ar10} | "
            f"large(60s): buy{w['buy_count']}({w['buy_eth']:.1f}E) sell{w['sell_count']}({w['sell_eth']:.1f}E) net={w['net_eth']:+.1f}E | "
            f"CVD={a.cvd:+.2f}E | health={a.health}"
        )
    await a.stop()


if __name__ == "__main__":
    asyncio.run(_test())
