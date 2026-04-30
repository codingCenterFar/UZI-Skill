from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.health import check_portfolio_consistency, check_runtime_health  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.metrics import query_runtime_metrics  # noqa: E402
from papertrade.recovery import recover_expired_runtime_lease  # noqa: E402
from papertrade.runtime_store import (  # noqa: E402
    DEFAULT_RUNTIME_LEASE_NAME,
    acquire_runtime_lease,
    create_runtime_loop,
    finish_runtime_loop,
    start_runtime_session,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_runtime_loop_metrics_are_persisted_and_queryable():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)
    session_id = start_runtime_session(conn, start_request_id="metrics-start-001", config_json={})

    for idx, duration in enumerate([100, 200, 300], start=1):
        loop_id = f"lp_metrics_{idx}"
        create_runtime_loop(conn, loop_id=loop_id, session_id=session_id, loop_no=idx)
        conn.execute(
            "UPDATE runtime_loops SET started_at_ms=? WHERE loop_id=?",
            (10_000, loop_id),
        )
        finish_runtime_loop(
            conn,
            loop_id=loop_id,
            status="completed",
            ticker_count=1,
            success_count=1,
            failed_count=0,
            db_write_duration_ms=idx * 10,
            metrics={
                "quote_freshness_ms": idx * 1000,
                "analysis_freshness_ms": idx * 2000,
                "event_queue_lag": idx,
            },
        )
        conn.execute(
            "UPDATE runtime_loops SET duration_ms=? WHERE loop_id=?",
            (duration, loop_id),
        )

    metrics = query_runtime_metrics(conn, limit=10)
    assert metrics["quote_freshness_ms"] == 3000
    assert metrics["analysis_freshness_ms"] == 6000
    assert metrics["loop_duration_p50"] == 200
    assert metrics["loop_duration_p95"] == 290

    response = api.get_metrics(conn)
    assert response["ok"] is True
    assert response["data"]["loop_count"] == 3


def test_runtime_start_can_take_over_expired_lease():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    first = api.post_runtime_start(
        conn,
        cfg=cfg,
        payload={"poll_seconds": 5},
        idempotency_key="health-runtime-start-001",
    )
    assert first["ok"] is True

    conn.execute(
        "UPDATE runtime_leases SET lease_expires_at_ms=?, heartbeat_at_ms=? WHERE lease_name=?",
        (1_000, 1_000, DEFAULT_RUNTIME_LEASE_NAME),
    )

    second = api.post_runtime_start(
        conn,
        cfg=cfg,
        payload={"poll_seconds": 5},
        idempotency_key="health-runtime-start-002",
    )
    assert second["ok"] is True
    assert second["data"]["session_id"] != first["data"]["session_id"]
    assert second["data"]["lease"]["takeover"] is True


def test_recovery_marks_stale_session_failed_when_taking_over_expired_lease():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)
    stale_session = start_runtime_session(conn, start_request_id="recover-stale", config_json={})
    new_session = start_runtime_session(conn, start_request_id="recover-new", config_json={})

    first = acquire_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=stale_session,
        ttl_ms=5_000,
        now_ms=1_000,
    )
    assert first.acquired is True

    recovered = recover_expired_runtime_lease(
        conn,
        owner_session_id=new_session,
        ttl_ms=5_000,
        now=10_000,
    )
    stale = conn.execute(
        "SELECT status, stop_reason FROM runtime_sessions WHERE session_id=?",
        (stale_session,),
    ).fetchone()

    assert recovered["recovered"] is True
    assert recovered["takeover"] is True
    assert stale["status"] == "failed"
    assert stale["stop_reason"] == "lease_expired_takeover"


def test_equity_recompute_diff_triggers_alert_event():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)
    conn.execute(
        """
        INSERT INTO paper_nav_daily (
            as_of_date, run_id, cash, position_market_value, equity,
            cumulative_return_pct, drawdown_pct, updated_at
        ) VALUES ('2026-04-24', 'bad_nav', 1.0, 0.0, 1.0, 0.0, 0.0, '2026-04-24T10:00:00')
        """
    )

    consistency = check_portfolio_consistency(conn, threshold=0.01, emit_alert=True)
    assert consistency["ok"] is False
    assert abs(consistency["equity_recompute_diff"]) > 0.01

    alert = conn.execute(
        "SELECT * FROM event_outbox WHERE event_type='alert.raised' AND entity_id='EQUITY_RECOMPUTE_DIFF'",
    ).fetchone()
    assert alert is not None

    health = check_runtime_health(conn, emit_alerts=False)
    assert health["status"] == "degraded"
    assert any(issue["code"] == "EQUITY_RECOMPUTE_DIFF" for issue in health["issues"])
