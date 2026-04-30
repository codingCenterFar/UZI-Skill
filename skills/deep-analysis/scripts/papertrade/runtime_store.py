from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

DEFAULT_RUNTIME_LEASE_NAME = "papertrade_runtime"
DEFAULT_RUNTIME_LEASE_TTL_MS = 900_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _host() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


def _cfg_hash(config_json: dict[str, Any] | None) -> str:
    payload = json.dumps(config_json or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class RuntimeLeaseAcquireResult:
    acquired: bool
    lease_name: str
    owner_session_id: str | None
    owner_pid: int | None
    owner_host: str | None
    lease_expires_at_ms: int | None
    heartbeat_at_ms: int | None
    version: int | None
    reason: str | None = None
    takeover: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "acquired": bool(self.acquired),
            "lease_name": self.lease_name,
            "owner_session_id": self.owner_session_id,
            "owner_pid": self.owner_pid,
            "owner_host": self.owner_host,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "heartbeat_at_ms": self.heartbeat_at_ms,
            "version": self.version,
            "reason": self.reason,
            "takeover": bool(self.takeover),
        }


def get_runtime_lease(conn: sqlite3.Connection, *, lease_name: str = DEFAULT_RUNTIME_LEASE_NAME) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM runtime_leases WHERE lease_name=?",
        (str(lease_name),),
    ).fetchone()
    return dict(row) if row else None


def _result_from_row(
    *,
    lease_name: str,
    acquired: bool,
    row: dict[str, Any] | None,
    reason: str | None = None,
    takeover: bool = False,
) -> RuntimeLeaseAcquireResult:
    return RuntimeLeaseAcquireResult(
        acquired=acquired,
        lease_name=str(lease_name),
        owner_session_id=(str(row.get("owner_session_id")) if row and row.get("owner_session_id") is not None else None),
        owner_pid=(int(row.get("owner_pid")) if row and row.get("owner_pid") is not None else None),
        owner_host=(str(row.get("owner_host")) if row and row.get("owner_host") is not None else None),
        lease_expires_at_ms=(int(row.get("lease_expires_at_ms")) if row and row.get("lease_expires_at_ms") is not None else None),
        heartbeat_at_ms=(int(row.get("heartbeat_at_ms")) if row and row.get("heartbeat_at_ms") is not None else None),
        version=(int(row.get("version")) if row and row.get("version") is not None else None),
        reason=reason,
        takeover=takeover,
    )


