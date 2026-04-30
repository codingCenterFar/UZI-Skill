from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return dict(payload)
    if payload is None:
        return {}
    return {"raw": payload}


def normalize_event_envelope(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(event.get("event_id") or _new_id("evt")),
        "event_type": str(event.get("event_type") or "message"),
        "entity_type": str(event.get("entity_type") or "event"),
        "entity_id": str(event.get("entity_id") or ""),
        "ts_ms": int(event.get("ts_ms") or _now_ms()),
        "source": str(event.get("source") or "event_stream"),
        "severity": str(event.get("severity") or "info"),
        "correlation_id": str(event.get("correlation_id") or _new_id("crr")),
        "causation_id": event.get("causation_id"),
        "event_batch_id": event.get("event_batch_id"),
        "payload_version": int(event.get("payload_version") or 1),
        "payload": _normalize_payload(event.get("payload")),
    }


def make_stream_gap_event(*, last_event_id: str, latest_event_id: str | None) -> dict[str, Any]:
    return normalize_event_envelope(
        {
            "event_id": _new_id("evt_stream_gap"),
            "event_type": "stream.gap",
            "entity_type": "event_stream",
            "entity_id": "events",
            "source": "event_stream",
            "severity": "warning",
            "causation_id": str(last_event_id),
            "payload": {
                "last_event_id": str(last_event_id),
                "latest_event_id": latest_event_id,
                "needs_backfill": True,
                "hint": "call GET /api/v1/events for reconciliation",
            },
        }
    )


def format_sse_frame(event: dict[str, Any]) -> str:
    evt = normalize_event_envelope(event)
    data = json.dumps(evt, ensure_ascii=False, separators=(",", ":"))
    return f"id: {evt['event_id']}\nevent: {evt['event_type']}\ndata: {data}\n\n"


def format_sse_heartbeat() -> str:
    return ": heartbeat\n\n"


def _push_non_blocking(target_queue: queue.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    try:
        target_queue.put_nowait(event)
        return
    except queue.Full:
        pass

    try:
        target_queue.get_nowait()
    except queue.Empty:
        return

    try:
        target_queue.put_nowait(event)
    except queue.Full:
        return


@dataclass
class EventSubscription:
    bus: "SSEEventBus"
    subscription_id: str
    _queue: queue.Queue[dict[str, Any]]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.bus.unsubscribe(self.subscription_id)

    def poll(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        if self._closed:
            return None
        timeout = max(0.0, float(timeout_seconds))
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def iter_events(
        self,
        *,
        heartbeat_interval_seconds: float = 15.0,
        poll_seconds: float = 1.0,
    ) -> Iterator[dict[str, Any] | None]:
        heartbeat = max(0.1, float(heartbeat_interval_seconds))
        poll_step = max(0.05, float(poll_seconds))
        next_heartbeat = time.monotonic() + heartbeat
        while not self._closed:
            remaining = max(0.0, next_heartbeat - time.monotonic())
            wait_seconds = min(poll_step, remaining)
            event = self.poll(timeout_seconds=wait_seconds)
            if event is not None:
                yield event
                next_heartbeat = time.monotonic() + heartbeat
                continue
            if time.monotonic() >= next_heartbeat:
                yield None
                next_heartbeat = time.monotonic() + heartbeat


class SSEEventBus:
    def __init__(self, replay_buffer_size: int = 2000):
        self._lock = threading.Lock()
        self._subscribers: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=max(1, int(replay_buffer_size)))
        self._latest_event_id: str | None = None

    @property
    def latest_event_id(self) -> str | None:
        with self._lock:
            return self._latest_event_id

    def publish(self, event: dict[str, Any]) -> str:
        normalized = normalize_event_envelope(event)
        with self._lock:
            self._recent_events.append(normalized)
            self._latest_event_id = normalized["event_id"]
            queues = list(self._subscribers.values())
        for target_queue in queues:
            _push_non_blocking(target_queue, normalized)
        return str(normalized["event_id"])

    def subscribe(self, *, last_event_id: str | None = None, max_queue_size: int = 1000) -> EventSubscription:
        queue_size = max(10, int(max_queue_size))
        sub_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        subscription_id = _new_id("sub")

        with self._lock:
            snapshot = list(self._recent_events)
            latest_event_id = self._latest_event_id
            replay_events: list[dict[str, Any]] = []
            if last_event_id:
                idx = next((i for i, item in enumerate(snapshot) if item.get("event_id") == str(last_event_id)), -1)
                if idx >= 0:
                    replay_events = snapshot[idx + 1 :]
                else:
                    replay_events = [make_stream_gap_event(last_event_id=str(last_event_id), latest_event_id=latest_event_id)]
            for evt in replay_events:
                _push_non_blocking(sub_queue, evt)
            self._subscribers[subscription_id] = sub_queue

        return EventSubscription(bus=self, subscription_id=subscription_id, _queue=sub_queue)

    def unsubscribe(self, subscription_id: str) -> None:
        with self._lock:
            self._subscribers.pop(str(subscription_id), None)


_GLOBAL_EVENT_BUS = SSEEventBus()


def get_global_event_bus() -> SSEEventBus:
    return _GLOBAL_EVENT_BUS
