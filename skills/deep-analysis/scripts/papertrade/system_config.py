from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from papertrade.api_common import PaperTradeAPIError, new_id, now_ms
from papertrade.outbox import enqueue_outbox_event


def _config_hash(config_json: dict[str, Any]) -> str:
    payload = json.dumps(config_json or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_config_change(
    conn: sqlite3.Connection,
    *,
    scope: str,
    config_json: dict[str, Any],
    changed_by: str | None = None,
    change_reason: str | None = None,
    effective_from_ms: int | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    scope_s = str(scope or "").strip()
    if not scope_s:
        raise PaperTradeAPIError("INVALID_REQUEST", "scope is required", {"field": "scope"})
    if not isinstance(config_json, dict):
        raise PaperTradeAPIError("INVALID_REQUEST", "config must be an object", {"field": "config"})

    ts_ms = int(effective_from_ms if effective_from_ms is not None else now_ms())
    cfg_hash = _config_hash(config_json)
    change_id = new_id("cfg")
    crr = correlation_id or new_id("crr")
    cfg_blob = json.dumps(config_json, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        INSERT INTO system_config_history (
            config_change_id, scope, config_hash, config_json,
            changed_by, change_reason, effective_from_ms, correlation_id, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            change_id,
            scope_s,
            cfg_hash,
            cfg_blob,
            changed_by,
            change_reason,
            ts_ms,
            crr,
            now_ms(),
        ),
    )
    event_id = enqueue_outbox_event(
        conn,
        event_type="config.changed",
        entity_type="system_config",
        entity_id=scope_s,
        source="system_config",
        severity="info",
        correlation_id=crr,
        causation_id=change_id,
        payload={
            "config_change_id": change_id,
            "scope": scope_s,
            "config_hash": cfg_hash,
            "changed_by": changed_by,
            "change_reason": change_reason,
            "effective_from_ms": ts_ms,
        },
    )
    return {
        "config_change_id": change_id,
        "scope": scope_s,
        "config_hash": cfg_hash,
        "event_id": event_id,
        "effective_from_ms": ts_ms,
    }
