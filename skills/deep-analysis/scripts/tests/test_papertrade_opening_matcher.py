from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.opening_matcher import match_queued_day_orders  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(dt_s: str) -> int:
    return int(datetime.fromisoformat(dt_s).timestamp() * 1000)


def _order_payload(*, key: str, tif: str, request_ts: str) -> dict:
    return {
        "ticker": "600519.SH",
        "side": "BUY",
        "qty": 100,
        "order_type": "LIMIT",
        "time_in_force": tif,
        "limit_price": 100.0,
        "trade_date": "2026-04-23",
        "request_ts_ms": _ts_ms(request_ts),
        "client_order_id": key,
    }


def test_ioc_market_closed_still_rejects_but_day_order_queues():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    ioc = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_order_payload(key="ioc-closed-001", tif="IOC", request_ts="2026-04-23T20:00:00"),
        idempotency_key="ioc-closed-001",
    )
    day = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_order_payload(key="day-closed-001", tif="DAY", request_ts="2026-04-23T20:00:00"),
        idempotency_key="day-closed-001",
    )

    assert ioc["ok"] is False
    assert ioc["error"]["error_code"] == "RULE_MARKET_CLOSED"

    assert day["ok"] is True
    assert day["data"]["queued"] is True
    assert day["data"]["intent"]["status"] == "queued"
    assert day["data"]["order"] is None
    assert day["data"]["rule_check"]["allow"] is False
    assert day["data"]["rule_check"]["reject_code"] == "RULE_MARKET_CLOSED"

    orders = conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
    fills = conn.execute("SELECT COUNT(*) AS n FROM paper_fills").fetchone()
    queued_evt = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE event_type='intent.queued'").fetchone()
    assert int(orders["n"]) == 0
    assert int(fills["n"]) == 0
    assert int(queued_evt["n"]) == 1


def test_opening_matcher_fills_queued_day_order_once():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    queued = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_order_payload(key="day-match-001", tif="DAY", request_ts="2026-04-23T20:00:00"),
        idempotency_key="day-match-001",
    )
    assert queued["ok"] is True
    intent_id = queued["data"]["intent"]["intent_id"]

    closed_match = match_queued_day_orders(conn, request_ts_ms=_ts_ms("2026-04-23T20:05:00"))
    still_queued = conn.execute("SELECT status FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
    assert closed_match["matched"] == 0
    assert still_queued["status"] == "queued"

    opened_match = match_queued_day_orders(conn, request_ts_ms=_ts_ms("2026-04-24T10:00:00"))
    filled_intent = conn.execute("SELECT status, latest_order_id FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
    orders = conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
    fills = conn.execute("SELECT COUNT(*) AS n FROM paper_fills").fetchone()
    pos = conn.execute("SELECT * FROM positions WHERE ticker='600519.SH'").fetchone()

    assert opened_match["matched"] == 1
    assert opened_match["filled"] == 1
    assert filled_intent["status"] == "filled"
    assert filled_intent["latest_order_id"]
    assert int(orders["n"]) == 1
    assert int(fills["n"]) == 1
    assert pos is not None
    assert int(pos["qty"]) == 100

    second_match = match_queued_day_orders(conn, request_ts_ms=_ts_ms("2026-04-24T10:01:00"))
    orders_after = conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
    fills_after = conn.execute("SELECT COUNT(*) AS n FROM paper_fills").fetchone()
    assert second_match["matched"] == 0
    assert int(orders_after["n"]) == 1
    assert int(fills_after["n"]) == 1


def test_api_match_opening_endpoint_uses_clock():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)
    api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_order_payload(key="day-api-match-001", tif="DAY", request_ts="2026-04-23T20:00:00"),
        idempotency_key="day-api-match-001",
    )

    response = api.post_match_opening(
        conn,
        payload={"request_ts_ms": _ts_ms("2026-04-24T10:00:00")},
    )

    assert response["ok"] is True
    assert response["data"]["matched"] == 1
    assert response["data"]["filled"] == 1
