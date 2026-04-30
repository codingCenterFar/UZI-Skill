from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from lib.market_router import parse_ticker

from papertrade.api_common import PaperTradeAPIError, new_id
from papertrade.config import PaperTradeConfig
from papertrade.human_gateway import submit_manual_order
from papertrade.intent_service import IdempotencyConflictError
from papertrade.opening_matcher import match_queued_day_orders
from papertrade.outbox import enqueue_outbox_event
from papertrade.rebuild import rebuild_read_models
from papertrade.replay import replay_loop
from papertrade.runtime_store import (
    DEFAULT_RUNTIME_LEASE_NAME,
    acquire_runtime_lease,
    finish_runtime_session,
    get_runtime_lease,
    release_runtime_lease,
    start_runtime_session,
)
from papertrade.system_config import record_config_change


def _body(payload: dict[str, Any] | None) -> dict[str, Any]:
    return dict(payload or {})


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise PaperTradeAPIError("INVALID_REQUEST", f"{key} is required", {"field": key})
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    try:
        value = int(payload.get(key))
    except Exception as e:
        raise PaperTradeAPIError("INVALID_REQUEST", f"{key} must be an integer", {"field": key}) from e
    if value <= 0:
        raise PaperTradeAPIError("INVALID_REQUEST", f"{key} must be > 0", {"field": key, "value": value})
    return value


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _latest_rule_snapshot(conn: sqlite3.Connection, rule_check_snapshot_id: str | None) -> dict[str, Any] | None:
    if not rule_check_snapshot_id:
        return None
    row = conn.execute(
        "SELECT * FROM rule_check_snapshots WHERE rule_check_snapshot_id=?",
        (str(rule_check_snapshot_id),),
    ).fetchone()
    return dict(row) if row else None


def _rule_reject_details(
    conn: sqlite3.Connection,
    *,
    intent: dict[str, Any],
    requested_qty: int,
) -> dict[str, Any]:
    snapshot_id = str(intent.get("rule_check_snapshot_id") or "")
    details: dict[str, Any] = {
        "intent_id": intent.get("intent_id"),
        "rule_check_snapshot_id": snapshot_id or None,
        "requested_qty": int(requested_qty),
    }
    snapshot = _latest_rule_snapshot(conn, snapshot_id)
    if not snapshot:
        return details

    details["market_phase"] = snapshot.get("market_phase")
    details["trade_date"] = snapshot.get("trade_date")
    try:
        checks = json.loads(snapshot.get("checks_json") or "[]")
    except Exception:
        checks = []
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict) or check.get("passed", True):
                continue
            check_details = check.get("details")
            if isinstance(check_details, dict):
                details.update(check_details)
            break
    return details


def _rule_check_payload(conn: sqlite3.Connection, rule_check_snapshot_id: str | None) -> dict[str, Any] | None:
    snapshot = _latest_rule_snapshot(conn, rule_check_snapshot_id)
    if not snapshot:
        return None
    return {
        "rule_check_snapshot_id": snapshot.get("rule_check_snapshot_id"),
        "allow": bool(int(snapshot.get("allow") or 0)),
        "reject_code": snapshot.get("reject_code"),
        "reject_reason": snapshot.get("reject_reason"),
    }


def _order_payload(conn: sqlite3.Connection, order_id: int | str | None) -> dict[str, Any] | None:
    if order_id in (None, ""):
        return None
    row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (int(order_id),)).fetchone()
    if not row:
        return None
    return {
        "order_id": int(row["id"]),
        "status": row["status"],
        "ticker": row["ticker"],
        "side": row["side"],
        "qty": int(float(row["order_qty"] or 0)),
        "filled_qty": int(float(row["filled_qty"] or 0)),
        "avg_fill_price": float(row["fill_price"]) if row["fill_price"] is not None else None,
    }


def _fills_payload(conn: sqlite3.Connection, order_id: int | str | None) -> list[dict[str, Any]]:
    if order_id in (None, ""):
        return []
    rows = conn.execute(
        "SELECT * FROM paper_fills WHERE order_id=? ORDER BY id ASC",
        (int(order_id),),
    ).fetchall()
    return [
        {
            "fill_id": int(row["id"]),
            "order_id": int(row["order_id"]),
            "qty": int(float(row["qty"] or 0)),
            "price": float(row["price"] or 0.0),
            "fee": float(row["fee"] or 0.0),
            "tax": float(row["tax"] or 0.0),
        }
        for row in rows
    ]


