from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.command_service import submit_intent_order  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.human_gateway import submit_manual_order  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(dt_s: str) -> int:
    return int(datetime.fromisoformat(dt_s).timestamp() * 1000)


def test_submit_intent_order_is_idempotent_for_same_key():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    first = submit_intent_order(
        conn,
        run_id="manual:2026-04-23",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        source="manual",
        idempotency_key="manual-buy-001",
        side="BUY",
        qty=100,
        price=100.0,
        action="MANUAL_ORDER",
        order_reason="manual buy",
        operator_id="tester",
        operator_channel="test",
        request_ts_ms=_ts_ms("2026-04-23T10:00:00"),
    )
    second = submit_intent_order(
        conn,
        run_id="manual:2026-04-23",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        source="manual",
        idempotency_key="manual-buy-001",
        side="BUY",
        qty=100,
        price=100.0,
        action="MANUAL_ORDER",
        order_reason="manual buy",
        operator_id="tester",
        operator_channel="test",
        request_ts_ms=_ts_ms("2026-04-23T10:01:00"),
    )

    intents = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='manual' AND idempotency_key='manual-buy-001'").fetchone()
    orders = conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
    fills = conn.execute("SELECT COUNT(*) AS n FROM paper_fills").fetchone()
    outbox_filled = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE event_type='order.filled'").fetchone()

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["reused"] is True
    assert first["order_info"]["order_id"] == second["order_info"]["order_id"]
    assert int(intents["n"]) == 1
    assert int(orders["n"]) == 1
    assert int(fills["n"]) == 1
    assert int(outbox_filled["n"]) == 1


def test_submit_intent_order_rejects_tplus1_sell():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    buy_result = submit_intent_order(
        conn,
        run_id="manual:2026-04-23",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        source="manual",
        idempotency_key="manual-buy-002",
        side="BUY",
        qty=100,
        price=100.0,
        action="MANUAL_ORDER",
        order_reason="buy first",
        operator_id="tester",
        operator_channel="test",
        request_ts_ms=_ts_ms("2026-04-23T10:00:00"),
    )
    sell_result = submit_intent_order(
        conn,
        run_id="manual:2026-04-23",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        source="manual",
        idempotency_key="manual-sell-002",
        side="SELL",
        qty=100,
        price=101.0,
        action="MANUAL_ORDER",
        order_reason="sell same day",
        operator_id="tester",
        operator_channel="test",
        request_ts_ms=_ts_ms("2026-04-23T10:05:00"),
    )

    assert buy_result["accepted"] is True
    assert sell_result["accepted"] is False
    assert sell_result["order_info"]["intent_status"] == "rejected"
    assert sell_result["order_info"]["reject_code"] == "RULE_TPLUS1_VIOLATION"
    outbox_rejected = conn.execute(
        "SELECT COUNT(*) AS n FROM event_outbox WHERE event_type='intent.rejected' AND entity_id=?",
        (str(sell_result["intent"]["intent_id"]),),
    ).fetchone()
    assert outbox_rejected and int(outbox_rejected["n"]) == 1


def test_submit_manual_order_uses_same_intent_chain():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    first = submit_manual_order(
        conn,
        cfg=cfg,
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        side="BUY",
        qty=100,
        price=100.0,
        idempotency_key="manual-gateway-001",
        note="via gateway",
        operator_id="ui",
        operator_channel="api",
        request_ts_ms=_ts_ms("2026-04-23T10:00:00"),
    )
    second = submit_manual_order(
        conn,
        cfg=cfg,
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        side="BUY",
        qty=100,
        price=100.0,
        idempotency_key="manual-gateway-001",
        note="via gateway",
        operator_id="ui",
        operator_channel="api",
        request_ts_ms=_ts_ms("2026-04-23T10:01:00"),
    )

    orders = conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
    intents = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='manual' AND idempotency_key='manual-gateway-001'").fetchone()

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert second["reused"] is True
    assert int(orders["n"]) == 1
    assert int(intents["n"]) == 1
