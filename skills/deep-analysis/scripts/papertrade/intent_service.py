from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any


VALID_STATUSES = {
    "intent",
    "validated",
    "queued",
    "rejected",
    "filled",
    "partial",
    "cancelled",
    "expired",
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "intent": {"validated", "rejected", "cancelled"},
    "validated": {"queued", "rejected", "cancelled", "expired"},
    "queued": {"filled", "partial", "rejected", "cancelled", "expired"},
    "partial": {"filled", "cancelled", "expired"},
    "rejected": set(),
    "filled": set(),
    "cancelled": set(),
    "expired": set(),
}


class IntentError(RuntimeError):
    pass


class IntentNotFoundError(IntentError):
    pass


class IdempotencyConflictError(IntentError):
    pass


class InvalidTransitionError(IntentError):
    pass


@dataclass
class IntentCreateResult:
    intent_id: str
    status: str
    reused: bool
    row: dict[str, Any]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_side(side: str) -> str:
    out = str(side or "").upper().strip()
    if out not in {"BUY", "SELL"}:
        raise ValueError(f"invalid side: {side}")
    return out


def _norm_source(source: str) -> str:
    out = str(source or "").strip().lower()
    if out not in {"auto", "manual", "system_replay"}:
        raise ValueError(f"invalid source: {source}")
    return out


def _stable_request_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def get_intent(conn: sqlite3.Connection, intent_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM order_intents WHERE intent_id=?", (intent_id,)).fetchone()
    return dict(row) if row else None


def get_intent_by_idempotency(conn: sqlite3.Connection, source: str, idempotency_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM order_intents WHERE source=? AND idempotency_key=?",
        (_norm_source(source), str(idempotency_key)),
    ).fetchone()
    return dict(row) if row else None


def create_intent(
    conn: sqlite3.Connection,
    *,
    source: str,
    idempotency_key: str,
    ticker: str,
    side: str,
    qty: int,
    order_type: str = "MARKET",
    limit_price: float | None = None,
    time_in_force: str = "IOC",
    signal_id: int | None = None,
    loop_id: str | None = None,
    operator_id: str | None = None,
    operator_channel: str | None = None,
    decision_snapshot_id: str | None = None,
    rule_check_snapshot_id: str | None = None,
    status: str = "intent",
    correlation_id: str | None = None,
    causation_id: str | None = None,
    expires_at_ms: int | None = None,
) -> IntentCreateResult:
    src = _norm_source(source)
    sd = _norm_side(side)
    status = str(status or "intent").strip().lower()
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")

    if int(qty) <= 0:
        raise ValueError("qty must be > 0")

    req_payload = {
        "ticker": str(ticker).strip().upper(),
        "side": sd,
        "qty": int(qty),
        "order_type": str(order_type or "MARKET").strip().upper(),
        "limit_price": float(limit_price) if limit_price is not None else None,
        "time_in_force": str(time_in_force or "IOC").strip().upper(),
        "signal_id": signal_id,
        "loop_id": loop_id,
    }
    req_hash = _stable_request_hash(req_payload)
    existing = get_intent_by_idempotency(conn, src, idempotency_key)
    if existing:
        if str(existing.get("request_hash") or "") != req_hash:
            raise IdempotencyConflictError(
                f"idempotency key reused with different payload: source={src}, key={idempotency_key}"
            )
        return IntentCreateResult(
            intent_id=str(existing["intent_id"]),
            status=str(existing["status"]),
            reused=True,
            row=existing,
        )

    now_ms = _now_ms()
    intent_id = _new_id("it")
    correlation_id = correlation_id or _new_id("crr")
    conn.execute(
        """
        INSERT INTO order_intents (
            intent_id, signal_id, loop_id, source, operator_id, operator_channel,
            idempotency_key, request_hash, ticker, side, qty, order_type, limit_price,
            time_in_force, status, decision_snapshot_id, rule_check_snapshot_id,
            latest_order_id, reject_code, reject_reason, expires_at_ms,
            correlation_id, causation_id, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            signal_id,
            loop_id,
            src,
            operator_id,
            operator_channel,
            str(idempotency_key),
            req_hash,
            req_payload["ticker"],
            sd,
            int(qty),
            req_payload["order_type"],
            req_payload["limit_price"],
            req_payload["time_in_force"],
            status,
            decision_snapshot_id,
            rule_check_snapshot_id,
            None,
            None,
            None,
            expires_at_ms,
            correlation_id,
            causation_id,
            now_ms,
            now_ms,
        ),
    )
    row = get_intent(conn, intent_id)
    return IntentCreateResult(
        intent_id=intent_id,
        status=status,
        reused=False,
        row=row or {},
    )


def transition_intent_status(
    conn: sqlite3.Connection,
    *,
    intent_id: str,
    to_status: str,
    reject_code: str | None = None,
    reject_reason: str | None = None,
    latest_order_id: str | None = None,
    rule_check_snapshot_id: str | None = None,
    decision_snapshot_id: str | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    current = get_intent(conn, intent_id)
    if not current:
        raise IntentNotFoundError(f"intent not found: {intent_id}")

    from_status = str(current.get("status") or "")
    next_status = str(to_status or "").strip().lower()
    if next_status not in VALID_STATUSES:
        raise ValueError(f"invalid target status: {to_status}")

    if strict and from_status != next_status:
        allowed = ALLOWED_TRANSITIONS.get(from_status, set())
        if next_status not in allowed:
            raise InvalidTransitionError(f"invalid transition: {from_status} -> {next_status}")

    now_ms = _now_ms()
    conn.execute(
        """
        UPDATE order_intents
        SET status=?,
            reject_code=COALESCE(?, reject_code),
            reject_reason=COALESCE(?, reject_reason),
            latest_order_id=COALESCE(?, latest_order_id),
            rule_check_snapshot_id=COALESCE(?, rule_check_snapshot_id),
            decision_snapshot_id=COALESCE(?, decision_snapshot_id),
            updated_at_ms=?
        WHERE intent_id=?
        """,
        (
            next_status,
            reject_code,
            reject_reason,
            latest_order_id,
            rule_check_snapshot_id,
            decision_snapshot_id,
            now_ms,
            intent_id,
        ),
    )
    row = get_intent(conn, intent_id)
    if not row:
        raise IntentNotFoundError(f"intent not found after update: {intent_id}")
    return row