def submit_sim_order_command(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]:
    data = _body(payload)
    idem = str(idempotency_key or "").strip()
    if not idem:
        raise PaperTradeAPIError("INVALID_REQUEST", "Idempotency-Key header is required", {"header": "Idempotency-Key"})
    client_order_id = str(data.get("client_order_id") or "").strip()
    if client_order_id and client_order_id != idem:
        raise PaperTradeAPIError(
            "IDEMPOTENCY_CONFLICT",
            "client_order_id must match Idempotency-Key",
            {"client_order_id": client_order_id, "idempotency_key": idem},
            status_code=409,
        )

    ticker_raw = _required_str(data, "ticker")
    ticker_info = parse_ticker(ticker_raw)
    ticker = ticker_info.full
    market = str(data.get("market") or ticker_info.market or "A")
    side = _required_str(data, "side").upper()
    if side not in {"BUY", "SELL"}:
        raise PaperTradeAPIError("INVALID_REQUEST", "side must be BUY or SELL", {"side": side})
    qty = _required_int(data, "qty")
    order_type = str(data.get("order_type") or "MARKET").upper()
    time_in_force = str(data.get("time_in_force") or "IOC").upper()
    if time_in_force not in {"IOC", "DAY"}:
        raise PaperTradeAPIError("INVALID_REQUEST", "time_in_force must be IOC or DAY", {"time_in_force": time_in_force})
    price = _float_or_none(data.get("limit_price"))
    if price is None:
        price = _float_or_none(data.get("price"))
    if price is None or price <= 0:
        raise PaperTradeAPIError(
            "INVALID_REQUEST",
            "sim order requires limit_price or price for deterministic simulation",
            {"order_type": order_type},
        )

    operator = data.get("operator") if isinstance(data.get("operator"), dict) else {}
    trade_date = str(data.get("trade_date") or date.today().isoformat())

    try:
        result = submit_manual_order(
            conn,
            cfg=cfg,
            as_of_date=trade_date,
            ticker=ticker,
            market=market,
            side=side,
            qty=qty,
            price=float(price),
            idempotency_key=idem,
            note=str(data.get("note") or "web manual order"),
            operator_id=str(operator.get("operator_id") or "local_user"),
            operator_channel=str(operator.get("channel") or "web"),
            sim_only=bool(data.get("sim_only", False)),
            request_ts_ms=data.get("request_ts_ms"),
            instrument_name=data.get("instrument_name"),
            order_type=order_type,
            time_in_force=time_in_force,
            queue_when_market_closed=bool(data.get("queue_when_market_closed", time_in_force == "DAY")),
        )
    except IdempotencyConflictError as e:
        raise PaperTradeAPIError(
            "IDEMPOTENCY_CONFLICT",
            str(e),
            {"idempotency_key": idem, "ticker": ticker, "side": side, "qty": qty},
            status_code=409,
        ) from e
    except ValueError as e:
        raise PaperTradeAPIError("INVALID_REQUEST", str(e), {"ticker": ticker_raw}) from e

    intent = result.get("intent") or {}
    intent_payload = {
        "intent_id": intent.get("intent_id"),
        "status": intent.get("status"),
        "source": intent.get("source"),
        "idempotency_key": intent.get("idempotency_key"),
        "decision_snapshot_id": intent.get("decision_snapshot_id"),
        "rule_check_snapshot_id": intent.get("rule_check_snapshot_id"),
    }

    if not result.get("accepted", False):
        code = str((result.get("order_info") or {}).get("reject_code") or intent.get("reject_code") or "INVALID_REQUEST")
        message = str((result.get("order_info") or {}).get("reject_reason") or intent.get("reject_reason") or code)
        raise PaperTradeAPIError(
            code,
            message,
            _rule_reject_details(conn, intent=intent, requested_qty=qty),
            retryable=code == "RULE_MARKET_CLOSED",
            status_code=409 if code.startswith("RULE_") else 400,
            correlation_id=intent.get("correlation_id"),
        )

    order_id = (result.get("order_info") or {}).get("order_id") or intent.get("latest_order_id")
    return {
        "intent": intent_payload,
        "order": _order_payload(conn, order_id),
        "fills": _fills_payload(conn, order_id),
        "rule_check": _rule_check_payload(conn, intent.get("rule_check_snapshot_id")),
        "queued": bool(result.get("queued")),
    }


