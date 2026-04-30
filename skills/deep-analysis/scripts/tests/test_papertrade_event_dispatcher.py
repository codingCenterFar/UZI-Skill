from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.api_events import query_events  # noqa: E402
from papertrade.event_dispatcher import OutboxDispatcher, make_sqlite_conn_factory  # noqa: E402
from papertrade.ledger import connect, init_db  # noqa: E402
from papertrade.outbox import enqueue_outbox_event  # noqa: E402


def _init_db_file(tmp_path: Path) -> Path:
    db_path = tmp_path / "papertrade_dispatcher.db"
    conn = connect(db_path)
    init_db(conn, initial_cash=1_000_000.0)
    conn.close()
    return db_path


def test_dispatcher_marks_event_published(tmp_path: Path):
    db_path = _init_db_file(tmp_path)
    conn = connect(db_path)
    event_id = enqueue_outbox_event(
        conn,
        event_type="loop.finished",
        entity_type="loop",
        entity_id="lp_001",
        payload={"ok": True},
    )
    conn.commit()
    conn.close()

    seen: list[dict] = []

    def _publisher(evt: dict) -> None:
        seen.append(evt)

    dispatcher = OutboxDispatcher(
        conn_factory=make_sqlite_conn_factory(db_path),
        publisher=_publisher,
    )
    stats = dispatcher.dispatch_once()

    check = connect(db_path)
    row = check.execute("SELECT status, publish_attempts, published_at_ms FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()
    check.close()

    assert stats.scanned == 1
    assert stats.published == 1
    assert stats.retried == 0
    assert stats.dead == 0
    assert len(seen) == 1
    assert seen[0]["event_id"] == event_id
    assert row is not None
    assert row["status"] == "published"
    assert int(row["publish_attempts"]) == 1
    assert int(row["published_at_ms"]) > 0


def test_dispatcher_retry_then_publish_and_events_can_backfill(tmp_path: Path):
    db_path = _init_db_file(tmp_path)
    conn = connect(db_path)
    event_id = enqueue_outbox_event(
        conn,
        event_type="order.filled",
        entity_type="order",
        entity_id="1001",
        payload={"ticker": "600519.SH"},
    )
    conn.commit()
    conn.close()

    calls = {"n": 0}

    def _flaky_publisher(_: dict) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sse temporarily unavailable")

    dispatcher = OutboxDispatcher(
        conn_factory=make_sqlite_conn_factory(db_path),
        publisher=_flaky_publisher,
        retry_base_ms=0,
        retry_max_ms=0,
        max_attempts=3,
    )

    first = dispatcher.dispatch_once()
    check = connect(db_path)
    row_after_first = check.execute("SELECT status, publish_attempts FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()
    backfill = query_events(check, limit=10, include_pending=True)
    check.close()

    assert first.scanned == 1
    assert first.published == 0
    assert first.retried == 1
    assert first.dead == 0
    assert row_after_first is not None
    assert row_after_first["status"] == "pending"
    assert int(row_after_first["publish_attempts"]) == 1
    assert any(evt["event_id"] == event_id and evt["status"] == "pending" for evt in backfill["events"])

    time.sleep(0.002)
    second = dispatcher.dispatch_once()
    check2 = connect(db_path)
    row_after_second = check2.execute("SELECT status, publish_attempts FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()
    check2.close()

    assert second.scanned == 1
    assert second.published == 1
    assert second.retried == 0
    assert second.dead == 0
    assert row_after_second is not None
    assert row_after_second["status"] == "published"
    assert int(row_after_second["publish_attempts"]) == 2


def test_dispatcher_marks_dead_after_max_attempts(tmp_path: Path):
    db_path = _init_db_file(tmp_path)
    conn = connect(db_path)
    event_id = enqueue_outbox_event(
        conn,
        event_type="intent.rejected",
        entity_type="intent",
        entity_id="it_001",
        payload={"reject_code": "RULE_TPLUS1_VIOLATION"},
    )
    conn.commit()
    conn.close()

    def _always_fail(_: dict) -> None:
        raise RuntimeError("sse hard failure")

    dispatcher = OutboxDispatcher(
        conn_factory=make_sqlite_conn_factory(db_path),
        publisher=_always_fail,
        retry_base_ms=0,
        retry_max_ms=0,
        max_attempts=2,
    )

    first = dispatcher.dispatch_once()
    time.sleep(0.003)
    second = dispatcher.dispatch_once()

    check = connect(db_path)
    row = check.execute("SELECT status, publish_attempts, last_error FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()
    check.close()

    assert first.retried == 1
    assert second.dead == 1
    assert row is not None
    assert row["status"] == "dead"
    assert int(row["publish_attempts"]) == 2
    assert "sse hard failure" in str(row["last_error"] or "")
