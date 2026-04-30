from __future__ import annotations

import sqlite3
from typing import Any

from papertrade.api_common import now_ms
from papertrade.event_dispatcher import dispatch_outbox_once
from papertrade.runtime_store import (
    DEFAULT_RUNTIME_LEASE_NAME,
    DEFAULT_RUNTIME_LEASE_TTL_MS,
    acquire_runtime_lease,
    finish_runtime_session,
    get_runtime_lease,
)


def recover_expired_runtime_lease(
    conn: sqlite3.Connection,
    *,
    owner_session_id: str,
    ttl_ms: int = DEFAULT_RUNTIME_LEASE_TTL_MS,
    lease_name: str = DEFAULT_RUNTIME_LEASE_NAME,
    now: int | None = None,
    mark_stale_sessions_failed: bool = True,
) -> dict[str, Any]:
    ts = int(now if now is not None else now_ms())
    before = get_runtime_lease(conn, lease_name=lease_name)
    result = acquire_runtime_lease(
        conn,
        lease_name=lease_name,
        owner_session_id=owner_session_id,
        ttl_ms=ttl_ms,
        now_ms=ts,
    )

    if result.acquired and result.takeover and mark_stale_sessions_failed and before:
        stale_session_id = str(before.get("owner_session_id") or "")
        if stale_session_id and stale_session_id != owner_session_id:
            finish_runtime_session(
                conn,
                session_id=stale_session_id,
                status="failed",
                stop_reason="lease_expired_takeover",
                last_loop_id=None,
            )

    return {
        "recovered": bool(result.acquired),
        "takeover": bool(result.takeover),
        "before": before,
        "lease": result.as_dict(),
    }


def recover_outbox_once(
    db_path: str,
    *,
    max_attempts: int = 8,
) -> dict[str, Any]:
    return dispatch_outbox_once(db_path, max_attempts=max_attempts)