def acquire_runtime_lease(
    conn: sqlite3.Connection,
    *,
    owner_session_id: str,
    lease_name: str = DEFAULT_RUNTIME_LEASE_NAME,
    ttl_ms: int = DEFAULT_RUNTIME_LEASE_TTL_MS,
    owner_pid: int | None = None,
    owner_host: str | None = None,
    now_ms: int | None = None,
) -> RuntimeLeaseAcquireResult:
    now = int(now_ms if now_ms is not None else _now_ms())
    ttl = max(1_000, int(ttl_ms))
    expires_at = now + ttl
    session_id = str(owner_session_id)
    pid = int(owner_pid if owner_pid is not None else os.getpid())
    host = str(owner_host or _host())
    name = str(lease_name)

    row = get_runtime_lease(conn, lease_name=name)
    if row is None:
        conn.execute(
            """
            INSERT INTO runtime_leases (
                lease_name, owner_session_id, owner_pid, owner_host,
                heartbeat_at_ms, lease_expires_at_ms, version, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (name, session_id, pid, host, now, expires_at, now),
        )
        inserted = get_runtime_lease(conn, lease_name=name)
        return _result_from_row(lease_name=name, acquired=True, row=inserted, reason=None, takeover=False)

    current_owner = str(row.get("owner_session_id") or "")
    row_version = int(row.get("version") or 0)
    row_exp = int(row.get("lease_expires_at_ms") or 0)
    expired = row_exp <= now

    if current_owner == session_id or expired:
        takeover = expired and current_owner != session_id
        updated = conn.execute(
            """
            UPDATE runtime_leases
            SET owner_session_id=?, owner_pid=?, owner_host=?,
                heartbeat_at_ms=?, lease_expires_at_ms=?, version=version+1, updated_at_ms=?
            WHERE lease_name=? AND version=?
            """,
            (session_id, pid, host, now, expires_at, now, name, row_version),
        )
        if int(updated.rowcount or 0) > 0:
            refreshed = get_runtime_lease(conn, lease_name=name)
            return _result_from_row(lease_name=name, acquired=True, row=refreshed, reason=None, takeover=takeover)
        row = get_runtime_lease(conn, lease_name=name)
        return _result_from_row(lease_name=name, acquired=False, row=row, reason="lease_race_lost")

    return _result_from_row(lease_name=name, acquired=False, row=row, reason="lease_owned_by_other")


def renew_runtime_lease(
    conn: sqlite3.Connection,
    *,
    owner_session_id: str,
    lease_name: str = DEFAULT_RUNTIME_LEASE_NAME,
    ttl_ms: int = DEFAULT_RUNTIME_LEASE_TTL_MS,
    now_ms: int | None = None,
) -> bool:
    now = int(now_ms if now_ms is not None else _now_ms())
    ttl = max(1_000, int(ttl_ms))
    expires_at = now + ttl
    updated = conn.execute(
        """
        UPDATE runtime_leases
        SET heartbeat_at_ms=?, lease_expires_at_ms=?, version=version+1, updated_at_ms=?
        WHERE lease_name=? AND owner_session_id=? AND lease_expires_at_ms > ?
        """,
        (now, expires_at, now, str(lease_name), str(owner_session_id), now),
    )
    return int(updated.rowcount or 0) > 0


def ensure_runtime_lease_owner(
    conn: sqlite3.Connection,
    *,
    owner_session_id: str,
    lease_name: str = DEFAULT_RUNTIME_LEASE_NAME,
    now_ms: int | None = None,
) -> bool:
    now = int(now_ms if now_ms is not None else _now_ms())
    row = conn.execute(
        """
        SELECT 1
        FROM runtime_leases
        WHERE lease_name=? AND owner_session_id=? AND lease_expires_at_ms > ?
        """,
        (str(lease_name), str(owner_session_id), now),
    ).fetchone()
    return row is not None


def release_runtime_lease(
    conn: sqlite3.Connection,
    *,
    owner_session_id: str,
    lease_name: str = DEFAULT_RUNTIME_LEASE_NAME,
    now_ms: int | None = None,
) -> bool:
    now = int(now_ms if now_ms is not None else _now_ms())
    updated = conn.execute(
        """
        UPDATE runtime_leases
        SET lease_expires_at_ms=?, heartbeat_at_ms=?, version=version+1, updated_at_ms=?
        WHERE lease_name=? AND owner_session_id=?
        """,
        (now, now, now, str(lease_name), str(owner_session_id)),
    )
    return int(updated.rowcount or 0) > 0


def start_runtime_session(
    conn: sqlite3.Connection,
    *,
    start_request_id: str | None,
    config_json: dict[str, Any] | None,
) -> str:
    now_ms = _now_ms()
    session_id = _new_id("rs")
    cfg_json = config_json or {}
    conn.execute(
        """
        INSERT INTO runtime_sessions (
            session_id, status, start_request_id, owner_pid, owner_host,
            config_hash, config_json,
            started_at_ms, heartbeat_at_ms, ended_at_ms, last_loop_id, stop_reason,
            created_at_ms, updated_at_ms
        ) VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
        """,
        (
            session_id,
            start_request_id,
            int(os.getpid()),
            _host(),
            _cfg_hash(cfg_json),
            json.dumps(cfg_json, ensure_ascii=False),
            now_ms,
            now_ms,
            now_ms,
            now_ms,
        ),
    )
    return session_id


def heartbeat_runtime_session(conn: sqlite3.Connection, *, session_id: str, last_loop_id: str | None = None) -> None:
    now_ms = _now_ms()
    conn.execute(
        """
        UPDATE runtime_sessions
        SET heartbeat_at_ms=?, updated_at_ms=?, last_loop_id=COALESCE(?, last_loop_id)
        WHERE session_id=?
        """,
        (now_ms, now_ms, last_loop_id, str(session_id)),
    )


def finish_runtime_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    status: str,
    stop_reason: str | None,
    last_loop_id: str | None,
) -> None:
    now_ms = _now_ms()
    conn.execute(
        """
        UPDATE runtime_sessions
        SET status=?, stop_reason=?, ended_at_ms=?, heartbeat_at_ms=?, updated_at_ms=?,
            last_loop_id=COALESCE(?, last_loop_id)
        WHERE session_id=?
        """,
        (
            str(status),
            stop_reason,
            now_ms,
            now_ms,
            now_ms,
            last_loop_id,
            str(session_id),
        ),
    )


def create_runtime_loop(
    conn: sqlite3.Connection,
    *,
    loop_id: str,
    session_id: str,
    loop_no: int,
    status: str = "started",
    snapshot_ts_ms: int | None = None,
    decision_version: str | None = None,
    event_batch_id: str | None = None,
) -> None:
    now_ms = _now_ms()
    conn.execute(
        """
        INSERT OR REPLACE INTO runtime_loops (
            loop_id, session_id, loop_no, status,
            snapshot_ts_ms, decision_version, event_batch_id,
            decision_basis_summary_json, staleness_summary_json,
            ticker_count, success_count, failed_count,
            started_at_ms, ended_at_ms, duration_ms, db_write_duration_ms,
            metrics_json, error_json, retry_of_loop_id,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, NULL, NULL, NULL, ?, NULL, NULL, ?, ?)
        """,
        (
            str(loop_id),
            str(session_id),
            int(loop_no),
            str(status),
            int(snapshot_ts_ms or now_ms),
            decision_version,
            event_batch_id,
            json.dumps({}, ensure_ascii=False),
            json.dumps({}, ensure_ascii=False),
            now_ms,
            json.dumps({}, ensure_ascii=False),
            now_ms,
            now_ms,
        ),
    )


def finish_runtime_loop(
    conn: sqlite3.Connection,
    *,
    loop_id: str,
    status: str,
    ticker_count: int,
    success_count: int,
    failed_count: int,
    db_write_duration_ms: int | None = None,
    error_json: dict[str, Any] | None = None,
    decision_basis_summary: dict[str, Any] | None = None,
    staleness_summary: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    event_batch_id: str | None = None,
) -> None:
    now_ms = _now_ms()
    row = conn.execute("SELECT started_at_ms FROM runtime_loops WHERE loop_id=?", (str(loop_id),)).fetchone()
    started_at_ms = int(row["started_at_ms"]) if row and row["started_at_ms"] is not None else now_ms
    duration_ms = max(0, now_ms - started_at_ms)

    conn.execute(
        """
        UPDATE runtime_loops
        SET status=?,
            event_batch_id=COALESCE(?, event_batch_id),
            decision_basis_summary_json=?,
            staleness_summary_json=?,
            ticker_count=?, success_count=?, failed_count=?,
            ended_at_ms=?, duration_ms=?, db_write_duration_ms=?,
            metrics_json=?,
            error_json=?, updated_at_ms=?
        WHERE loop_id=?
        """,
        (
            str(status),
            event_batch_id,
            json.dumps(decision_basis_summary or {}, ensure_ascii=False),
            json.dumps(staleness_summary or {}, ensure_ascii=False),
            int(ticker_count),
            int(success_count),
            int(failed_count),
            now_ms,
            duration_ms,
            db_write_duration_ms,
            json.dumps(metrics or {}, ensure_ascii=False),
            json.dumps(error_json, ensure_ascii=False) if error_json else None,
            now_ms,
            str(loop_id),
        ),
    )
