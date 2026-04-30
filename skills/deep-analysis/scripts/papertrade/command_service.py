from __future__ import annotations

import sqlite3
import time
from typing import Any

from papertrade.intent_service import create_intent, transition_intent_status
from papertrade.ledger import create_order, fill_order
from papertrade.market_calendar_service import MarketCalendarService
from papertrade.outbox import enqueue_outbox_event
from papertrade.rule_engine import RuleValidationContext, validate_and_record_rule_check

_TERMINAL_INTENT_STATUSES = {"rejected", "filled", "cancelled", "expired"}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _fetch_legacy_order(conn: sqlite3.Connection, order_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM paper_orders WHERE id=?", (int(order_id),)).fetchone()
    return dict(row) if row else None


def _fetch_intent(conn: sqlite3.Connection, intent_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM order_intents WHERE intent_id=?", (str(intent_id),)).fetchone()
    return dict(row) if row else {}


def _to_rejected_order_info(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent_id": intent.get("intent_id"),
        "intent_status": intent.get("status"),
        "rejected": True,
        "reject_code": intent.get("reject_code"),
        "reject_reason": intent.get("reject_reason"),
    }


def _to_filled_order_info(
    *,
    intent: dict[str, Any],
    order_row: dict[str, Any],
    order_reason: str,
    reused: bool,
) -> dict[str, Any]:
    return {
        "intent_id": intent.get("intent_id"),
        "intent_status": intent.get("status"),
        "intent_reused": bool(reused),
        "order_id": int(order_row["id"]),
        "side": order_row.get("side"),
        "qty": int(_f(order_row.get("filled_qty"), _f(order_row.get("order_qty"), 0.0))),
        "price": round(_f(order_row.get("fill_price"), _f(order_row.get("order_price"), 0.0)), 4),
        "fee": round(_f(order_row.get("fee"), 0.0), 4),
        "tax": round(_f(order_row.get("tax"), 0.0), 4),
        "reason": order_reason,
    }


def _to_sim_only_order_info(
    *,
    intent: dict[str, Any],
    side: str,
    qty: int,
    price: float,
    order_reason: str,
    reused: bool,
) -> dict[str, Any]:
    return {
        "intent_id": intent.get("intent_id"),
        "intent_status": intent.get("status"),
        "intent_reused": bool(reused),
        "sim_only": True,
        "side": side,
        "qty": int(qty),
        "price": round(float(price), 4),
        "reason": order_reason,
    }


def _to_queued_order_info(
    *,
    intent: dict[str, Any],
    side: str,
    qty: int,
    price: float,
    order_reason: str,
    reused: bool,
    rule_check_snapshot_id: str | None = None,
) -> dict[str, Any]:
    return {
        "intent_id": intent.get("intent_id"),
        "intent_status": intent.get("status"),
        "intent_reused": bool(reused),
        "queued": True,
        "side": side,
        "qty": int(qty),
        "price": round(float(price), 4),
        "reason": order_reason,
        "rule_check_snapshot_id": rule_check_snapshot_id,
    }


def submit_intent_order(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of_date: str,
    ticker: str,
    market: str,
    source: str,
    idempotency_key: str,
    side: str,
    qty: int,
    price: float,
    action: str,
    order_reason: str,
    order_type: str = "LIMIT",
    time_in_force: str = "IOC",
    signal_id: int | None = None,
    loop_id: str | None = None,
    operator_id: str | None = None,
    operator_channel: str | None = None,
    decision_snapshot_id: str | None = None,
    rule_check_snapshot_id: str | None = None,
    sim_only: bool = False,
    queue_when_market_closed: bool = False,
    force_rule_recheck: bool = False,
    request_ts_ms: int | None = None,
    instrument_name: str | None = None,
    decision_basis: str | None = None,
    analysis_age_ms: int = 0,
    quote_age_ms: int = 0,
    fee: float | None = None,
    tax: float | None = None,
    lot_size: int = 100,
    commission_bps: float = 2.0,
    stamp_duty_bps: float = 10.0,
    slippage_bps: float = 0.0,
    policy_snapshot: dict[str, Any] | None = None,
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    side_u = str(side or "").upper()
    qty_i = int(qty)
    px = float(price)
    order_type_u = str(order_type or "LIMIT").upper()
    tif_u = str(time_in_force or "IOC").upper()

    value = px * qty_i
    fee_v = float(fee) if fee is not None else value * float(commission_bps) / 10000.0
    tax_v = float(tax) if tax is not None else (value * float(stamp_duty_bps) / 10000.0 if side_u == "SELL" else 0.0)

    created = create_intent(
        conn,
        source=source,
        idempotency_key=idempotency_key,
        ticker=ticker,
        side=side_u,
        qty=qty_i,
        order_type=order_type_u,
        limit_price=px,
        time_in_force=tif_u,
        signal_id=signal_id,
        loop_id=loop_id,
        operator_id=operator_id,
        operator_channel=operator_channel,
        decision_snapshot_id=decision_snapshot_id,
        rule_check_snapshot_id=rule_check_snapshot_id,
    )
    intent = created.row
    intent_id = str(intent["intent_id"])
    correlation_id = str(intent.get("correlation_id") or "")

    if not created.reused:
        enqueue_outbox_event(
            conn,
            event_type="intent.created",
            entity_type="intent",
            entity_id=intent_id,
            source=f"{source}_command",
            severity="info",
            correlation_id=correlation_id,
            payload={
                "intent_id": intent_id,
                "source": source,
                "ticker": str(ticker).upper(),
                "side": side_u,
                "qty": qty_i,
                "sim_only": bool(sim_only),
            },
        )

    # Idempotent replay: terminal statuses and existing fills are returned as-is.
    current_status = str(intent.get("status") or "")
    if current_status in _TERMINAL_INTENT_STATUSES:
        latest_order_id = intent.get("latest_order_id")
        if latest_order_id:
            row = _fetch_legacy_order(conn, int(latest_order_id))
            if row:
                return {
                    "intent": intent,
                    "accepted": current_status == "filled",
                    "reused": bool(created.reused),
                    "order_info": _to_filled_order_info(
                        intent=intent,
                        order_row=row,
                        order_reason=order_reason,
                        reused=bool(created.reused),
                    ),
                }
        return {
            "intent": intent,
            "accepted": False,
            "reused": bool(created.reused),
            "order_info": _to_rejected_order_info(intent),
        }
    if (
        bool(created.reused)
        and current_status == "queued"
        and not intent.get("latest_order_id")
        and not force_rule_recheck
    ):
        return {
            "intent": intent,
            "accepted": True,
            "queued": True,
            "reused": True,
            "order_info": _to_queued_order_info(
                intent=intent,
                side=side_u,
                qty=qty_i,
                price=px,
                order_reason=order_reason,
                reused=True,
                rule_check_snapshot_id=str(intent.get("rule_check_snapshot_id") or ""),
            ),
        }

    # Centralized rule-engine path for both auto/manual commands.
    current_rule_snapshot_id = "" if force_rule_recheck else str(intent.get("rule_check_snapshot_id") or "")
    rule_result = None
    if not current_rule_snapshot_id:
        rule_result = validate_and_record_rule_check(
            conn,
            ctx=RuleValidationContext(
                intent_id=intent_id,
                ticker=ticker,
                market=market,
                side=side_u,
                qty=qty_i,
                order_type=order_type_u,
                limit_price=px,
                trade_date=as_of_date,
                request_ts_ms=request_ts_ms,
                instrument_name=instrument_name,
                decision_basis=decision_basis,
                analysis_age_ms=analysis_age_ms,
                quote_age_ms=quote_age_ms,
                quote_price=px,
            ),
            commission_bps=commission_bps,
            stamp_duty_bps=stamp_duty_bps,
        )
        current_rule_snapshot_id = rule_result.rule_check_snapshot_id

    if rule_result and (not rule_result.allow):
        if (
            bool(queue_when_market_closed)
            and tif_u == "DAY"
            and rule_result.reject_code == "RULE_MARKET_CLOSED"
        ):
            calendar = MarketCalendarService()
            expiry_clock = calendar.get_clock(ts_ms=request_ts_ms, trade_date=rule_result.trade_date)
            expires_at_ms = calendar.day_order_expiry_ts_ms(expiry_clock)
            intent = transition_intent_status(
                conn,
                intent_id=intent_id,
                to_status="queued",
                rule_check_snapshot_id=current_rule_snapshot_id,
                strict=False,
            )
            conn.execute(
                "UPDATE order_intents SET expires_at_ms=?, updated_at_ms=? WHERE intent_id=?",
                (int(expires_at_ms), int(time.time() * 1000), intent_id),
            )
            intent = _fetch_intent(conn, intent_id)
            enqueue_outbox_event(
                conn,
                event_type="intent.queued",
                entity_type="intent",
                entity_id=intent_id,
                source=f"{source}_command",
                severity="info",
                correlation_id=correlation_id,
                causation_id=rule_result.rule_check_snapshot_id,
                payload={
                    "intent_id": intent_id,
                    "ticker": str(ticker).upper(),
                    "side": side_u,
                    "qty": qty_i,
                    "reason": "market_closed_day_order",
                    "rule_check_snapshot_id": rule_result.rule_check_snapshot_id,
                    "market_phase": rule_result.market_phase,
                    "trade_date": rule_result.trade_date,
                    "expires_at_ms": int(expires_at_ms),
                },
            )
            return {
                "intent": intent,
                "accepted": True,
                "queued": True,
                "reused": bool(created.reused),
                "order_info": _to_queued_order_info(
                    intent=intent,
                    side=side_u,
                    qty=qty_i,
                    price=px,
                    order_reason=order_reason,
                    reused=bool(created.reused),
                    rule_check_snapshot_id=rule_result.rule_check_snapshot_id,
                ),
            }
        intent = transition_intent_status(
            conn,
            intent_id=intent_id,
            to_status="rejected",
            reject_code=rule_result.reject_code,
            reject_reason=rule_result.reject_reason,
            rule_check_snapshot_id=current_rule_snapshot_id,
            strict=False,
        )
        enqueue_outbox_event(
            conn,
            event_type="intent.rejected",
            entity_type="intent",
            entity_id=intent_id,
            source=f"{source}_command",
            severity="warning",
            correlation_id=correlation_id,
            causation_id=rule_result.rule_check_snapshot_id,
            payload={
                "intent_id": intent_id,
                "ticker": str(ticker).upper(),
                "side": side_u,
                "qty": qty_i,
                "reject_code": rule_result.reject_code,
                "reject_reason": rule_result.reject_reason,
                "rule_check_snapshot_id": rule_result.rule_check_snapshot_id,
                "attempt_no": rule_result.attempt_no,
            },
        )
        return {
            "intent": intent,
            "accepted": False,
            "reused": bool(created.reused),
            "order_info": _to_rejected_order_info(intent),
        }
    if current_status != "intent" and current_rule_snapshot_id and (
        force_rule_recheck or not intent.get("rule_check_snapshot_id")
    ):
        intent = transition_intent_status(
            conn,
            intent_id=intent_id,
            to_status=current_status,
            rule_check_snapshot_id=current_rule_snapshot_id,
            strict=False,
        )

    if current_status == "intent":
        intent = transition_intent_status(
            conn,
            intent_id=intent_id,
            to_status="validated",
            rule_check_snapshot_id=current_rule_snapshot_id or rule_check_snapshot_id,
            strict=False,
        )
        enqueue_outbox_event(
            conn,
            event_type="intent.validated",
            entity_type="intent",
            entity_id=intent_id,
            source=f"{source}_command",
            severity="info",
            correlation_id=correlation_id,
            causation_id=current_rule_snapshot_id or None,
            payload={
                "intent_id": intent_id,
                "ticker": str(ticker).upper(),
                "side": side_u,
                "qty": qty_i,
                "rule_check_snapshot_id": current_rule_snapshot_id,
            },
        )

    if sim_only:
        return {
            "intent": intent,
            "accepted": True,
            "reused": bool(created.reused),
            "order_info": _to_sim_only_order_info(
                intent=intent,
                side=side_u,
                qty=qty_i,
                price=px,
                order_reason=order_reason,
                reused=bool(created.reused),
            ),
        }

    if str(intent.get("status") or "") == "validated":
        intent = transition_intent_status(
            conn,
            intent_id=intent_id,
            to_status="queued",
            strict=False,
        )

    order_row: dict[str, Any] | None = None
    latest_order_id = intent.get("latest_order_id")
    if latest_order_id:
        order_row = _fetch_legacy_order(conn, int(latest_order_id))

    if not order_row:
        order_id = create_order(
            conn,
            run_id=run_id,
            as_of_date=as_of_date,
            ticker=ticker,
            side=side_u,
            action=action,
            order_price=px,
            order_qty=qty_i,
            slippage_bps=float(slippage_bps),
            reason=order_reason,
            policy_snapshot=policy_snapshot or {},
            source=source_snapshot or {},
        )
        order_row = _fetch_legacy_order(conn, order_id)
        intent = transition_intent_status(
            conn,
            intent_id=intent_id,
            to_status="queued",
            latest_order_id=str(order_id),
            strict=False,
        )
        enqueue_outbox_event(
            conn,
            event_type="order.created",
            entity_type="order",
            entity_id=str(order_id),
            source=f"{source}_command",
            severity="info",
            correlation_id=correlation_id,
            causation_id=intent_id,
            payload={
                "order_id": int(order_id),
                "intent_id": intent_id,
                "ticker": str(ticker).upper(),
                "side": side_u,
                "qty": qty_i,
                "price": round(px, 4),
                "reason": order_reason,
            },
        )

    if not order_row:
        raise RuntimeError(f"failed to create/fetch legacy order for intent={intent_id}")

    order_id_i = int(order_row["id"])
    if str(order_row.get("status") or "").lower() != "filled":
        fill_order(
            conn,
            order_id=order_id_i,
            intent_id=intent_id,
            run_id=run_id,
            as_of_date=as_of_date,
            ticker=ticker,
            market=market,
            side=side_u,
            fill_price=px,
            fill_qty=qty_i,
            fee=fee_v,
            tax=tax_v,
        )
        order_row = _fetch_legacy_order(conn, order_id_i)
        enqueue_outbox_event(
            conn,
            event_type="order.filled",
            entity_type="order",
            entity_id=str(order_id_i),
            source=f"{source}_command",
            severity="info",
            correlation_id=correlation_id,
            causation_id=intent_id,
            payload={
                "order_id": order_id_i,
                "intent_id": intent_id,
                "ticker": str(ticker).upper(),
                "side": side_u,
                "qty": qty_i,
                "price": round(px, 4),
                "fee": round(fee_v, 4),
                "tax": round(tax_v, 4),
            },
        )

    intent = transition_intent_status(
        conn,
        intent_id=intent_id,
        to_status="filled",
        latest_order_id=str(order_id_i),
        strict=False,
    )
    enqueue_outbox_event(
        conn,
        event_type="intent.filled",
        entity_type="intent",
        entity_id=intent_id,
        source=f"{source}_command",
        severity="info",
        correlation_id=correlation_id,
        causation_id=str(order_id_i),
        payload={
            "intent_id": intent_id,
            "order_id": order_id_i,
            "ticker": str(ticker).upper(),
            "side": side_u,
            "qty": qty_i,
            "status": "filled",
        },
    )

    return {
        "intent": intent,
        "accepted": True,
        "reused": bool(created.reused),
        "order_info": _to_filled_order_info(
            intent=intent,
            order_row=order_row or {},
            order_reason=order_reason,
            reused=bool(created.reused),
        ),
    }
