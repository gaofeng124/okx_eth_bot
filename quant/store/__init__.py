"""Merged store package."""
from __future__ import annotations

import json
import threading

try:
    import sqlite3
    _SQLITE3_AVAILABLE = True
except ImportError:
    _SQLITE3_AVAILABLE = False
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant.models import OrderIntent


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class NullAuditStore:
    def __init__(self, db_path: Any = None) -> None:
        pass

    def start_run(self, run_id: str, inst_id: str, meta: dict[str, Any] | None = None) -> None:
        pass

    def log_signal(
        self,
        run_id: str,
        intent: OrderIntent,
        *,
        last: float,
        bid: float,
        ask: float,
        reason: str | None = None,
        features_json: str | None = None,
    ) -> None:
        pass

    def log_order_submit(
        self,
        run_id: str,
        client_order_id: str,
        intent: OrderIntent,
    ) -> None:
        pass

    def session_flow_counts(self, run_id: str) -> tuple[int, int]:
        return (0, 0)

    def count_fee_gate_rejected_signals(self, run_id: str) -> int:
        return 0

    def signal_reason_breakdown(self, run_id: str) -> dict[str, int]:
        return {"total": 0, "fee_gate": 0, "close": 0, "stale_exit": 0, "open": 0}

    def list_recent_run_ids(self, limit: int = 20) -> list[str]:
        return []

    def feature_block_counts(self, run_id: str, *, sample_limit: int = 8000) -> dict[str, Any]:
        return {"parsed_rows": 0, "parse_errors": 0, "flags": {}}

    def load_order_signal_refs(self, run_id: str) -> dict[str, tuple[float | None, float | None, float | None]]:
        return {}

    def log_order_result(
        self,
        client_order_id: str,
        ok: bool,
        response: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        pass

    def log_execution_guard(
        self,
        run_id: str,
        *,
        guard_type: str,
        inst_id: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        pass

    def close(self) -> None:
        pass

    def load_calibration_samples(self, run_id: str, limit: int = 8000) -> list[dict[str, Any]]:
        return []

    def log_calibration_sample(
        self,
        *,
        run_id: str,
        inst_id: str,
        raw_score: float,
        label: int,
        side: str,
        pnl_pct: float,
        method: str,
    ) -> None:
        pass

    def log_calibration_ece(
        self,
        *,
        run_id: str,
        n_samples: int,
        ece: float,
        mce: float,
        method: str,
        n_bins: int,
        bins_json: str,
    ) -> None:
        pass

    def log_funding_arb_record(
        self,
        *,
        run_id: str,
        open_ts: str,
        close_ts: str | None,
        swap_inst_id: str,
        spot_inst_id: str,
        direction: str,
        notional_usdt: float | None,
        funding_collected_usdt: float | None,
        hedge_cost_usdt: float | None,
        net_pnl_usdt: float | None,
        detail_json: str | None = None,
    ) -> None:
        pass



class AuditStore:
    """SQLite 审计：信号、下单请求与结果，便于复盘与合规。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                inst_id TEXT NOT NULL,
                meta_json TEXT
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                inst_id TEXT NOT NULL,
                side TEXT NOT NULL,
                ord_type TEXT NOT NULL,
                px TEXT,
                sz TEXT NOT NULL,
                last REAL,
                bid REAL,
                ask REAL,
                reason TEXT,
                features_json TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                client_order_id TEXT UNIQUE,
                ts_submit TEXT NOT NULL,
                ts_result TEXT,
                inst_id TEXT NOT NULL,
                side TEXT NOT NULL,
                ord_type TEXT NOT NULL,
                px TEXT,
                sz TEXT NOT NULL,
                ok INTEGER,
                response_json TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id);
            CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(run_id);
            """
        )
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        for col in ("reason", "features_json"):
            try:
                self._conn.execute(f"ALTER TABLE signals ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        for col in ("ref_last", "ref_bid", "ref_ask"):
            try:
                self._conn.execute(f"ALTER TABLE orders ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calibration_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                inst_id TEXT NOT NULL,
                raw_score REAL NOT NULL,
                label INTEGER NOT NULL,
                side TEXT,
                pnl_pct REAL,
                method TEXT NOT NULL DEFAULT 'platt'
            );
            CREATE INDEX IF NOT EXISTS idx_calib_samples_run ON calibration_samples(run_id);
            CREATE TABLE IF NOT EXISTS calibration_ece_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                n_samples INTEGER NOT NULL,
                ece REAL NOT NULL,
                mce REAL,
                method TEXT,
                n_bins INTEGER,
                bins_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_calib_ece_run ON calibration_ece_log(run_id);
            CREATE TABLE IF NOT EXISTS execution_guard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                guard_type TEXT NOT NULL,
                inst_id TEXT,
                reason TEXT,
                detail_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exec_guard_run ON execution_guard_events(run_id);
            -- 历史：现货+永续资金费套利会话；当前主程序为单永续时可不写入，表保留兼容旧库。
            CREATE TABLE IF NOT EXISTS funding_arb_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                open_ts TEXT NOT NULL,
                close_ts TEXT,
                swap_inst_id TEXT NOT NULL,
                spot_inst_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                notional_usdt REAL,
                funding_collected_usdt REAL,
                hedge_cost_usdt REAL,
                net_pnl_usdt REAL,
                detail_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_funding_arb_run ON funding_arb_records(run_id);
            """
        )
        self._conn.commit()

    def load_calibration_samples(self, run_id: str, limit: int = 8000) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit), 50000))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT raw_score, label FROM calibration_samples
                WHERE run_id=? ORDER BY id ASC LIMIT ?
                """,
                (run_id, lim),
            )
            return [{"raw_score": r[0], "label": int(r[1])} for r in cur.fetchall()]

    def log_calibration_sample(
        self,
        *,
        run_id: str,
        inst_id: str,
        raw_score: float,
        label: int,
        side: str,
        pnl_pct: float,
        method: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO calibration_samples(
                    run_id, ts, inst_id, raw_score, label, side, pnl_pct, method
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    _utc_now(),
                    inst_id,
                    raw_score,
                    1 if int(label) == 1 else 0,
                    side[:16] if side else "",
                    pnl_pct,
                    method[:16],
                ],
            )
            self._conn.commit()

    def log_calibration_ece(
        self,
        *,
        run_id: str,
        n_samples: int,
        ece: float,
        mce: float,
        method: str,
        n_bins: int,
        bins_json: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO calibration_ece_log(
                    run_id, ts, n_samples, ece, mce, method, n_bins, bins_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    _utc_now(),
                    int(n_samples),
                    float(ece),
                    float(mce),
                    method[:24],
                    int(n_bins),
                    bins_json,
                ],
            )
            self._conn.commit()

    def log_funding_arb_record(
        self,
        *,
        run_id: str,
        open_ts: str,
        close_ts: str | None,
        swap_inst_id: str,
        spot_inst_id: str,
        direction: str,
        notional_usdt: float | None,
        funding_collected_usdt: float | None,
        hedge_cost_usdt: float | None,
        net_pnl_usdt: float | None,
        detail_json: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO funding_arb_records(
                    run_id, open_ts, close_ts, swap_inst_id, spot_inst_id, direction,
                    notional_usdt, funding_collected_usdt, hedge_cost_usdt, net_pnl_usdt, detail_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    open_ts,
                    close_ts,
                    swap_inst_id,
                    spot_inst_id,
                    direction[:64],
                    notional_usdt,
                    funding_collected_usdt,
                    hedge_cost_usdt,
                    net_pnl_usdt,
                    detail_json,
                ],
            )
            self._conn.commit()

    def start_run(self, run_id: str, inst_id: str, meta: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at, inst_id, meta_json) VALUES(?,?,?,?)",
                [
                    run_id,
                    _utc_now(),
                    inst_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                ],
            )
            self._conn.commit()

    def log_signal(
        self,
        run_id: str,
        intent: OrderIntent,
        *,
        last: float,
        bid: float,
        ask: float,
        reason: str | None = None,
        features_json: str | None = None,
    ) -> None:
        r = reason if reason is not None else intent.reason
        fj = features_json if features_json is not None else intent.features_json
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO signals(run_id, ts, inst_id, side, ord_type, px, sz, last, bid, ask, reason, features_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    run_id,
                    _utc_now(),
                    intent.inst_id,
                    intent.side,
                    intent.ord_type,
                    intent.px,
                    intent.sz,
                    last,
                    bid,
                    ask,
                    r,
                    fj,
                ],
            )
            self._conn.commit()

    def log_order_submit(
        self,
        run_id: str,
        client_order_id: str,
        intent: OrderIntent,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders(
                    run_id, client_order_id, ts_submit, inst_id, side, ord_type, px, sz, ok,
                    ref_last, ref_bid, ref_ask
                )
                VALUES(?,?,?,?,?,?,?,?,NULL,?,?,?)
                """,
                [
                    run_id,
                    client_order_id,
                    _utc_now(),
                    intent.inst_id,
                    intent.side,
                    intent.ord_type,
                    intent.px,
                    intent.sz,
                    intent.signal_last,
                    intent.signal_bid,
                    intent.signal_ask,
                ],
            )
            self._conn.commit()

    def session_flow_counts(self, run_id: str) -> tuple[int, int]:
        """本会话：signals 条数、orders 表中 ok=1（REST 受理成功）条数。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE run_id=?",
                (run_id,),
            )
            n_sig = int(cur.fetchone()[0] or 0)
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM orders WHERE run_id=? AND ok=1",
                (run_id,),
            )
            n_ok = int(cur.fetchone()[0] or 0)
        return (n_sig, n_ok)

    def count_fee_gate_rejected_signals(self, run_id: str) -> int:
        """本会话 signals 表中 reason=fee_gate_rejected 条数（与 metrics 同源审计）。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE run_id=? AND reason='fee_gate_rejected'",
                (run_id,),
            )
            return int(cur.fetchone()[0] or 0)

    def signal_reason_breakdown(self, run_id: str) -> dict[str, int]:
        """
        按 reason 粗分会话信号结构，便于区分“开仓信号”与“平仓维护信号（含 stale_exit）”。
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE run_id=?",
                (run_id,),
            )
            total = int(cur.fetchone()[0] or 0)
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE run_id=? AND reason='fee_gate_rejected'",
                (run_id,),
            )
            fee_gate = int(cur.fetchone()[0] or 0)
            cur = self._conn.execute(
                """
                SELECT COUNT(*) FROM signals
                WHERE run_id=? AND (
                    lower(coalesce(reason,'')) LIKE '%stale_exit%'
                    OR lower(coalesce(reason,'')) LIKE '%trail_exit%'
                    OR lower(coalesce(reason,'')) LIKE '%scaleout%'
                    OR lower(coalesce(reason,'')) LIKE '%quick_loss_exit%'
                    OR lower(coalesce(reason,'')) LIKE '%force_cut%'
                    OR lower(coalesce(reason,'')) LIKE '%funding_bleed%'
                    OR lower(coalesce(reason,'')) LIKE '%stuck_quote_lite%'
                    OR lower(coalesce(reason,'')) LIKE 'lev5 exit %'
                )
                """,
                (run_id,),
            )
            close_sig = int(cur.fetchone()[0] or 0)
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE run_id=? AND lower(coalesce(reason,'')) LIKE '%stale_exit%'",
                (run_id,),
            )
            stale_sig = int(cur.fetchone()[0] or 0)
        open_sig = max(0, total - fee_gate - close_sig)
        return {
            "total": total,
            "fee_gate": fee_gate,
            "close": close_sig,
            "stale_exit": stale_sig,
            "open": open_sig,
        }

    def list_recent_run_ids(self, limit: int = 20) -> list[str]:
        lim = max(1, min(int(limit), 500))
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT run_id FROM signals
                GROUP BY run_id
                ORDER BY MAX(id) DESC
                LIMIT ?
                """,
                (lim,),
            )
            return [str(r[0]) for r in cur.fetchall() if r[0]]

    def feature_block_counts(
        self, run_id: str, *, sample_limit: int = 8000
    ) -> dict[str, Any]:
        """
        从最近若干条 signals 的 features_json 聚合「拦截类」布尔键出现次数，
        便于快速判断哪类门控在消耗 tick（调参优先级）。
        """
        lim = max(1, min(int(sample_limit), 50_000))
        flags: dict[str, int] = {}
        parsed = 0
        errors = 0

        def _is_diag_true(k: str) -> bool:
            """开仓前拦截 / 门控 / 抑制类布尔键，便于复盘调参。"""
            kl = k.lower()
            return (
                kl.endswith("_blocked")
                or kl.endswith("_block")
                or "_blocked_" in kl
                or kl.startswith("blocked_")
                or kl.endswith("_suppress")
                or "_gate_" in kl
                or kl.endswith("_gate_blocked")
                or kl.startswith("prediction_gate_")
            )

        with self._lock:
            cur = self._conn.execute(
                """
                SELECT features_json FROM signals
                WHERE run_id=? AND features_json IS NOT NULL AND features_json != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (run_id, lim),
            )
            rows = cur.fetchall()
        for (fj,) in rows:
            if not fj or not isinstance(fj, str):
                continue
            try:
                d = json.loads(fj)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(d, dict):
                errors += 1
                continue
            parsed += 1
            for k, v in d.items():
                if v is True and _is_diag_true(k):
                    flags[k] = flags.get(k, 0) + 1
        return {"parsed_rows": parsed, "parse_errors": errors, "flags": flags}

    def load_order_signal_refs(
        self, run_id: str
    ) -> dict[str, tuple[float | None, float | None, float | None]]:
        out: dict[str, tuple[float | None, float | None, float | None]] = {}
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT client_order_id, ref_last, ref_bid, ref_ask
                FROM orders WHERE run_id=? AND client_order_id IS NOT NULL
                """,
                (run_id,),
            )
            for row in cur.fetchall():
                cid = str(row[0] or "").strip()
                if not cid:
                    continue
                out[cid] = (row[1], row[2], row[3])
        return out

    def log_order_result(
        self,
        client_order_id: str,
        ok: bool,
        response: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET ts_result=?, ok=?, response_json=?, error=?
                WHERE client_order_id=?
                """,
                [
                    _utc_now(),
                    1 if ok else 0,
                    json.dumps(response) if response is not None else None,
                    error,
                    client_order_id,
                ],
            )
            self._conn.commit()

    def log_execution_guard(
        self,
        run_id: str,
        *,
        guard_type: str,
        inst_id: str,
        reason: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO execution_guard_events(
                    run_id, ts, guard_type, inst_id, reason, detail_json
                ) VALUES(?,?,?,?,?,?)
                """,
                [
                    run_id,
                    _utc_now(),
                    guard_type[:32],
                    inst_id,
                    reason[:2000] if reason else "",
                    json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                ],
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# 若 sqlite3 不可用（如在某些精简编译的 Python 上），AuditStore 降级为 NullAuditStore
if not _SQLITE3_AVAILABLE:
    AuditStore = NullAuditStore  # type: ignore[misc]

# ----- pnl_jsonl.py -----

"""将 [盈亏] 快照追加到 JSONL，便于日后检索或交给工具分析。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant.settings import DATA_DIR, LOG_JSONL_SNAPSHOTS


def append_pnl_snapshot(payload: dict[str, Any]) -> None:
    if not LOG_JSONL_SNAPSHOTS:
        return
    path = Path(DATA_DIR) / "logs" / "pnl_snapshots.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

# ----- runtime_checkpoints.py -----

"""Structured runtime checkpoints for post-run self diagnostics."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quant.settings import DATA_DIR, LOG_JSONL_SNAPSHOTS


def append_runtime_checkpoint(event: str, payload: dict[str, Any]) -> None:
    if not LOG_JSONL_SNAPSHOTS:
        return
    path = Path(DATA_DIR) / "logs" / "runtime_checkpoints.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")