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


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(dt_s: str) -> int:
    return int(datetime.fromisoformat(dt_s).timestamp() * 1000)


def _buy_payload(key: str, qty: int = 100) -> dict:
    return {
        "ticker": "600519.SH",
        "side": "BUY",
        "qty": qty,
        "order_type": "LIMIT",
        "limit_price": 100.0,
        "trade_date": "2026-04-23",
        "request_ts_ms": _ts_ms("2026-04-23T10:00:00"),
        "client_order_id": key,
        "operator": {"operator_id": "tester", "channel": "pytest"},
    }


def test_read_model_api_returns_uniform_envelopes_after_manual_order():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    buy = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_buy_payload("api-buy-read-001"),
        idempotency_key="api-buy-read-001",
    )
    assert buy["ok"] is True

    positions = api.get_positions(conn)
    assert positions["ok"] is True
    assert positions["data"]["items"][0]["ticker"] == "600519.SH"
    assert positions["data"]["items"][0]["sellable_qty"] == 0

    watchlist = api.get_watchlist(conn, watchlist_id="wl_default")
    assert watchlist["ok"] is True
    assert watchlist["data"]["items"][0]["ticker"] == "600519.SH"
    assert "analysis_age_ms" in watchlist["data"]["items"][0]
    assert "decision_basis" in watchlist["data"]["items"][0]

    ticker = api.get_ticker(conn, "600519.SH")
    assert ticker["ok"] is True
    assert ticker["data"]["position"]["qty"] == 100
    assert ticker["data"]["recent_orders"][0]["intent_id"] == buy["data"]["intent"]["intent_id"]

    dashboard = api.get_dashboard_summary(conn)
    assert dashboard["ok"] is True
    assert dashboard["data"]["equity"] > 0
    assert "event_queue_lag" in dashboard["data"]["freshness"]


def test_post_sim_order_rule_reject_uses_error_envelope_and_persists_audit():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    buy = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_buy_payload("api-buy-tplus1-001"),
        idempotency_key="api-buy-tplus1-001",
    )
    assert buy["ok"] is True

    sell = api.post_sim_order(
        conn,
        cfg=cfg,
        payload={
            "ticker": "600519.SH",
            "side": "SELL",
            "qty": 100,
            "order_type": "LIMIT",
            "limit_price": 101.0,
            "trade_date": "2026-04-23",
            "request_ts_ms": _ts_ms("2026-04-23T10:05:00"),
            "client_order_id": "api-sell-tplus1-001",
        },
        idempotency_key="api-sell-tplus1-001",
    )

    assert sell["ok"] is False
    assert sell["error"]["error_code"] == "RULE_TPLUS1_VIOLATION"
    assert sell["error"]["details"]["intent_id"]
    assert sell["error"]["details"]["rule_check_snapshot_id"]
    assert sell["error"]["details"]["sellable_qty"] == 0

    intent = conn.execute(
        "SELECT * FROM order_intents WHERE intent_id=?",
        (sell["error"]["details"]["intent_id"],),
    ).fetchone()
    assert intent is not None
    assert intent["status"] == "rejected"
    checks = conn.execute(
        "SELECT COUNT(*) AS n FROM order_rule_checks WHERE intent_id=?",
        (sell["error"]["details"]["intent_id"],),
    ).fetchone()
    assert int(checks["n"]) == 1


def test_post_sim_order_idempotency_conflict_is_api_error():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    first = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_buy_payload("api-buy-conflict-001", qty=100),
        idempotency_key="api-buy-conflict-001",
    )
    second = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_buy_payload("api-buy-conflict-001", qty=200),
        idempotency_key="api-buy-conflict-001",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["error_code"] == "IDEMPOTENCY_CONFLICT"

    intents = conn.execute(
        "SELECT COUNT(*) AS n FROM order_intents WHERE idempotency_key='api-buy-conflict-001'",
    ).fetchone()
    assert int(intents["n"]) == 1


def test_runtime_start_status_stop_use_uniform_runtime_errors():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    started = api.post_runtime_start(
        conn,
        cfg=cfg,
        payload={"poll_seconds": 5, "watchlist_id": "wl_default"},
        idempotency_key="runtime-start-001",
    )
    assert started["ok"] is True
    assert started["data"]["status"] == "running"

    status = api.get_runtime_status(conn)
    assert status["ok"] is True
    assert status["data"]["runtime"]["session_id"] == started["data"]["session_id"]

    blocked = api.post_runtime_start(
        conn,
        cfg=cfg,
        payload={"poll_seconds": 5},
        idempotency_key="runtime-start-002",
    )
    assert blocked["ok"] is False
    assert blocked["error"]["error_code"] == "RUNTIME_ALREADY_RUNNING"

    stopped = api.post_runtime_stop(
        conn,
        payload={"session_id": started["data"]["session_id"], "reason": "pytest_stop"},
    )
    assert stopped["ok"] is True
    assert stopped["data"]["status"] == "stopped"

    after_stop = api.get_runtime_status(conn)
    assert after_stop["ok"] is False
    assert after_stop["error"]["error_code"] == "RUNTIME_NOT_RUNNING"
