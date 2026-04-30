from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Iterator

from papertrade.event_stream import (
    SSEEventBus,
    format_sse_frame,
    format_sse_heartbeat,
    get_global_event_bus,
)
from papertrade.outbox import (
    OUTBOX_STATUS_DEAD,
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_PUBLISHED,
    list_outbox_events,
    list_outbox_events_since,
    outbox_row_to_envelope,
)

DEFAULT_EVENTS_LIMIT = 100
MAX_EVENTS_LIMIT = 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _resolve_statuses(
    *,
    include_pending: bool = True,
    include_dead: bool = False,
    statuses: Iterable[str] | None = None,
) -> tuple[str, ...]:
    if statuses is not None:
        cleaned = tuple(str(s).strip() for s in statuses if str(s).strip())
        if cleaned:
            return cleaned
    resolved: list[str] = [OUTBOX_STATUS_PUBLISHED]
    if include_pending:
        resolved.append(OUTBOX_STATUS_PENDING)
    if include_dead:
        resolved.append(OUTBOX_STATUS_DEAD)
    return tuple(resolved)


def _validate_limit(limit: int) -> int:
    val = int(limit)
    if val < 1 or val > MAX_EVENTS_LIMIT:
        raise ValueError(f"limit out of range: {val}")
    return val


def query_events(
    conn: Any,
    *,
    limit: int = DEFAULT_EVENTS_LIMIT,
    cursor: str | None = None,
    order: str = "desc",
    include_pending: bool = True,
    include_dead: bool = False,
    statuses: Iterable[str] | None = None,
) -> dict[str, Any]:
    limit_n = _validate_limit(limit)
    rows = list_outbox_events(
        conn,
        limit=limit_n,
        cursor=cursor,
        order=order,
        statuses=_resolve_statuses(
            include_pending=include_pending,
            include_dead=include_dead,
            statuses=statuses,
        ),
    )
    events = [outbox_row_to_envelope(row) for row in rows]
    next_cursor = events[-1]["event_id"] if len(events) >= limit_n else None
    return {
        "events": events,
        "next_cursor": next_cursor,
    }


def query_events_since(
    conn: Any,
    *,
    last_event_id: str | None,
    limit: int = MAX_EVENTS_LIMIT,
    include_pending: bool = True,
    include_dead: bool = False,
    statuses: Iterable[str] | None = None,
) -> dict[str, Any]:
    limit_n = _validate_limit(limit)
    rows = list_outbox_events_since(
        conn,
        last_event_id=last_event_id,
        limit=limit_n,
        statuses=_resolve_statuses(
            include_pending=include_pending,
            include_dead=include_dead,
            statuses=statuses,
        ),
    )
    events = [outbox_row_to_envelope(row) for row in rows]
    next_cursor = events[-1]["event_id"] if len(events) >= limit_n else None
    return {
        "events": events,
        "next_cursor": next_cursor,
    }


def build_events_response(
    conn: Any,
    *,
    limit: int = DEFAULT_EVENTS_LIMIT,
    cursor: str | None = None,
    order: str = "desc",
    include_pending: bool = True,
) -> dict[str, Any]:
    data = query_events(
        conn,
        limit=limit,
        cursor=cursor,
        order=order,
        include_pending=include_pending,
    )
    return {
        "ok": True,
        "data": data,
        "meta": {
            "correlation_id": _new_id("crr"),
            "ts_ms": _now_ms(),
        },
    }


def stream_sse_frames(
    *,
    event_bus: SSEEventBus | None = None,
    last_event_id: str | None = None,
    heartbeat_interval_seconds: float = 15.0,
    poll_seconds: float = 1.0,
    max_queue_size: int = 1000,
) -> Iterator[str]:
    bus = event_bus or get_global_event_bus()
    sub = bus.subscribe(last_event_id=last_event_id, max_queue_size=max_queue_size)
    try:
        for event in sub.iter_events(
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            poll_seconds=poll_seconds,
        ):
            if event is None:
                yield format_sse_heartbeat()
            else:
                yield format_sse_frame(event)
    finally:
        sub.close()
