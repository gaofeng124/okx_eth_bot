"""Merged metrics package."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """进程内指标（线程安全计数器）；可对接 Prometheus 等外部系统。"""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    ticks: int = 0
    signals: int = 0
    fee_gate_rejected: int = 0
    orders_ok: int = 0
    orders_fail: int = 0
    circuit_open_count: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def inc_ticks(self) -> None:
        with self._lock:
            self.ticks += 1

    def inc_signals(self) -> None:
        with self._lock:
            self.signals += 1

    def inc_fee_gate_rejected(self) -> None:
        with self._lock:
            self.fee_gate_rejected += 1

    def inc_orders_ok(self) -> None:
        with self._lock:
            self.orders_ok += 1

    def inc_orders_fail(self) -> None:
        with self._lock:
            self.orders_fail += 1

    def inc_circuit_open(self) -> None:
        with self._lock:
            self.circuit_open_count += 1

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            uptime = time.monotonic() - self.started_at
            return {
                "ticks": self.ticks,
                "signals": self.signals,
                "fee_gate_rejected_count": self.fee_gate_rejected,
                "orders_ok": self.orders_ok,
                "orders_fail": self.orders_fail,
                "circuit_open_count": self.circuit_open_count,
                "uptime_sec": round(uptime, 1),
            }

    def __str__(self) -> str:
        s = self.snapshot()
        return " ".join(f"{k}={v}" for k, v in s.items())


__all__ = ["Metrics"]
