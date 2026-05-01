"""
按交易日分类的离线详细日志（JSON Lines，UTF-8）。

根目录: {DATA_DIR}/logs/daily/{YYYY-MM-DD}/

文件说明（每日一套，日期为本地日历日）:
  - market.jsonl     逐笔行情摘要：last/bid/ask/mid、点差、盘口摘要、库存摘要、tick 结果
  - analysis.jsonl   策略侧分析快照（网格状态、regime、波动率等）
  - decisions.jsonl  决策与门控：意图产生、保证金/停机/回撤拦截、干跑、发单入口
  - execution.jsonl  执行层：OMS 提交、成功/失败、延迟、熔断相关
  - system.jsonl     会话级：启动配置、清理旧日志记录、异常摘要

环境变量（见 quant/settings.py）:
  DETAILED_DAILY_LOG=1          总开关（默认开）
  DETAILED_LOG_PURGE_LEGACY=1 启动时删除 data/logs 下旧 *.log / quant.log.* / live_*.log（默认开）
  DETAILED_LOG_TICK_MIN_SEC=0 行情 tick 写入最小间隔（秒，0=每笔都写）
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from quant.settings import (
    DATA_DIR,
    DETAILED_DAILY_LOG,
    DETAILED_LOG_PURGE_LEGACY,
    DETAILED_LOG_TICK_MIN_SEC,
)

_lock = threading.Lock()
_run_id: str = ""
_date_str: str = ""
_base_dir: Path | None = None
_last_tick_wall: float = 0.0

_README_LINES = """# 当日详细日志（离线分析）

本目录按「本地日历日」分文件夹，路径形如:
  {data_dir}/logs/daily/{{YYYY-MM-DD}}/

文件类型:
  | 文件名           | 内容 |
  |-----------------|------|
  | market.jsonl    | 每笔 tick：价格、点差、盘口摘要、库存摘要、策略是否产出意图、结果标签 |
  | analysis.jsonl  | 策略周期性/事件性分析快照（如 grid 状态机） |
  | decisions.jsonl | 决策链路：意图字段、风控门控拒绝、干跑、拦截原因 |
  | execution.jsonl | REST 执行：受理/拒绝/异常、延迟毫秒 |
  | system.jsonl    | 会话启动、旧日志清理记录、配置指纹等 |

