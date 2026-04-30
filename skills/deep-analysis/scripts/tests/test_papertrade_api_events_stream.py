from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.api_events import query_events, query_events_since, stream_sse_frames  # noqa: E402
from papertrade.event_stream import SSEEventBus  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.outbox import enqueue_outbox_event  # noqa: E402


def _conn():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_query_events_cursor_and_since():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    ids: list[str] = []
    for idx in range(3):
        event_id = enqueue_outbox_event(
            conn,
            event_type="signal.generated",
            entity_type="signal",
            entity_id=str(1000 + idx),
            payload={"idx": idx},
        )
        ids.append(event_id)
        conn.commit()
        time.sleep(0.002)

    page1 = query_events(conn, limit=2, order="desc", include_pending=True)
    assert len(page1["events"]) == 2
    assert page1["events"][0]["event_id"] == ids[2]
    assert page1["events"][1]["event_id"] == ids[1]
    assert page1["next_cursor"] == ids[1]

    page2 = query_events(conn, limit=2, order="desc", cursor=page1["next_cursor"], include_pending=True)
    assert len(page2["events"]) == 1
    assert page2["events"][0]["event_id"] == ids[0]

    since_oldest = query_events_since(conn, last_event_id=ids[0], limit=10, include_pending=True)
    assert [evt["event_id"] for evt in since_oldest["events"]] == [ids[1], ids[2]]


def test_sse_last_event_id_replay_and_gap():
    bus = SSEEventBus(replay_buffer_size=10)
    bus.publish({"event_id": "evt_1", "event_type": "loop.started", "entity_type": "loop", "entity_id": "lp_1", "payload": {}})
    bus.publish({"event_id": "evt_2", "event_type": "signal.generated", "entity_type": "signal", "entity_id": "sg_2", "payload": {}})
    bus.publish({"event_id": "evt_3", "event_type": "loop.finished", "entity_type": "loop", "entity_id": "lp_1", "payload": {}})

    sub = bus.subscribe(last_event_id="evt_1")
    replay_1 = sub.poll(timeout_seconds=0.05)
    replay_2 = sub.poll(timeout_seconds=0.05)
    sub.close()

    assert replay_1 is not None
    assert replay_2 is not None
    assert replay_1["event_id"] == "evt_2"
    assert replay_2["event_id"] == "evt_3"

    sub_gap = bus.subscribe(last_event_id="evt_missing")
    gap_evt = sub_gap.poll(timeout_seconds=0.05)
    sub_gap.close()

    assert gap_evt is not None
    assert gap_evt["event_type"] == "stream.gap"
    assert gap_evt["payload"]["needs_backfill"] is True
    assert gap_evt["payload"]["last_event_id"] == "evt_missing"


def test_stream_sse_frames_outputs_gap_control_frame():
    bus = SSEEventBus(replay_buffer_size=3)
    bus.publish({"event_id": "evt_a", "event_type": "loop.started", "entity_type": "loop", "entity_id": "lp_a", "payload": {}})

    gen = stream_sse_frames(
        event_bus=bus,
        last_event_id="evt_not_found",
        heartbeat_interval_seconds=1.0,
        poll_seconds=0.05,
    )
    try:
        frame = next(gen)
    finally:
        gen.close()

    assert "event: stream.gap" in frame
    assert "needs_backfill" in frame