def start_runtime_command(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    data = _body(payload)
    start_request_id = str(idempotency_key or data.get("start_request_id") or "").strip() or None
    if start_request_id:
        existing = conn.execute(
            "SELECT * FROM runtime_sessions WHERE start_request_id=? ORDER BY created_at_ms DESC LIMIT 1",
            (start_request_id,),
        ).fetchone()
        if existing:
            lease = get_runtime_lease(conn, lease_name=DEFAULT_RUNTIME_LEASE_NAME)
            return {
                "session_id": existing["session_id"],
                "status": existing["status"],
                "lease": lease,
                "idempotent_replay": True,
            }

    config_json = cfg.as_dict()
    config_json["api_start_request"] = {
        "poll_seconds": data.get("poll_seconds", cfg.realtime.poll_seconds),
        "refresh_every_loops": data.get("refresh_every_loops", cfg.realtime.refresh_every_loops),
        "watchlist_id": data.get("watchlist_id", "wl_default"),
        "quote_overlay": bool(data.get("quote_overlay", cfg.realtime.quote_overlay)),
        "watcher_overlay": bool(data.get("watcher_overlay", cfg.realtime.watcher_overlay)),
        "dry_run": bool(data.get("dry_run", False)),
    }
    session_id = start_runtime_session(
        conn,
        start_request_id=start_request_id,
        config_json=config_json,
    )
    ttl_ms = max(1_000, int(data.get("lease_ttl_seconds", cfg.realtime.lease_ttl_seconds) or cfg.realtime.lease_ttl_seconds) * 1000)
    lease_result = acquire_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=session_id,
        ttl_ms=ttl_ms,
    )
    if not lease_result.acquired:
        finish_runtime_session(
            conn,
            session_id=session_id,
            status="failed",
            stop_reason="runtime_lock_conflict",
            last_loop_id=None,
        )
        raise PaperTradeAPIError(
            "RUNTIME_ALREADY_RUNNING",
            "papertrade runtime is already running",
            lease_result.as_dict(),
            retryable=True,
            status_code=409,
        )

    enqueue_outbox_event(
        conn,
        event_type="runtime.started",
        entity_type="runtime_session",
        entity_id=session_id,
        source="api_runtime",
        severity="info",
        correlation_id=new_id("crr"),
        payload={
            "session_id": session_id,
            "status": "running",
            "config_hash": conn.execute(
                "SELECT config_hash FROM runtime_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()["config_hash"],
        },
    )
    return {
        "session_id": session_id,
        "status": "running",
        "lease": lease_result.as_dict(),
    }


def stop_runtime_command(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _body(payload)
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        row = conn.execute(
            """
            SELECT *
            FROM runtime_sessions
            WHERE status IN ('starting', 'running', 'stopping')
            ORDER BY updated_at_ms DESC
            LIMIT 1
            """
        ).fetchone()
        session_id = str(row["session_id"]) if row else ""

    if not session_id:
        raise PaperTradeAPIError(
            "RUNTIME_NOT_RUNNING",
            "papertrade runtime is not running",
            retryable=True,
            status_code=409,
        )

    session = conn.execute("SELECT * FROM runtime_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not session or str(session["status"]) not in {"starting", "running", "stopping"}:
        raise PaperTradeAPIError(
            "RUNTIME_NOT_RUNNING",
            "papertrade runtime is not running",
            {"session_id": session_id},
            retryable=True,
            status_code=409,
        )

    lease = get_runtime_lease(conn, lease_name=DEFAULT_RUNTIME_LEASE_NAME)
    if not lease or str(lease.get("owner_session_id") or "") != session_id:
        raise PaperTradeAPIError(
            "RUNTIME_LOCK_NOT_OWNED",
            "runtime lease is not owned by this session",
            {"session_id": session_id, "lease": lease},
            retryable=True,
            status_code=409,
        )

    reason = str(data.get("reason") or "operator_stop")
    finish_runtime_session(
        conn,
        session_id=session_id,
        status="stopped",
        stop_reason=reason,
        last_loop_id=session["last_loop_id"],
    )
    release_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=session_id,
    )
    enqueue_outbox_event(
        conn,
        event_type="runtime.stopped",
        entity_type="runtime_session",
        entity_id=session_id,
        source="api_runtime",
        severity="info",
        correlation_id=new_id("crr"),
        payload={"session_id": session_id, "reason": reason, "status": "stopped"},
    )
    return {"session_id": session_id, "status": "stopped"}


def match_opening_command(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _body(payload)
    return match_queued_day_orders(
        conn,
        request_ts_ms=data.get("request_ts_ms"),
        trade_date=data.get("trade_date"),
        limit=int(data.get("limit", 200)),
        run_id=data.get("run_id"),
    )


def replay_loop_command(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _body(payload)
    return replay_loop(conn, loop_id=_required_str(data, "loop_id"))


def rebuild_readmodels_command(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _body(payload)
    initial_cash_raw = data.get("initial_cash")
    initial_cash = cfg.trade.initial_cash if initial_cash_raw in (None, "") else float(initial_cash_raw)
    return rebuild_read_models(
        conn,
        trade_date=data.get("trade_date"),
        tickers=data.get("tickers"),
        initial_cash=initial_cash,
        clear_missing=bool(data.get("clear_missing", True)),
    )


def record_runtime_config_command(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = _body(payload)
    config_json = data.get("config")
    if config_json is None:
        config_json = cfg.as_dict()
    return record_config_change(
        conn,
        scope=str(data.get("scope") or "runtime"),
        config_json=config_json,
        changed_by=data.get("changed_by"),
        change_reason=data.get("change_reason"),
        effective_from_ms=data.get("effective_from_ms"),
        correlation_id=data.get("correlation_id"),
    )
