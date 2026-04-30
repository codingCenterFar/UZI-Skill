from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.ledger import init_db  # noqa: E402
from papertrade.runtime_store import (  # noqa: E402
    DEFAULT_RUNTIME_LEASE_NAME,
    acquire_runtime_lease,
    create_runtime_loop,
    ensure_runtime_lease_owner,
    finish_runtime_loop,
    finish_runtime_session,
    get_runtime_lease,
    heartbeat_runtime_session,
    release_runtime_lease,
    renew_runtime_lease,
    start_runtime_session,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_runtime_session_and_loop_lifecycle():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    session_id = start_runtime_session(
        conn,
        start_request_id="start-001",
        config_json={"depth": "medium"},
    )
    create_runtime_loop(
        conn,
        loop_id="lp_test_001",
        session_id=session_id,
        loop_no=1,
        status="started",
        decision_version="test-v1",
        event_batch_id="eb_test_001",
    )
    finish_runtime_loop(
        conn,
        loop_id="lp_test_001",
        status="completed",
        ticker_count=2,
        success_count=2,
        failed_count=0,
        db_write_duration_ms=12,
        event_batch_id="eb_test_001",
        decision_basis_summary={"from_cache_only": False},
        staleness_summary={"flags": []},
    )
    heartbeat_runtime_session(conn, session_id=session_id, last_loop_id="lp_test_001")
    finish_runtime_session(
        conn,
        session_id=session_id,
        status="stopped",
        stop_reason="completed",
        last_loop_id="lp_test_001",
    )

    rs = conn.execute("SELECT * FROM runtime_sessions WHERE session_id=?", (session_id,)).fetchone()
    lp = conn.execute("SELECT * FROM runtime_loops WHERE loop_id='lp_test_001'").fetchone()

    assert rs is not None
    assert rs["status"] == "stopped"
    assert rs["last_loop_id"] == "lp_test_001"

    assert lp is not None
    assert lp["status"] == "completed"
    assert int(lp["ticker_count"]) == 2
    assert int(lp["success_count"]) == 2
    assert int(lp["failed_count"]) == 0


def test_runtime_lease_single_active_takeover_and_release():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    s1 = start_runtime_session(conn, start_request_id="start-lease-1", config_json={"depth": "medium"})
    s2 = start_runtime_session(conn, start_request_id="start-lease-2", config_json={"depth": "medium"})

    r1 = acquire_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s1,
        ttl_ms=5_000,
        now_ms=1_000,
    )
    assert r1.acquired is True
    assert r1.owner_session_id == s1

    blocked = acquire_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s2,
        ttl_ms=5_000,
        now_ms=2_000,
    )
    assert blocked.acquired is False
    assert blocked.owner_session_id == s1
    assert blocked.reason == "lease_owned_by_other"

    renewed = renew_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s1,
        ttl_ms=5_000,
        now_ms=3_000,
    )
    assert renewed is True
    assert ensure_runtime_lease_owner(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s1,
        now_ms=3_500,
    )

    takeover = acquire_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s2,
        ttl_ms=5_000,
        now_ms=9_500,
    )
    assert takeover.acquired is True
    assert takeover.takeover is True
    assert takeover.owner_session_id == s2

    assert not ensure_runtime_lease_owner(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s1,
        now_ms=9_600,
    )
    assert ensure_runtime_lease_owner(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s2,
        now_ms=9_600,
    )

    released = release_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=s2,
        now_ms=10_000,
    )
    assert released is True
    lease_row = get_runtime_lease(conn, lease_name=DEFAULT_RUNTIME_LEASE_NAME)
    assert lease_row is not None
    assert int(lease_row["lease_expires_at_ms"]) == 10_000
