from __future__ import annotations

import sqlite3
from typing import Any

from lib.market_router import parse_ticker

from papertrade.command_service import submit_intent_order
from papertrade.market_calendar_service import MarketCalendarService
from papertrade.outbox import enqueue_outbox_event


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def list_queued_day_orders(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses = [
        "status='queued'",
        "time_in_force='DAY'",
        "(latest_order_id IS NULL OR latest_order_id='')",
    ]
    params: list[Any] = []
    if ticker:
        clauses.append("ticker=?")
        params.append(str(ticker).upper())
    rows = conn.execute(
        f"""
        SELECT *
        FROM order_intents
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at_ms ASC
        LIMIT ?
        """,
        tuple(params + [max(1, int(limit))]),
    ).fetchall()
    return [dict(row) for row in rows]


def match_queued_day_orders(
    conn: sqlite3.Connection,
    *,
    request_ts_ms: int | None = None,
    trade_date: str | None = None,
    limit: int = 200,
    run_id: str | None = None,
    calendar_service: MarketCalendarService | None = None,
) -> dict[str, Any]:
    calendar = calendar_service or MarketCalendarService()
    clock = calendar.get_clock(ts_ms=request_ts_ms, trade_date=trade_date)
    if not clock.is_open_session:
        return {
            "matched": 0,
            "filled": 0,
            "rejected": 0,
            "skipped": 0,
            "market_phase": clock.phase,
            "trade_date": clock.trade_date,
            "items": [],
        }

    queued = list_queued_day_orders(conn, limit=limit)
    if not queued:
        return {
            "matched": 0,
            "filled": 0,
            "rejected": 0,
            "skipped": 0,
            "market_phase": clock.phase,
            "trade_date": clock.trade_date,
            "items": [],
        }

    items: list[dict[str, Any]] = []
    filled = 0
    rejected = 0
    skipped = 0
    match_run_id = run_id or f"opening:{clock.trade_date}"

    enqueue_outbox_event(
        conn,
        event_type="opening_match.started",
        entity_type="opening_match",
        entity_id=match_run_id,
        source="opening_matcher",
        severity="info",
        payload={
            "run_id": match_run_id,
            "market_phase": clock.phase,
            "trade_date": clock.trade_date,
            "queued_count": len(queued),
        },
    )

    for intent in queued:
        intent_id = str(intent.get("intent_id") or "")
        ticker = str(intent.get("ticker") or "").upper()
        if not intent_id or not ticker:
            skipped += 1
            continue
        ti = parse_ticker(ticker)
        try:
            result = submit_intent_order(
                conn,
                run_id=match_run_id,
                as_of_date=clock.trade_date,
                ticker=ti.full,
                market=ti.market,
                source=str(intent.get("source") or "manual"),
                idempotency_key=str(intent.get("idempotency_key") or ""),
                side=str(intent.get("side") or ""),
                qty=_i(intent.get("qty")),
                price=_f(intent.get("limit_price")),
                action="QUEUED_DAY_ORDER",
                order_reason="opening match for queued DAY order",
                order_type=str(intent.get("order_type") or "LIMIT"),
                time_in_force="DAY",
                operator_id=intent.get("operator_id"),
                operator_channel=intent.get("operator_channel"),
                decision_snapshot_id=intent.get("decision_snapshot_id"),
                request_ts_ms=request_ts_ms,
                decision_basis="queued_day_opening_match",
                queue_when_market_closed=False,
                force_rule_recheck=True,
            )
        except Exception as exc:
            skipped += 1
            items.append(
                {
                    "intent_id": intent_id,
                    "ticker": ticker,
                    "status": "error",
                    "error": str(exc),
                }
            )
            continue

        status = str((result.get("intent") or {}).get("status") or "")
        if result.get("accepted") and status == "filled":
            filled += 1
        elif status == "rejected" or not result.get("accepted"):
            rejected += 1
        else:
            skipped += 1
        items.append(
            {
                "intent_id": intent_id,
                "ticker": ticker,
                "status": status,
                "accepted": bool(result.get("accepted")),
                "order": result.get("order_info"),
            }
        )

    enqueue_outbox_event(
        conn,
        event_type="opening_match.finished",
        entity_type="opening_match",
        entity_id=match_run_id,
        source="opening_matcher",
        severity="info",
        payload={
            "run_id": match_run_id,
            "market_phase": clock.phase,
            "trade_date": clock.trade_date,
            "matched": len(items),
            "filled": filled,
            "rejected": rejected,
            "skipped": skipped,
        },
    )

    return {
        "matched": len(items),
        "filled": filled,
        "rejected": rejected,
        "skipped": skipped,
        "market_phase": clock.phase,
        "trade_date": clock.trade_date,
        "items": items,
    }
