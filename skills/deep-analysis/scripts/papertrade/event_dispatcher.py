from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from papertrade.event_stream import get_global_event_bus
from papertrade.ledger import connect
from papertrade.outbox import (
    list_pending_outbox_events,
    mark_outbox_event_dead,
    mark_outbox_event_published,
    mark_outbox_event_retry,
    outbox_row_to_envelope,
)

EventPublisher = Callable[[dict[str, Any]], None]


@dataclass
class DispatchStats:
    scanned: int = 0
    published: int = 0
    retried: int = 0
    dead: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": int(self.scanned),
            "published": int(self.published),
            "retried": int(self.retried),
            "dead": int(self.dead),
            "errors": list(self.errors),
        }


def _retry_delay_ms(*, attempt_no: int, base_ms: int, max_ms: int) -> int:
    # attempt_no is 1-based after the current failed publish attempt.
    exp = max(0, int(attempt_no) - 1)
    return min(int(max_ms), int(base_ms) * (2 ** exp))


class OutboxDispatcher:
    def __init__(
        self,
        *,
        conn_factory: Callable[[], Any],
        publisher: EventPublisher,
        batch_size: int = 200,
        retry_base_ms: int = 1_000,
        retry_max_ms: int = 60_000,
        max_attempts: int = 8,
    ):
        self._conn_factory = conn_factory
        self._publisher = publisher
        self._batch_size = max(1, int(batch_size))
        self._retry_base_ms = max(1, int(retry_base_ms))
        self._retry_max_ms = max(self._retry_base_ms, int(retry_max_ms))
        self._max_attempts = max(1, int(max_attempts))

    def dispatch_once(self) -> DispatchStats:
        stats = DispatchStats()
        conn = self._conn_factory()
        try:
            rows = list_pending_outbox_events(conn, limit=self._batch_size)
            stats.scanned = len(rows)
            if not rows:
                return stats

            for row in rows:
                event_id = str(row.get("event_id") or "")
                attempts_before = int(row.get("publish_attempts") or 0)
                envelope = outbox_row_to_envelope(row)
                try:
                    self._publisher(envelope)
                except Exception as exc:
                    err_text = str(exc).strip() or exc.__class__.__name__
                    attempt_no = attempts_before + 1
                    if attempt_no >= self._max_attempts:
                        mark_outbox_event_dead(conn, event_id, error=err_text)
                        stats.dead += 1
                    else:
                        delay_ms = _retry_delay_ms(
                            attempt_no=attempt_no,
                            base_ms=self._retry_base_ms,
                            max_ms=self._retry_max_ms,
                        )
                        mark_outbox_event_retry(
                            conn,
                            event_id,
                            error=err_text,
                            retry_after_ms=delay_ms,
                        )
                        stats.retried += 1
                    stats.errors.append(f"{event_id}: {err_text}")
                else:
                    mark_outbox_event_published(conn, event_id)
                    stats.published += 1

            conn.commit()
            return stats
        finally:
            conn.close()

    def dispatch_forever(
        self,
        *,
        poll_interval_seconds: float = 0.5,
        stop_when: Callable[[], bool] | None = None,
    ) -> None:
        interval = max(0.0, float(poll_interval_seconds))
        while True:
            if stop_when and stop_when():
                return
            stats = self.dispatch_once()
            if stats.scanned == 0 and interval > 0:
                time.sleep(interval)


def make_sqlite_conn_factory(db_path: str | Path) -> Callable[[], Any]:
    path = Path(db_path)

    def _factory() -> Any:
        return connect(path)

    return _factory


def dispatch_outbox_once(
    db_path: str | Path,
    *,
    publisher: EventPublisher | None = None,
    batch_size: int = 200,
    retry_base_ms: int = 1_000,
    retry_max_ms: int = 60_000,
    max_attempts: int = 8,
) -> dict[str, Any]:
    event_bus = get_global_event_bus()
    dispatch = OutboxDispatcher(
        conn_factory=make_sqlite_conn_factory(db_path),
        publisher=publisher or event_bus.publish,
        batch_size=batch_size,
        retry_base_ms=retry_base_ms,
        retry_max_ms=retry_max_ms,
        max_attempts=max_attempts,
    )
    return dispatch.dispatch_once().as_dict()
