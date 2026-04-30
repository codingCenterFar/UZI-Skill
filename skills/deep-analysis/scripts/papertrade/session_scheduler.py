from __future__ import annotations

import sqlite3
import time
from datetime import datetime, time as dt_time
from typing import Any

from papertrade.api_common import PaperTradeAPIError
from papertrade.candidate_generator import generate_candidate_pool
from papertrade.candidate_pool import DEFAULT_POOL_NAME
from papertrade.intent_service import transition_intent_status
from papertrade.market_calendar_service import MarketCalendarService, MarketClockState
from papertrade.outbox import enqueue_outbox_event


def _now_ms() -> int:
    return int(time.time() * 1000)


def _loads_count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row["n"] if row and "n" in row.keys() else 0)


def scheduler_phase(clock: MarketClockState, *, ts_ms: int | None = None) -> str:
    if clock.phase == "lunch_break":
        return "lunch"
    if clock.phase == "open_afternoon":
        if ts_ms is None:
            now = datetime.now()
        else:
            now = datetime.fromtimestamp(int(ts_ms) / 1000)
        if now.time() >= dt_time(14, 57):
            return "closing"
    return str(clock.phase)


def build_session_plan(
    *,
    request_ts_ms: int | None = None,
    trade_date: str | None = None,
    refresh_mode: dict[str, Any] | None = None,
    calendar_service: MarketCalendarService | None = None,
) -> dict[str, Any]:
    calendar = calendar_service or MarketCalendarService()
    clock = calendar.get_clock(ts_ms=request_ts_ms, trade_date=trade_date)
    phase = scheduler_phase(clock, ts_ms=request_ts_ms)
    refresh = dict(refresh_mode or {})
    requested_refresh_mode = str(refresh.get("mode") or "cached_cycle")

    actions_by_phase: dict[str, list[str]] = {
        "pre_open": ["resolve_universe", "refresh_quotes", "wait_open"],
        "open_morning": ["match_opening_orders", "run_decision_cycle", "dispatch_events"],
        "lunch": ["refresh_quotes", "risk_snapshot", "dispatch_events"],
        "open_afternoon": ["match_opening_orders", "run_decision_cycle", "dispatch_events"],
        "closing": ["match_opening_orders", "refresh_quotes", "tail_risk_snapshot", "dispatch_events"],
        "after_close": ["expire_day_orders", "after_close_review", "dispatch_events"],
        "holiday": ["expire_day_orders", "after_close_review", "dispatch_events"],
    }
    if requested_refresh_mode == "quote_only":
        loop_mode = "quote_only"
    else:
        loop_mode = requested_refresh_mode

    return {
        "trade_date": clock.trade_date,
        "market_phase": clock.phase,
        "scheduler_phase": phase,
        "is_trade_day": bool(clock.is_trade_day),
        "is_open_session": bool(clock.is_open_session),
        "loop_mode": loop_mode,
        "actions": actions_by_phase.get(phase, ["dispatch_events"]),
        "match_opening": phase in {"open_morning", "open_afternoon", "closing"},
        "expire_day_orders": phase in {"after_close", "holiday"},
        "after_close_review": phase in {"after_close", "holiday"},
        "cycle_enabled": bool(loop_mode != "quote_only"),
        "quote_only_enabled": bool(loop_mode == "quote_only"),
        "refresh_mode": refresh,
    }


