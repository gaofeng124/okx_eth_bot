"""Merged reconcile: pending log + stale cancel."""
from __future__ import annotations

import time
from typing import Any

from quant.exchange import OKXRestClient
from quant.logging_config import get_logger

log = get_logger(__name__)


def log_pending_orders(client: OKXRestClient, inst_id: str) -> dict[str, Any]:
    """
    拉取当前挂单（REST GET orders-pending）。
    附带最老挂单年龄，便于观察「挂单堆积」与限时撤销策略是否生效。
    """
    now_ms = int(time.time() * 1000)
    data = client.orders_pending(inst_id)
    rows = data.get("data") or []
    ages: list[float] = []
    for o in rows:
        if not isinstance(o, dict):
            continue
        try:
            ages.append((now_ms - int(o.get("cTime") or 0)) / 1000.0)
        except (TypeError, ValueError):
            continue
    oldest = max(ages) if ages else 0.0
    log.info(
        "[对账] 挂单数=%s | 最老挂单≈%.0fs | instId=%s",
        len(rows),
        oldest,
        inst_id,
    )
    return data


def cancel_stale_pending_orders(
    client: OKXRestClient,
    inst_id: str,
    *,
    max_age_sec: float,
) -> int:
    """
    撤销创建时间早于 (now - max_age_sec) 的挂单（永续）。
    返回成功撤销笔数（单次扫描）。
    """
    if max_age_sec <= 0:
        return 0
    now_ms = int(time.time() * 1000)
    data = client.orders_pending(inst_id)
    rows = data.get("data") or []
    cancelled = 0
    for o in rows:
        if not isinstance(o, dict):
            continue
        try:
            ctime = int(o.get("cTime") or 0)
        except (TypeError, ValueError):
            continue
        age_sec = (now_ms - ctime) / 1000.0
        if age_sec <= max_age_sec:
            continue
        oid = o.get("ordId")
        if not oid:
            continue
        try:
            try:
                from quant import settings as _cfg

                td_mode = str(getattr(_cfg, "LEV5_TD_MODE", "isolated") or "isolated")
            except Exception:
                td_mode = "isolated"
            client.cancel_order(inst_id=inst_id, ord_id=str(oid), td_mode=td_mode)
            cancelled += 1
            log.info(
                "[挂单] 已撤销超时限价单 | ordId=%s side=%s px=%s 已挂≈%.0fs",
                oid,
                o.get("side"),
                o.get("px"),
                age_sec,
            )
        except Exception as e:
            log.warning("[挂单] 撤销失败 | ordId=%s | %s", oid, e)
    return cancelled


__all__ = ["log_pending_orders", "cancel_stale_pending_orders"]
