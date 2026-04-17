from __future__ import annotations

from typing import Any, Protocol

from quant.models import OrderIntent


class TickStrategy(Protocol):
    """tick → 订单意图（WS 或 REST 轮询均可；可选附带 K 线增强分析）。"""

    def on_tick(
        self,
        *,
        last: float,
        bid: float,
        ask: float,
        market_context: dict[str, Any] | None = None,
    ) -> OrderIntent | None: ...
