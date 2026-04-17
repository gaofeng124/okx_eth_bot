"""
会话级信号复盘：reason 结构 + features_json 内拦截类布尔键聚合。

用法（在 okx_eth_bot 根目录）::

    python -m quant.tools.signal_digest
    python -m quant.tools.signal_digest --latest
    python -m quant.tools.signal_digest --run 20260407T120000-abc12345
    python -m quant.tools.signal_digest --db ./data/custom.sqlite3 --latest --limit 12000

依赖 quant.settings 中的 DATA_DIR / AUDIT_DB_NAME（或 --db）。

说明：仅「产生 OrderIntent 并写入 signals」的行才有 features_json；策略在某一
tick 直接 return None 时不会落库，因此拦截类计数反映的是已审计信号内的诊断键，
而非全市场 tick 统计。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from quant.settings import AUDIT_DB_NAME, DATA_DIR
from quant.store import AuditStore


def _default_db_path() -> Path:
    return Path(DATA_DIR) / AUDIT_DB_NAME


def main() -> None:
    ap = argparse.ArgumentParser(
        description="审计库：信号 reason 分解 + features_json 拦截键计数（调参/复盘）"
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"SQLite 路径（默认 {DATA_DIR}/{AUDIT_DB_NAME}）",
    )
    ap.add_argument("--run", type=str, default="", help="run_id；与 --latest 二选一")
    ap.add_argument(
        "--latest",
        action="store_true",
        help="使用 signals 表中最近一条所属的 run_id",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=8000,
        help="扫描 features_json 的最大条数（新在前）",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON，便于管道到 jq / 脚本",
    )
    args = ap.parse_args()

    db_path = args.db or _default_db_path()
    if not db_path.is_file():
        raise SystemExit(f"找不到审计库: {db_path}")

    store = AuditStore(db_path)
    try:
        recent = store.list_recent_run_ids(30)
        if args.latest and not args.run:
            if not recent:
                raise SystemExit("signals 表为空，无法 --latest")
            run_id = recent[0]
        elif args.run.strip():
            run_id = args.run.strip()
        else:
            print("最近 run_id（signals 按 id 最新活动排序）:")
            for r in recent:
                print(f"  {r}")
            print()
            print("请指定:  --latest  或  --run <run_id>")
            return

        flow = store.signal_reason_breakdown(run_id)
        blocks = store.feature_block_counts(run_id, sample_limit=args.limit)
        out = {
            "run_id": run_id,
            "db": str(db_path.resolve()),
            "signal_flow": flow,
            "feature_blocks": blocks,
        }
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        print(f"run_id: {run_id}")
        print(f"db:     {db_path.resolve()}")
        print()
        print("[signal_flow]")
        for k in ("total", "open", "close", "fee_gate", "stale_exit"):
            if k in flow:
                print(f"  {k}: {flow[k]}")
        print()
        print(
            f"[feature_blocks] 解析 {blocks.get('parsed_rows', 0)} 条 features_json "
            f"(parse_errors={blocks.get('parse_errors', 0)}, limit={args.limit})"
        )
        flags = blocks.get("flags") or {}
        if not flags:
            print("  (无门控/拦截类 True 键，或本段无 features；见模块说明)")
        else:
            for k, v in sorted(flags.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {k}: {v}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
