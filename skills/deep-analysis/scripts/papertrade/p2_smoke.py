from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.api_common import new_id, now_ms  # noqa: E402
from papertrade.config import PaperTradeConfig, load_config  # noqa: E402
from papertrade.ledger import append_jsonl, connect, init_db  # noqa: E402


P2_TABLES = [
    "candidate_pool",
    "candidate_pool_items",
    "quote_snapshots",
    "runtime_sessions",
    "runtime_loops",
    "runtime_leases",
    "order_intents",
    "event_outbox",
    "watchlists",
]

LEGACY_TABLES = [
    "signals",
    "positions",
    "paper_positions",
    "paper_orders",
    "paper_nav_daily",
]

ACCEPTED_ENDPOINT_ERRORS = {
    "candidate_pool": {"NOT_FOUND"},
    "dashboard_candidates": {"NOT_FOUND"},
    "runtime_status": {"RUNTIME_NOT_RUNNING"},
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"] if row else 0)


def _endpoint_check(name: str, envelope: dict[str, Any]) -> dict[str, Any]:
    ok = bool(envelope.get("ok"))
    data = envelope.get("data") if ok else {}
    err = envelope.get("error") if not ok else {}
    error_code = str((err or {}).get("error_code") or "")
    accepted_errors = ACCEPTED_ENDPOINT_ERRORS.get(name, set())
    check = {
        "ok": ok,
        "accepted": ok or error_code in accepted_errors,
        "error_code": None if ok else error_code,
    }
    if not ok:
        check["message"] = str((err or {}).get("message") or "")
    if isinstance(data, dict):
        if "items" in data:
            check["item_count"] = len(data.get("items") or [])
        if "status" in data:
            check["status"] = data.get("status")
    return check


def _collect_endpoints(
    conn: sqlite3.Connection,
    *,
    candidate_limit: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    envelopes = {
        "dashboard_summary": api.get_dashboard_summary(conn),
        "candidate_pool": api.get_candidate_pool(conn, limit=candidate_limit),
        "dashboard_candidates": api.get_dashboard_candidates(conn, limit=candidate_limit),
        "quote_latest": api.get_quote_snapshots(conn, limit=100),
        "runtime_status": api.get_runtime_status(conn),
        "metrics": api.get_metrics(conn, limit=100),
        "health": api.get_health(conn, emit_alerts=False),
    }
    checks = {name: _endpoint_check(name, envelope) for name, envelope in envelopes.items()}
    return envelopes, checks


def _observed_action_states(conn: sqlite3.Connection) -> dict[str, list[str]]:
    candidate_states: list[str] = []
    signal_actions: list[str] = []
    if _table_exists(conn, "candidate_pool_items"):
        rows = conn.execute(
            """
            SELECT DISTINCT action_state
            FROM candidate_pool_items
            WHERE action_state IS NOT NULL AND action_state != ''
            ORDER BY action_state
            """
        ).fetchall()
        candidate_states = [str(row["action_state"]) for row in rows]
    if _table_exists(conn, "signals"):
        rows = conn.execute(
            """
            SELECT DISTINCT action
            FROM signals
            WHERE action IS NOT NULL AND action != ''
            ORDER BY action
            """
        ).fetchall()
        signal_actions = [str(row["action"]) for row in rows]
    return {"candidate_pool_items": candidate_states, "signals": signal_actions}


def _candidate_summary(envelope: dict[str, Any]) -> dict[str, Any]:
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        return {
            "available": False,
            "error_code": error.get("error_code"),
            "item_count": 0,
        }
    data = envelope.get("data") or {}
    return {
        "available": True,
        "candidate_batch_id": data.get("candidate_batch_id"),
        "pool_name": data.get("pool_name"),
        "status": data.get("status"),
        "item_count": int(data.get("item_count") or len(data.get("items") or [])),
        "buckets": data.get("buckets", {}),
    }


def _quote_summary(envelope: dict[str, Any]) -> dict[str, Any]:
    if not envelope.get("ok"):
        error = envelope.get("error") or {}
        return {"available": False, "error_code": error.get("error_code"), "item_count": 0}
    data = envelope.get("data") or {}
    items = list(data.get("items") or [])
    failed = sum(1 for item in items if item.get("status") != "ok")
    return {
        "available": True,
        "item_count": len(items),
        "ok_count": len(items) - failed,
        "failed_count": failed,
    }


def run_p2_smoke(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    candidate_limit: int = 20,
    write_log: bool = True,
    db_label: str | None = None,
) -> dict[str, Any]:
    """Run a dry P2 smoke over persisted papertrade state.

    This intentionally does not submit intents, match orders, refresh network
    quotes, or rebuild candidate pools. It only reads API/read-model surfaces
    and writes a JSONL audit record when requested.
    """

    smoke_id = new_id("p2smoke")
    ts_ms = now_ms()
    endpoints, checks = _collect_endpoints(conn, candidate_limit=candidate_limit)
    table_counts = {table: _table_count(conn, table) for table in [*P2_TABLES, *LEGACY_TABLES]}
    missing_tables = [table for table in P2_TABLES if table_counts.get(table) is None]
    action_states = _observed_action_states(conn)

    warnings: list[str] = []
    if not endpoints["candidate_pool"].get("ok"):
        warnings.append("candidate_pool_missing_or_empty")
    if not (endpoints["quote_latest"].get("data") or {}).get("items"):
        warnings.append("quote_snapshots_empty")
    if not action_states["candidate_pool_items"]:
        warnings.append("candidate_action_states_not_observed")

    feature_checks = {
        "schema_tables_ok": not missing_tables,
        "candidate_api_ok": checks["candidate_pool"]["accepted"] and checks["dashboard_candidates"]["accepted"],
        "quote_api_ok": checks["quote_latest"]["accepted"],
        "session_api_ok": checks["runtime_status"]["accepted"],
        "metrics_api_ok": checks["metrics"]["accepted"],
        "health_api_ok": checks["health"]["accepted"],
    }
    ok = all(feature_checks.values()) and checks["dashboard_summary"]["accepted"]

    record = {
        "kind": "p2_smoke",
        "schema_version": 1,
        "smoke_id": smoke_id,
        "ts_ms": ts_ms,
        "dry_run": True,
        "db_path": db_label or str(cfg.db_path),
        "candidate_limit": int(candidate_limit),
        "ok": ok,
        "feature_checks": feature_checks,
        "checks": checks,
        "tables": table_counts,
        "missing_p2_tables": missing_tables,
        "candidate": _candidate_summary(endpoints["dashboard_candidates"]),
        "quotes": _quote_summary(endpoints["quote_latest"]),
        "action_states": action_states,
        "warnings": warnings,
        "log": {
            "target": str(cfg.run_logs_jsonl),
            "written": bool(write_log),
        },
    }
    if write_log:
        append_jsonl(cfg.run_logs_jsonl, record)
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a dry P2 papertrade smoke check.")
    parser.add_argument("--config", help="Optional papertrade config JSON path.")
    parser.add_argument("--db", help="SQLite DB path. Defaults to config db_path.")
    parser.add_argument("--log", help="JSONL execution log path. Defaults to config run_logs_jsonl.")
    parser.add_argument("--candidate-limit", type=int, default=20)
    parser.add_argument("--no-log", action="store_true", help="Do not append the smoke record to run_logs_jsonl.")
    parser.add_argument("--no-ensure-schema", action="store_true", help="Skip additive schema initialization.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    overrides: dict[str, Any] = {}
    if args.db:
        overrides["db_path"] = args.db
    if args.log:
        overrides["run_logs_jsonl"] = args.log
    cfg = load_config(args.config, overrides=overrides or None)
    conn = connect(cfg.db_path)
    try:
        if not args.no_ensure_schema:
            init_db(conn, initial_cash=cfg.trade.initial_cash)
        record = run_p2_smoke(
            conn,
            cfg=cfg,
            candidate_limit=args.candidate_limit,
            write_log=not args.no_log,
        )
    finally:
        conn.close()
    print(json.dumps(record, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if record.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run_p2_smoke", "main"]
