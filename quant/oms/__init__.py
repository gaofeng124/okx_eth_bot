"""Merged oms package."""
from __future__ import annotations

import uuid


class OrderManager:
    """生成欧易 clOrdId（≤32 位字母数字）并跟踪序号。"""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id.replace("-", "")[:8]

    def new_cl_ord_id(self) -> str:
        h = uuid.uuid4().hex[:24]
        return f"{self._run_id}{h}"[:32]


__all__ = ["OrderManager"]
