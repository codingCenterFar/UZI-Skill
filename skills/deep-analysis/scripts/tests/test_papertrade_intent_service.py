from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.intent_service import (  # noqa: E402
    IdempotencyConflictError,
    create_intent,
    transition_intent_status,
)
from papertrade.ledger import init_db  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_create_intent_is_idempotent_when_payload_same():
    conn = _conn()
    init_db(conn, initial_cash=1000000.0)

    first = create_intent(
        conn,
        source="manual",
        idempotency_key="idem-001",
        ticker="600519.SH",
        side="BUY",
        qty=100,
    )
    second = create_intent(
        conn,
        source="manual",
        idempotency_key="idem-001",
        ticker="600519.SH",
        side="BUY",
        qty=100,
    )
    rows = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='manual' AND idempotency_key='idem-001'").fetchone()

    assert first.reused is False
    assert second.reused is True
    assert first.intent_id == second.intent_id
    assert int(rows["n"]) == 1


def test_create_intent_raises_idempotency_conflict_when_payload_differs():
    conn = _conn()
    init_db(conn, initial_cash=1000000.0)

    create_intent(
        conn,
        source="manual",
        idempotency_key="idem-002",
        ticker="600519.SH",
        side="BUY",
        qty=100,
    )
    with pytest.raises(IdempotencyConflictError):
        create_intent(
            conn,
            source="manual",
            idempotency_key="idem-002",
            ticker="600519.SH",
            side="BUY",
            qty=200,
        )


def test_transition_intent_status_updates_state():
    conn = _conn()
    init_db(conn, initial_cash=1000000.0)

    created = create_intent(
        conn,
        source="auto",
        idempotency_key="idem-003",
        ticker="600519.SH",
        side="SELL",
        qty=100,
    )
    transition_intent_status(conn, intent_id=created.intent_id, to_status="validated")
    transition_intent_status(conn, intent_id=created.intent_id, to_status="queued")
    final = transition_intent_status(
        conn,
        intent_id=created.intent_id,
        to_status="rejected",
        reject_code="RULE_INSUFFICIENT_SELLABLE",
        reject_reason="sellable qty is 0",
    )

    assert final["status"] == "rejected"
    assert final["reject_code"] == "RULE_INSUFFICIENT_SELLABLE"
