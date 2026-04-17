"""
统一日志：后续可换 JSON / structlog，业务代码只拿 logger 名。

环境变量 LOG_LEVEL：
- INFO：默认，能看到 [行情][策略][审计][执行][结果][指标][对账] 等关键步骤。
- DEBUG：额外输出策略内部「为何本笔不下单」（价差、波动、预热等），量较大。

LOG_TO_FILE=1（默认）时追加写入 DATA_DIR/logs/quant.log（10MB×5 轮转），便于复盘。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from quant.settings import DATA_DIR, DETAILED_DAILY_LOG, LOG_LEVEL, LOG_TO_FILE


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    root = logging.getLogger()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        root.addHandler(h)
        root.setLevel(level)
    else:
        root.setLevel(level)

    if not LOG_TO_FILE:
        return
    # 详细按日日志开启时，不再写 quant.log 轮转文件（避免与 daily JSONL 重复）
    if DETAILED_DAILY_LOG:
        return
    log_path = Path(DATA_DIR) / "logs" / "quant.log"
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler):
            try:
                if Path(h.baseFilename).resolve() == log_path.resolve():
                    return
            except (OSError, ValueError, AttributeError):
                continue
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        str(log_path),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def brief_okx_order_response(resp: dict[str, Any]) -> str:
    """从 OKX place-order 响应里抽取订单号等关键字段（原 logging_support）。"""
    data = resp.get("data")
    if not data:
        return f"code={resp.get('code')} msg={resp.get('msg')!r}"
    row = data[0] if isinstance(data, list) else data
    if not isinstance(row, dict):
        return str(row)[:200]
    parts: list[str] = []
    for k in ("ordId", "clOrdId", "tag", "sCode", "sMsg"):
        if k in row and row[k] not in (None, ""):
            parts.append(f"{k}={row[k]}")
    return "; ".join(parts) if parts else str(row)[:200]
