"""Merged models package."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OrderSide = Literal["buy", "sell"]
OrderType = Literal["limit", "market", "post_only", "ioc", "fok"]

PRICED_ORDER_TYPES: frozenset[str] = frozenset(
    ("limit", "post_only", "ioc", "fok"),
)


def is_priced_order(ord_type: str) -> bool:
    return ord_type in PRICED_ORDER_TYPES


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """策略 → 执行层：统一订单意图（与交易所字段对齐，便于审计与回测）。"""

    inst_id: str
    side: OrderSide
    ord_type: OrderType
    sz: str
    px: str | None = None
    tgt_ccy: str | None = None
    client_order_id: str | None = None
    reason: str | None = None
    features_json: str | None = None
    reduce_only_sell: bool = False
    instrument_type: Literal["spot", "swap"] = "swap"
    td_mode: Literal["cash", "cross", "isolated"] = "isolated"
    pos_side: Literal["net", "long", "short"] | None = None
    leverage: float | None = None
    reduce_only: bool = False
    signal_last: float | None = None
    signal_bid: float | None = None
    signal_ask: float | None = None


__all__ = [
    "OrderIntent",
    "OrderSide",
    "OrderType",
    "PRICED_ORDER_TYPES",
    "is_priced_order",
]