每行一条 JSON，便于 jq / pandas 分析。超大字段（如完整 order_book）会做截断摘要。
""".format(data_dir="{DATA_DIR}")


def _today_str() -> str:
    return date.today().isoformat()


def _ensure_day_dir() -> Path:
    global _date_str, _base_dir
    d = _today_str()
    if _date_str != d or _base_dir is None:
        _date_str = d
        _base_dir = Path(DATA_DIR) / "logs" / "daily" / d
        _base_dir.mkdir(parents=True, exist_ok=True)
        readme = _base_dir / "README.txt"
        if not readme.exists():
            txt = _README_LINES.replace("{DATA_DIR}", str(Path(DATA_DIR).resolve()))
            readme.write_text(txt, encoding="utf-8")
    assert _base_dir is not None
    return _base_dir


def purge_legacy_root_logs() -> list[str]:
    """删除 data/logs 根目录下旧轮转日志（不动 daily/ 子目录、不动 audit.sqlite3）。"""
    removed: list[str] = []
    root = Path(DATA_DIR) / "logs"
    if not root.exists():
        return removed
    for pattern in ("*.log", "quant.log.*", "live_*.log"):
        for p in root.glob(pattern):
            if p.is_file():
                try:
                    p.unlink()
                    removed.append(str(p.resolve()))
                except OSError:
                    pass
    lock = root / "run_strategy.lock"
    if lock.exists():
        try:
            lock.unlink()
            removed.append(str(lock.resolve()))
        except OSError:
            pass
    return removed


def init_session(*, run_id: str) -> Path | None:
    """会话启动时调用：可选清理旧日志、写入 system 头、固定 run_id。"""
    global _run_id, _last_tick_wall
    if not DETAILED_DAILY_LOG:
        return None
    _run_id = run_id
    _last_tick_wall = 0.0
    removed: list[str] = []
    if DETAILED_LOG_PURGE_LEGACY:
        removed = purge_legacy_root_logs()
    base = _ensure_day_dir()
    _write(
        "system",
        "session_init",
        {
            "run_id": run_id,
            "purge_legacy_removed": removed,
            "data_dir": str(Path(DATA_DIR).resolve()),
            "daily_log_dir": str(base.resolve()),
            "log_files": {
                "market.jsonl": "逐笔 tick：last/bid/ask/mid、点差、盘口摘要、库存、意图是否产生、outcome",
                "analysis.jsonl": "策略分析快照：网格 regime、波动率、status_summary 等",
                "decisions.jsonl": "决策链路：意图、门控拒绝、干跑、拦截原因",
                "execution.jsonl": "REST 执行：提交前后、成功/失败、延迟、熔断跳过",
                "system.jsonl": "会话启动、旧日志清理、配置与目录元信息",
                "README.txt": "人类可读的当日目录说明",
            },
        },
    )
    return base


def _write(channel: str, event: str, payload: dict[str, Any]) -> None:
    if not DETAILED_DAILY_LOG:
        return
    base = _ensure_day_dir()
    path = base / f"{channel}.jsonl"
    rec: dict[str, Any] = {
        "ts_wall": datetime.now().isoformat(timespec="milliseconds"),
        "run_id": _run_id,
        "channel": channel,
        "event": event,
        **payload,
    }
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    with _lock:
        with path.open("a", encoding="utf-8") as _fh:
            _fh.write(line)


def _order_book_brief(ob: Any, max_levels: int = 5) -> dict[str, Any] | None:
    if not isinstance(ob, dict):
        return None
    out: dict[str, Any] = {}
    for side in ("bids", "asks"):
        rows = ob.get(side)
        if not isinstance(rows, list):
            continue
        slim = []
        for row in rows[:max_levels]:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                slim.append({"px": row[0], "sz": row[1]})
            elif isinstance(row, dict):
                slim.append({k: row.get(k) for k in ("px", "sz", "price", "size") if k in row})
        out[side] = slim
    return out or None


def _mctx_public_slice(mctx: dict[str, Any]) -> dict[str, Any]:
    """去掉可能过大的字段，保留键与可序列化摘要。"""
    keys = sorted(mctx.keys())
    slim: dict[str, Any] = {"keys": keys}
    for k in (
        "spread_bps",
        "mid",
        "quote_age_sec",
        "ticker_ts",
        "inventory",
        "usdt_avail_swap",
        "funding_rate",
        "mark_px",
    ):
        if k in mctx:
            slim[k] = mctx.get(k)
    ob = mctx.get("order_book")
    if isinstance(ob, dict):
        slim["order_book_brief"] = _order_book_brief(ob)
    return slim


def record_tick(
    *,
    outcome: str,
    last: float,
    bid: float,
    ask: float,
    qh: dict[str, Any],
    mctx: dict[str, Any],
    intent: Any,
    extra: dict[str, Any] | None = None,
) -> None:
    """主循环每个 tick 汇总（受 DETAILED_LOG_TICK_MIN_SEC 节流）。"""
    global _last_tick_wall
    if not DETAILED_DAILY_LOG:
        return
    now = time.time()
    min_iv = float(DETAILED_LOG_TICK_MIN_SEC or 0.0)
    if min_iv > 0.0 and (now - _last_tick_wall) < min_iv:
        return
    _last_tick_wall = now
    mid = (float(bid) + float(ask)) / 2.0 if bid and ask else float(last)
    payload: dict[str, Any] = {
        "outcome": outcome,
        "last": last,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "enriched_quote": qh,
        "market_context": _mctx_public_slice(mctx),
        "intent_generated": intent is not None,
    }
    if intent is not None:
        try:
            payload["intent"] = asdict(intent)
        except TypeError:
            payload["intent"] = str(intent)
    if extra:
        payload["extra"] = extra
    _write("market", "tick", payload)


def record_decision(event: str, **fields: Any) -> None:
    if not DETAILED_DAILY_LOG:
        return
    _write("decisions", event, fields)


def record_analysis(event: str, **fields: Any) -> None:
    if not DETAILED_DAILY_LOG:
        return
    _write("analysis", event, fields)


def record_execution(event: str, **fields: Any) -> None:
    if not DETAILED_DAILY_LOG:
        return
    _write("execution", event, fields)


def record_system(event: str, **fields: Any) -> None:
    if not DETAILED_DAILY_LOG:
        return
    _write("system", event, fields)


def intent_dict(intent: Any) -> dict[str, Any] | None:
    if intent is None:
        return None
    try:
        return asdict(intent)
    except TypeError:
        return {"repr": repr(intent)}