def expire_queued_day_orders(
    conn: sqlite3.Connection,
    *,
    request_ts_ms: int | None = None,
    trade_date: str | None = None,
    session_id: str | None = None,
    loop_no: int | None = None,
    calendar_service: MarketCalendarService | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    calendar = calendar_service or MarketCalendarService()
    clock = calendar.get_clock(ts_ms=request_ts_ms, trade_date=trade_date)
    phase = scheduler_phase(clock, ts_ms=request_ts_ms)
    now = int(request_ts_ms or _now_ms())
    if phase not in {"after_close", "holiday"}:
        return {
            "expired": 0,
            "scanned": 0,
            "market_phase": clock.phase,
            "scheduler_phase": phase,
            "trade_date": clock.trade_date,
            "items": [],
        }

    rows = conn.execute(
        """
        SELECT *
        FROM order_intents
        WHERE status='queued'
          AND time_in_force='DAY'
          AND (latest_order_id IS NULL OR latest_order_id='')
          AND expires_at_ms IS NOT NULL
          AND expires_at_ms <= ?
        ORDER BY created_at_ms ASC
        LIMIT ?
        """,
        (now, max(1, int(limit))),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        intent_id = str(row["intent_id"])
        updated = transition_intent_status(
            conn,
            intent_id=intent_id,
            to_status="expired",
            reject_code="DAY_ORDER_EXPIRED",
            reject_reason="DAY order expired at session close",
            strict=False,
        )
        item = {
            "intent_id": intent_id,
            "ticker": str(row["ticker"] or "").upper(),
            "side": row["side"],
            "expires_at_ms": row["expires_at_ms"],
            "status": updated.get("status"),
        }
        items.append(item)
        enqueue_outbox_event(
            conn,
            event_type="intent.expired",
            entity_type="intent",
            entity_id=intent_id,
            source="session_scheduler",
            severity="info",
            correlation_id=str(row["correlation_id"] or ""),
            payload={
                **item,
                "session_id": session_id,
                "loop_no": loop_no,
                "market_phase": clock.phase,
                "scheduler_phase": phase,
                "trade_date": clock.trade_date,
            },
        )

    return {
        "expired": len(items),
        "scanned": len(rows),
        "market_phase": clock.phase,
        "scheduler_phase": phase,
        "trade_date": clock.trade_date,
        "items": items,
    }


def record_after_close_review(
    conn: sqlite3.Connection,
    *,
    request_ts_ms: int | None = None,
    trade_date: str | None = None,
    session_id: str | None = None,
    loop_no: int | None = None,
    ticker_universe: dict[str, Any] | None = None,
    calendar_service: MarketCalendarService | None = None,
) -> dict[str, Any]:
    calendar = calendar_service or MarketCalendarService()
    clock = calendar.get_clock(ts_ms=request_ts_ms, trade_date=trade_date)
    phase = scheduler_phase(clock, ts_ms=request_ts_ms)
    if phase not in {"after_close", "holiday"}:
        return {
            "recorded": False,
            "market_phase": clock.phase,
            "scheduler_phase": phase,
            "trade_date": clock.trade_date,
        }

    queued_day_count = _loads_count(
        conn,
        """
        SELECT COUNT(*) AS n
        FROM order_intents
        WHERE status='queued' AND time_in_force='DAY'
        """,
    )
    expired_day_count = _loads_count(
        conn,
        """
        SELECT COUNT(*) AS n
        FROM order_intents
        WHERE status='expired' AND time_in_force='DAY'
        """,
    )
    latest_nav = conn.execute(
        """
        SELECT *
        FROM portfolio_nav_snapshots
        ORDER BY as_of_ts_ms DESC
        LIMIT 1
        """
    ).fetchone()
    payload = {
        "session_id": session_id,
        "loop_no": loop_no,
        "market_phase": clock.phase,
        "scheduler_phase": phase,
        "trade_date": clock.trade_date,
        "ticker_count": int((ticker_universe or {}).get("ticker_count") or 0),
        "queued_day_count": queued_day_count,
        "expired_day_count": expired_day_count,
        "latest_nav": dict(latest_nav) if latest_nav else None,
    }
    event_id = f"{session_id or 'session'}:{loop_no or 0}:{clock.trade_date}:{phase}:review"
    enqueue_outbox_event(
        conn,
        event_type="session.after_close_review",
        entity_type="runtime_session",
        entity_id=event_id,
        source="session_scheduler",
        severity="info",
        payload=payload,
    )
    return {"recorded": True, **payload}


def rebuild_candidate_pool_after_close(
    conn: sqlite3.Connection,
    *,
    cfg: Any,
    request_ts_ms: int | None = None,
    trade_date: str | None = None,
    session_id: str | None = None,
    loop_no: int | None = None,
    pool_name: str | None = None,
    watchlist_id: str | None = None,
    cache_roots: list[str] | None = None,
    max_candidates: int = 50,
    calendar_service: MarketCalendarService | None = None,
) -> dict[str, Any]:
    calendar = calendar_service or MarketCalendarService()
    clock = calendar.get_clock(ts_ms=request_ts_ms, trade_date=trade_date)
    phase = scheduler_phase(clock, ts_ms=request_ts_ms)
    if phase not in {"after_close", "holiday"}:
        return {
            "rebuilt": False,
            "skipped_reason": f"scheduler_phase={phase}",
            "market_phase": clock.phase,
            "scheduler_phase": phase,
            "trade_date": clock.trade_date,
        }

    target_pool = str(pool_name or DEFAULT_POOL_NAME)
    try:
        generated = generate_candidate_pool(
            conn,
            cfg=cfg,
            pool_name=target_pool,
            watchlist_id=watchlist_id or "wl_default",
            cache_roots=cache_roots,
            max_candidates=max(1, int(max_candidates)),
            as_of_date=clock.trade_date,
            archive_previous=True,
        )
    except PaperTradeAPIError as e:
        payload = {
            "session_id": session_id,
            "loop_no": loop_no,
            "pool_name": target_pool,
            "market_phase": clock.phase,
            "scheduler_phase": phase,
            "trade_date": clock.trade_date,
            "error_code": e.error_code,
            "message": e.message,
            "details": e.details or {},
        }
        enqueue_outbox_event(
            conn,
            event_type="candidate_pool.rebuild_failed",
            entity_type="candidate_pool",
            entity_id=target_pool,
            source="session_scheduler",
            severity="warning",
            payload=payload,
        )
        return {"rebuilt": False, **payload}
    except Exception as e:
        payload = {
            "session_id": session_id,
            "loop_no": loop_no,
            "pool_name": target_pool,
            "market_phase": clock.phase,
            "scheduler_phase": phase,
            "trade_date": clock.trade_date,
            "error_code": "CANDIDATE_POOL_REBUILD_FAILED",
            "message": str(e),
            "details": {},
        }
        enqueue_outbox_event(
            conn,
            event_type="candidate_pool.rebuild_failed",
            entity_type="candidate_pool",
            entity_id=target_pool,
            source="session_scheduler",
            severity="warning",
            payload=payload,
        )
        return {"rebuilt": False, **payload}

    payload = {
        "session_id": session_id,
        "loop_no": loop_no,
        "pool_name": target_pool,
        "candidate_batch_id": generated.get("candidate_batch_id"),
        "item_count": generated.get("item_count"),
        "market_phase": clock.phase,
        "scheduler_phase": phase,
        "trade_date": clock.trade_date,
    }
    enqueue_outbox_event(
        conn,
        event_type="candidate_pool.rebuilt",
        entity_type="candidate_pool",
        entity_id=str(generated.get("candidate_batch_id") or target_pool),
        source="session_scheduler",
        severity="info",
        payload=payload,
    )
    return {"rebuilt": True, **payload}
