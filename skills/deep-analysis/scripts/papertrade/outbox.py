from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Iterable

OUTBOX_STATUS_PENDING = "pending"
OUTBOX_STATUS_PUBLISHED = "published"
OUTBOX_STATUS_DEAD = "dead"
DEFAULT_EVENT_STATUSES = (OUTBOX_STATUS_PUBLISHED, OUTBOX_STATUS_PENDING)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _normalize_statuses(statuses: Iterable[str] | None) -> tuple[str, ...]:
    cleaned = tuple(str(s).strip() for s in (statuses or ()) if str(s).strip())
    return cleaned or DEFAULT_EVENT_STATUSES


def _as_dict(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return row if isinstance(row, dict) else dict(row)


def enqueue_outbox_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    source: str = "papertrade",
    severity: str = "info",
    correlation_id: str | None = None,
    causation_id: str | None = None,
    event_batch_id: str | None = None,
    status: str = "pending",
) -> str:
    event_id = _new_id("evt")
    now_ms = _now_ms()
    conn.execute(
        """
        INSERT INTO event_outbox (
            event_id, event_batch_id, event_type, entity_type, entity_id,
            source, severity, correlation_id, causation_id,
            payload_version, payload_json, status,
            publish_attempts, next_retry_at_ms, last_error, created_at_ms, published_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, NULL, ?, NULL)
        """,
        (
            event_id,
            event_batch_id,
            str(event_type),
            str(entity_type),
            str(entity_id),
            str(source),
            str(severity),
            str(correlation_id or _new_id("crr")),
            causation_id,
            json.dumps(payload or {}, ensure_ascii=False),
            str(status),
            now_ms,
            now_ms,
        ),
    )
    return event_id


def list_pending_outbox_events(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT *
        FROM event_outbox
        WHERE status=? AND (next_retry_at_ms IS NULL OR next_retry_at_ms <= ?)
        ORDER BY created_at_ms ASC
        LIMIT ?
        """,
        (OUTBOX_STATUS_PENDING, _now_ms(), int(limit)),
    )
    return [dict(r) for r in cur.fetchall()]


def get_outbox_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM event_outbox WHERE event_id=?", (str(event_id),)).fetchone()
    return dict(row) if row else None


def list_outbox_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    cursor: str | None = None,
    order: str = "desc",
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if int(limit) <= 0:
        return []

    order_norm = str(order or "desc").lower()
    if order_norm not in {"desc", "asc"}:
        raise ValueError(f"unsupported order: {order}")

    status_list = _normalize_statuses(statuses)
    where_parts = [f"status IN ({','.join('?' for _ in status_list)})"]
    params: list[Any] = list(status_list)

    if cursor:
        anchor = conn.execute(
            "SELECT created_at_ms, event_id FROM event_outbox WHERE event_id=?",
            (str(cursor),),
        ).fetchone()
        if anchor is None:
            return []
        anchor_ms = int(anchor["created_at_ms"])
        anchor_id = str(anchor["event_id"])
        if order_norm == "desc":
            where_parts.append("(created_at_ms < ? OR (created_at_ms = ? AND event_id < ?))")
        else:
            where_parts.append("(created_at_ms > ? OR (created_at_ms = ? AND event_id > ?))")
        params.extend([anchor_ms, anchor_ms, anchor_id])

    order_sql = "DESC" if order_norm == "desc" else "ASC"
    sql = f"""
        SELECT *
        FROM event_outbox
        WHERE {' AND '.join(where_parts)}
        ORDER BY created_at_ms {order_sql}, event_id {order_sql}
        LIMIT ?
    """
    params.append(int(limit))
    cur = conn.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def list_outbox_events_since(
    conn: sqlite3.Connection,
    *,
    last_event_id: str | None,
    limit: int = 1000,
    statuses: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if int(limit) <= 0:
        return []

    status_list = _normalize_statuses(statuses)
    params: list[Any] = list(status_list)
    where_parts = [f"status IN ({','.join('?' for _ in status_list)})"]

    if last_event_id:
        anchor = conn.execute(
            "SELECT created_at_ms, event_id FROM event_outbox WHERE event_id=?",
            (str(last_event_id),),
        ).fetchone()
        if anchor is None:
            return []
        anchor_ms = int(anchor["created_at_ms"])
        anchor_id = str(anchor["event_id"])
        where_parts.append("(created_at_ms > ? OR (created_at_ms = ? AND event_id > ?))")
        params.extend([anchor_ms, anchor_ms, anchor_id])

    sql = f"""
        SELECT *
        FROM event_outbox
        WHERE {' AND '.join(where_parts)}
        ORDER BY created_at_ms ASC, event_id ASC
        LIMIT ?
    """
    params.append(int(limit))
    cur = conn.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def outbox_row_to_envelope(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    data = _as_dict(row)
    payload_raw = data.get("payload_json")
    payload: dict[str, Any]
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
        if not isinstance(payload, dict):
            payload = {"raw": payload}
    except Exception:
        payload = {"raw": payload_raw}

    return {
        "event_id": str(data.get("event_id") or ""),
        "event_type": str(data.get("event_type") or ""),
        "entity_type": str(data.get("entity_type") or ""),
        "entity_id": str(data.get("entity_id") or ""),
        "ts_ms": int(data.get("created_at_ms") or 0),
        "source": str(data.get("source") or ""),
        "severity": str(data.get("severity") or "info"),
        "correlation_id": str(data.get("correlation_id") or ""),
        "causation_id": data.get("causation_id"),
        "event_batch_id": data.get("event_batch_id"),
        "payload_version": int(data.get("payload_version") or 1),
        "payload": payload,
        "status": str(data.get("status") or ""),
        "publish_attempts": int(data.get("publish_attempts") or 0),
        "published_at_ms": data.get("published_at_ms"),
        "last_error": data.get("last_error"),
    }


def mark_outbox_event_published(conn: sqlite3.Connection, event_id: str) -> None:
    now_ms = _now_ms()
    conn.execute(
        """
        UPDATE event_outbox
        SET status=?, published_at_ms=?, publish_attempts=publish_attempts+1, next_retry_at_ms=NULL, last_error=NULL
        WHERE event_id=?
        """,
        (OUTBOX_STATUS_PUBLISHED, now_ms, str(event_id)),
    )


def mark_outbox_event_retry(conn: sqlite3.Connection, event_id: str, *, error: str, retry_after_ms: int) -> None:
    next_retry = _now_ms() + max(0, int(retry_after_ms))
    conn.execute(
        """
        UPDATE event_outbox
        SET status=?, publish_attempts=publish_attempts+1, next_retry_at_ms=?, last_error=?
        WHERE event_id=?
        """,
        (OUTBOX_STATUS_PENDING, next_retry, str(error)[:500], str(event_id)),
    )


def mark_outbox_event_dead(conn: sqlite3.Connection, event_id: str, *, error: str) -> None:
    conn.execute(
        """
        UPDATE event_outbox
        SET status=?, publish_attempts=publish_attempts+1, next_retry_at_ms=NULL, last_error=?
        WHERE event_id=?
        """,
        (OUTBOX_STATUS_DEAD, str(error)[:500], str(event_id)),
    )
