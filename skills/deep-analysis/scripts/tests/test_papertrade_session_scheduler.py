from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.candidate_pool import get_candidate_pool_top  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db, record_signal  # noqa: E402
from papertrade.market_calendar_service import MarketCalendarService  # noqa: E402
from papertrade.session_scheduler import (  # noqa: E402
    build_session_plan,
    expire_queued_day_orders,
    rebuild_candidate_pool_after_close,
    record_after_close_review,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _order_payload(*, key: str, request_ts: str) -> dict:
    return {
        "ticker": "600519.SH",
        "side": "BUY",
        "qty": 100,
        "order_type": "LIMIT",
        "time_in_force": "DAY",
        "limit_price": 100.0,
        "trade_date": "2026-04-23",
        "request_ts_ms": _ts_ms(request_ts),
        "client_order_id": key,
    }


def test_session_plan_maps_market_phases_to_scheduler_actions():
    refresh = {"mode": "cached_cycle", "cycle_from_cache_only": True}
    cases = [
        ("2026-04-24T09:00:00", "pre_open", False, False, False),
        ("2026-04-24T10:00:00", "open_morning", True, True, False),
        ("2026-04-24T12:00:00", "lunch", False, False, False),
        ("2026-04-24T14:30:00", "open_afternoon", True, True, False),
        ("2026-04-24T14:58:00", "closing", True, True, False),
        ("2026-04-24T15:30:00", "after_close", False, False, True),
    ]
    for ts_s, phase, is_open, match_opening, expire_day in cases:
        plan = build_session_plan(request_ts_ms=_ts_ms(ts_s), refresh_mode=refresh)
        assert plan["scheduler_phase"] == phase
        assert plan["is_open_session"] is is_open
        assert plan["match_opening"] is match_opening
        assert plan["expire_day_orders"] is expire_day
        assert plan["actions"]


def test_day_order_gets_next_close_expiry_and_expires_after_close():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    queued = api.post_sim_order(
        conn,
        cfg=cfg,
        payload=_order_payload(key="day-expire-001", request_ts="2026-04-23T20:00:00"),
        idempotency_key="day-expire-001",
    )
    assert queued["ok"] is True
    intent_id = queued["data"]["intent"]["intent_id"]

    row = conn.execute("SELECT status, expires_at_ms FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
    assert row["status"] == "queued"
    assert int(row["expires_at_ms"]) == MarketCalendarService().close_ts_ms("2026-04-24")

    before_close = expire_queued_day_orders(conn, request_ts_ms=_ts_ms("2026-04-24T14:59:00"))
    still_queued = conn.execute("SELECT status FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
    assert before_close["expired"] == 0
    assert still_queued["status"] == "queued"

    after_close = expire_queued_day_orders(
        conn,
        request_ts_ms=_ts_ms("2026-04-24T15:01:00"),
        session_id="rs_pytest",
        loop_no=3,
    )
    expired = conn.execute("SELECT status, reject_code FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
    evt = conn.execute("SELECT payload_json FROM event_outbox WHERE event_type='intent.expired'").fetchone()

    assert after_close["expired"] == 1
    assert expired["status"] == "expired"
    assert expired["reject_code"] == "DAY_ORDER_EXPIRED"
    payload = json.loads(evt["payload_json"])
    assert payload["session_id"] == "rs_pytest"
    assert payload["scheduler_phase"] == "after_close"


def test_after_close_review_records_outbox_event():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    review = record_after_close_review(
        conn,
        request_ts_ms=_ts_ms("2026-04-24T15:30:00"),
        session_id="rs_review",
        loop_no=9,
        ticker_universe={"ticker_count": 2},
    )
    evt = conn.execute(
        "SELECT * FROM event_outbox WHERE event_type='session.after_close_review'"
    ).fetchone()

    assert review["recorded"] is True
    assert review["scheduler_phase"] == "after_close"
    assert review["ticker_count"] == 2
    assert evt is not None
    payload = json.loads(evt["payload_json"])
    assert payload["session_id"] == "rs_review"
    assert payload["loop_no"] == 9


def test_after_close_candidate_pool_rebuild_uses_local_facts_only(tmp_path: Path):
    conn = _conn()
    cfg = load_config(overrides={"cache_root": str(tmp_path / "cache")})
    init_db(conn, initial_cash=1_000_000.0)
    record_signal(
        conn,
        run_id="run_after_close",
        as_of_date="2026-04-24",
        ticker="600519.SH",
        market="A",
        panel_mode="investor_panel",
        action="CANDIDATE_A",
        score_final=70.0,
        score_strategy=68.0,
        score_panel=71.0,
        score_tactical=66.0,
        score_core=72.0,
        bonus_agent=4.0,
        penalties=[],
        gates=[],
        summary={"agent_reviewed": True, "verdict_label": "candidate"},
    )

    rebuilt = rebuild_candidate_pool_after_close(
        conn,
        cfg=cfg,
        request_ts_ms=_ts_ms("2026-04-24T15:30:00"),
        session_id="rs_rebuild",
        loop_no=11,
        pool_name="after_close_pool",
        cache_roots=[str(tmp_path / "cache")],
        max_candidates=5,
    )
    pool = get_candidate_pool_top(conn, pool_name="after_close_pool", limit=5)
    evt = conn.execute("SELECT payload_json FROM event_outbox WHERE event_type='candidate_pool.rebuilt'").fetchone()

    assert rebuilt["rebuilt"] is True
    assert rebuilt["item_count"] == 1
    assert pool["items"][0]["ticker"] == "600519.SH"
    assert evt is not None
    payload = json.loads(evt["payload_json"])
    assert payload["session_id"] == "rs_rebuild"
