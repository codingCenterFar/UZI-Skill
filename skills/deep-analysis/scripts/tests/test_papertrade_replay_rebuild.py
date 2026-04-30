from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.command_service import submit_intent_order  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db, record_nav, record_signal  # noqa: E402
from papertrade.outbox import enqueue_outbox_event  # noqa: E402
from papertrade.runtime_store import create_runtime_loop, finish_runtime_loop, start_runtime_session  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(dt_s: str) -> int:
    return int(datetime.fromisoformat(dt_s).timestamp() * 1000)


def _fact_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "signals",
        "order_intents",
        "paper_orders",
        "paper_fills",
        "position_lots",
        "lot_allocations",
        "event_outbox",
        "runtime_loops",
    )
    return {
        table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in tables
    }


def _seed_completed_loop(conn: sqlite3.Connection, *, loop_id: str = "loop_replay_001") -> dict:
    cfg = load_config()
    session_id = start_runtime_session(
        conn,
        start_request_id="pytest-replay-session",
        config_json=cfg.as_dict(),
    )
    event_batch_id = "eb_replay_001"
    create_runtime_loop(
        conn,
        loop_id=loop_id,
        session_id=session_id,
        loop_no=1,
        status="started",
        decision_version="pytest-v1",
        event_batch_id=event_batch_id,
    )
    enqueue_outbox_event(
        conn,
        event_type="loop.started",
        entity_type="loop",
        entity_id=loop_id,
        source="pytest",
        event_batch_id=event_batch_id,
        correlation_id=f"crr_{loop_id}",
        payload={"loop_id": loop_id},
    )
    signal_id = record_signal(
        conn,
        run_id=loop_id,
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        panel_mode="investor_panel",
        action="PAPER_BUY_A",
        score_final=82.0,
        score_strategy=80.0,
        score_panel=81.0,
        score_tactical=78.0,
        score_core=85.0,
        bonus_agent=0.0,
        penalties=[],
        gates=[],
        summary={"verdict_label": "buy"},
    )
    submit_intent_order(
        conn,
        run_id=loop_id,
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        source="auto",
        idempotency_key=f"auto:{loop_id}:600519.SH:PAPER_BUY_A:BUY:100:ORDER:2026-04-23",
        side="BUY",
        qty=100,
        price=100.0,
        action="PAPER_BUY_A",
        order_reason="pytest buy",
        order_type="LIMIT",
        time_in_force="IOC",
        signal_id=signal_id,
        loop_id=loop_id,
        operator_id="pytest",
        operator_channel="loop",
        request_ts_ms=_ts_ms("2026-04-23T10:00:00"),
        decision_basis="pytest",
    )
    nav = record_nav(
        conn,
        loop_id,
        "2026-04-23",
        cfg.trade.initial_cash,
        session_id=session_id,
        loop_id=loop_id,
    )
    enqueue_outbox_event(
        conn,
        event_type="loop.finished",
        entity_type="loop",
        entity_id=loop_id,
        source="pytest",
        event_batch_id=event_batch_id,
        correlation_id=f"crr_{loop_id}",
        payload={"loop_id": loop_id, "nav": nav},
    )
    finish_runtime_loop(
        conn,
        loop_id=loop_id,
        status="completed",
        ticker_count=1,
        success_count=1,
        failed_count=0,
        event_batch_id=event_batch_id,
        metrics={"event_queue_lag": 0},
    )
    conn.commit()
    return {"cfg": cfg, "session_id": session_id, "loop_id": loop_id}


def test_replay_loop_returns_reconstructed_key_read_models():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    seeded = _seed_completed_loop(conn)

    response = api.post_replay_loop(conn, payload={"loop_id": seeded["loop_id"]})

    assert response["ok"] is True
    data = response["data"]
    assert data["loop"]["loop_id"] == seeded["loop_id"]
    assert data["counts"]["signals"] == 1
    assert data["counts"]["intents"] == 1
    assert data["counts"]["orders"] == 1
    assert data["counts"]["fills"] == 1
    assert data["counts"]["events"] >= 2
    replay_pos = data["reconstructed"]["positions_from_loop_fills"][0]
    assert replay_pos["ticker"] == "600519.SH"
    assert replay_pos["qty"] == 100
    assert replay_pos["market_value"] == 10000.0
    current_pos = data["reconstructed"]["current_positions"][0]
    assert current_pos["qty"] == 100


def test_rebuild_readmodels_restores_projection_without_rewriting_fact_tables():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_completed_loop(conn)
    conn.execute("DELETE FROM positions")
    conn.execute("DELETE FROM paper_positions")
    conn.commit()
    before = _fact_counts(conn)

    response = api.post_rebuild_readmodels(
        conn,
        cfg=cfg,
        payload={"trade_date": "2026-04-24"},
    )
    after = _fact_counts(conn)

    assert response["ok"] is True
    assert response["data"]["facts_unchanged"] is True
    assert before == after
    assert response["data"]["rebuilt_count"] == 1
    pos = conn.execute("SELECT * FROM positions WHERE ticker='600519.SH'").fetchone()
    paper_pos = conn.execute("SELECT * FROM paper_positions WHERE ticker='600519.SH'").fetchone()
    nav = conn.execute(
        "SELECT * FROM portfolio_nav_snapshots WHERE loop_id LIKE 'rebuild:%' ORDER BY created_at_ms DESC LIMIT 1"
    ).fetchone()
    assert pos is not None
    assert paper_pos is not None
    assert nav is not None
    assert int(pos["qty"]) == 100
    assert int(pos["sellable_qty"]) == 100
    assert float(pos["market_value"]) == 10000.0
    assert int(float(paper_pos["quantity"])) == 100


def test_runtime_config_audit_records_history_and_config_changed_event():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)

    response = api.post_runtime_config(
        conn,
        cfg=cfg,
        payload={
            "scope": "runtime",
            "config": {"poll_seconds": 15, "watchlist_id": "wl_default"},
            "changed_by": "pytest",
            "change_reason": "tighten poll interval",
            "effective_from_ms": _ts_ms("2026-04-23T09:30:00"),
        },
    )

    assert response["ok"] is True
    change_id = response["data"]["config_change_id"]
    history = conn.execute(
        "SELECT * FROM system_config_history WHERE config_change_id=?",
        (change_id,),
    ).fetchone()
    event = conn.execute(
        "SELECT * FROM event_outbox WHERE event_type='config.changed' AND causation_id=?",
        (change_id,),
    ).fetchone()
    assert history is not None
    assert event is not None
    assert history["scope"] == "runtime"
    assert history["config_hash"] == response["data"]["config_hash"]
