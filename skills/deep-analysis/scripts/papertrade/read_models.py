from __future__ import annotations

import json
import sqlite3
from typing import Any

from papertrade.api_common import PaperTradeAPIError
from papertrade.api_events import query_events
from papertrade.candidate_pool import get_candidate_pool_top, list_candidate_pool_batches
from papertrade.health import check_runtime_health
from papertrade.metrics import query_runtime_metrics
from papertrade.quote_snapshots import get_latest_quote_snapshots, get_quote_batch

OPEN_INTENT_STATUSES = ("intent", "validated", "queued", "partial")


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


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


def _ticker(value: str) -> str:
    out = str(value or "").strip().upper()
    if not out:
        raise PaperTradeAPIError("INVALID_REQUEST", "ticker is required", {"field": "ticker"})
    return out


def _row_to_position(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "ticker": str(data.get("ticker") or "").upper(),
        "qty": _i(data.get("qty")),
        "sellable_qty": _i(data.get("sellable_qty")),
        "avg_cost": round(_f(data.get("avg_cost")), 4),
        "last_price": round(_f(data.get("last_price")), 4),
        "market_value": round(_f(data.get("market_value")), 4),
        "unrealized_pnl": round(_f(data.get("unrealized_pnl")), 4),
        "realized_pnl_cum": round(_f(data.get("realized_pnl_cum")), 4),
        "lot_count_open": _i(data.get("lot_count_open")),
        "updated_at_ms": data.get("updated_at_ms"),
    }


def _legacy_row_to_position(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "ticker": str(data.get("ticker") or "").upper(),
        "qty": _i(data.get("quantity")),
        "sellable_qty": _i(data.get("quantity")),
        "avg_cost": round(_f(data.get("avg_cost")), 4),
        "last_price": round(_f(data.get("last_price")), 4),
        "market_value": round(_f(data.get("market_value")), 4),
        "unrealized_pnl": round(_f(data.get("unrealized_pnl")), 4),
        "realized_pnl_cum": round(_f(data.get("realized_pnl")), 4),
        "lot_count_open": 0,
        "updated_at_ms": None,
    }


def _latest_runtime_session(conn: sqlite3.Connection, *, running_only: bool = False) -> dict[str, Any] | None:
    if running_only:
        row = conn.execute(
            """
            SELECT *
            FROM runtime_sessions
            WHERE status IN ('starting', 'running', 'stopping')
            ORDER BY updated_at_ms DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM runtime_sessions ORDER BY updated_at_ms DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _latest_runtime_loop(conn: sqlite3.Connection, session_id: str | None = None) -> dict[str, Any] | None:
    if session_id:
        row = conn.execute(
            """
            SELECT *
            FROM runtime_loops
            WHERE session_id=?
            ORDER BY loop_no DESC, started_at_ms DESC
            LIMIT 1
            """,
            (str(session_id),),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM runtime_loops ORDER BY started_at_ms DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _latest_decision(conn: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM decision_snapshots
        WHERE ticker=?
        ORDER BY created_at_ms DESC
        LIMIT 1
        """,
        (_ticker(ticker),),
    ).fetchone()
    return dict(row) if row else None


def _latest_signal(conn: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM signals
        WHERE ticker=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (_ticker(ticker),),
    ).fetchone()
    return dict(row) if row else None


def get_positions_model(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM positions WHERE qty > 0 ORDER BY market_value DESC").fetchall()
    if rows:
        return {"items": [_row_to_position(row) for row in rows]}

    legacy = conn.execute("SELECT * FROM paper_positions WHERE quantity > 0 ORDER BY market_value DESC").fetchall()
    return {"items": [_legacy_row_to_position(row) for row in legacy]}


def _position_by_ticker(conn: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    t = _ticker(ticker)
    row = conn.execute("SELECT * FROM positions WHERE ticker=?", (t,)).fetchone()
    if row:
        return _row_to_position(row)
    legacy = conn.execute("SELECT * FROM paper_positions WHERE ticker=?", (t,)).fetchone()
    if legacy:
        return _legacy_row_to_position(legacy)
    return None


def _candidate_bucket(action_state: Any, position: dict[str, Any] | None) -> str:
    if position and _i(position.get("qty")) > 0:
        return "POSITION"
    action = str(action_state or "").upper()
    if "AVOID" in action:
        return "AVOID"
    if "WAIT" in action:
        return "WAIT"
    if action in {"CANDIDATE_A", "PAPER_WATCH_B", "OBSERVE", "CANDIDATE_OBSERVE"}:
        return "OBSERVE"
    if action == "PAPER_BUY_A":
        return "BUY_READY"
    return "OBSERVE"


def _trigger_price_from_candidate(item: dict[str, Any]) -> float | None:
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    staleness = item.get("staleness") if isinstance(item.get("staleness"), dict) else {}
    candidates: list[Any] = [
        item.get("trigger_price"),
        item.get("wait_trigger_price"),
        meta.get("trigger_price"),
        meta.get("wait_trigger_price"),
        staleness.get("trigger_price"),
    ]
    levels = meta.get("levels") if isinstance(meta.get("levels"), dict) else {}
    candidates.extend(
        [
            levels.get("retest_entry"),
            levels.get("breakout_trigger"),
            levels.get("breakout_price"),
            levels.get("support"),
        ]
    )
    for value in candidates:
        price = _f(value, 0.0)
        if price > 0:
            return round(price, 4)
    return None


def _quote_by_ticker(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    latest = get_latest_quote_snapshots(conn, tickers=tickers, limit=len(tickers))
    return {str(item.get("ticker") or "").upper(): item for item in latest.get("items", [])}


def get_dashboard_summary_model(conn: sqlite3.Connection) -> dict[str, Any]:
    nav = conn.execute(
        """
        SELECT cash, position_market_value, equity, cumulative_return_pct, drawdown_pct, as_of_ts_ms AS ts_ms
        FROM portfolio_nav_snapshots
        ORDER BY as_of_ts_ms DESC
        LIMIT 1
        """
    ).fetchone()
    if nav is None:
        nav = conn.execute(
            """
            SELECT cash, position_market_value, equity, cumulative_return_pct, drawdown_pct, NULL AS ts_ms
            FROM paper_nav_daily
            ORDER BY as_of_date DESC
            LIMIT 1
            """
        ).fetchone()

    if nav is None:
        cash_row = conn.execute("SELECT cash FROM account_state WHERE id=1").fetchone()
        cash = _f(cash_row["cash"] if cash_row else 0.0)
        mv_row = conn.execute("SELECT COALESCE(SUM(market_value), 0) AS mv FROM positions WHERE qty > 0").fetchone()
        mv = _f(mv_row["mv"] if mv_row else 0.0)
        nav_data = {
            "equity": round(cash + mv, 4),
            "cash": round(cash, 4),
            "position_market_value": round(mv, 4),
            "drawdown_pct": 0.0,
            "cumulative_return_pct": 0.0,
        }
    else:
        nav_data = {
            "equity": round(_f(nav["equity"]), 4),
            "cash": round(_f(nav["cash"]), 4),
            "position_market_value": round(_f(nav["position_market_value"]), 4),
            "drawdown_pct": round(_f(nav["drawdown_pct"]), 4),
            "cumulative_return_pct": round(_f(nav["cumulative_return_pct"]), 4),
        }

    runtime = _latest_runtime_session(conn)
    runtime_metrics = query_runtime_metrics(conn, limit=100)
    pending = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='pending'").fetchone()

    return {
        **nav_data,
        "runtime": {
            "status": runtime.get("status") if runtime else "stopped",
            "session_id": runtime.get("session_id") if runtime else None,
            "last_loop_id": runtime.get("last_loop_id") if runtime else None,
        },
        "freshness": {
            "quote_freshness_ms": _i(runtime_metrics.get("quote_freshness_ms")),
            "analysis_freshness_ms": _i(runtime_metrics.get("analysis_freshness_ms")),
            "event_queue_lag": _i(pending["n"]) if pending else 0,
        },
    }


def _watchlist_symbols(conn: sqlite3.Connection, watchlist_id: str | None) -> tuple[str, list[str]]:
    wl_id = str(watchlist_id or "wl_default")
    row = conn.execute(
        "SELECT * FROM watchlists WHERE watchlist_id=? AND status='active'",
        (wl_id,),
    ).fetchone()
    if row:
        symbols = _loads(row["symbols_json"], [])
        return wl_id, [_ticker(x) for x in symbols if str(x).strip()]

    if wl_id != "wl_default":
        raise PaperTradeAPIError("INVALID_REQUEST", "watchlist not found", {"watchlist_id": wl_id})

    rows = conn.execute(
        """
        SELECT ticker FROM positions WHERE qty > 0
        UNION
        SELECT ticker FROM paper_positions WHERE quantity > 0
        UNION
        SELECT ticker FROM signals
        UNION
        SELECT ticker FROM decision_snapshots
        ORDER BY ticker
        """
    ).fetchall()
    return wl_id, [_ticker(row["ticker"]) for row in rows]


def get_watchlist_model(conn: sqlite3.Connection, *, watchlist_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    limit_n = max(1, min(1000, int(limit)))
    wl_id, symbols = _watchlist_symbols(conn, watchlist_id)
    items: list[dict[str, Any]] = []
    for ticker in symbols[:limit_n]:
        decision = _latest_decision(conn, ticker)
        signal = None if decision else _latest_signal(conn, ticker)
        pos = _position_by_ticker(conn, ticker)
        summary = _loads(decision.get("summary_json") if decision else None, {}) if decision else _loads(signal.get("summary_json") if signal else None, {})
        staleness = _loads(decision.get("staleness_flags_json") if decision else None, []) if decision else []
        items.append(
            {
                "ticker": ticker,
                "last_price": (pos or {}).get("last_price", 0.0),
                "change_pct": summary.get("change_pct"),
                "action": decision.get("action") if decision else (signal.get("action") if signal else "OBSERVE"),
                "score_final": round(_f(decision.get("score_final") if decision else (signal.get("score_final") if signal else 0.0)), 4),
                "decision_basis": decision.get("decision_basis") if decision else "legacy_signal",
                "analysis_age_ms": _i(decision.get("analysis_age_ms") if decision else 0),
                "quote_age_ms": _i(decision.get("quote_age_ms") if decision else 0),
                "staleness_flags": staleness if isinstance(staleness, list) else [],
            }
        )
    return {"watchlist_id": wl_id, "items": items}


def get_candidate_pool_model(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None = None,
    candidate_batch_id: str | None = None,
    limit: int = 50,
    status: str | None = "active",
    include_archived: bool = False,
) -> dict[str, Any]:
    pool = get_candidate_pool_top(
        conn,
        pool_name=pool_name,
        candidate_batch_id=candidate_batch_id,
        limit=limit,
        status=status,
        include_archived=include_archived,
    )
    return {
        **pool,
        "batches": list_candidate_pool_batches(
            conn,
            pool_name=pool_name or pool.get("pool_name"),
            status=None if include_archived else status,
            limit=20,
        ),
    }


def get_dashboard_candidates_model(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None = None,
    candidate_batch_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    pool = get_candidate_pool_model(
        conn,
        pool_name=pool_name,
        candidate_batch_id=candidate_batch_id,
        limit=limit,
        status="active",
        include_archived=False,
    )
    tickers = [str(item.get("ticker") or "").upper() for item in pool.get("items", []) if item.get("ticker")]
    quotes = _quote_by_ticker(conn, tickers)
    enriched: list[dict[str, Any]] = []
    quote_missing = 0
    quote_failed = 0
    for item in pool.get("items", []):
        ticker = str(item.get("ticker") or "").upper()
        pos = _position_by_ticker(conn, ticker)
        quote = quotes.get(ticker)
        quote_status = str((quote or {}).get("status") or "missing")
        if quote_status == "missing":
            quote_missing += 1
        elif quote_status != "ok":
            quote_failed += 1
        quote_age = _i((quote or {}).get("quote_age_ms"), _i(item.get("quote_age_ms")))
        analysis_age = _i(item.get("analysis_age_ms"))
        enriched.append(
            {
                **item,
                "bucket": _candidate_bucket(item.get("action_state"), pos),
                "position_qty": _i((pos or {}).get("qty")),
                "is_position": bool(pos and _i(pos.get("qty")) > 0),
                "trigger_price": _trigger_price_from_candidate(item),
                "latest_quote": quote,
                "quote_status": quote_status,
                "quote_price": (quote or {}).get("price"),
                "quote_change_pct": (quote or {}).get("change_pct"),
                "quote_source": (quote or {}).get("source"),
                "quote_age_ms": quote_age,
                "analysis_age_ms": analysis_age,
                "freshness_flags": [
                    flag
                    for flag, active in (
                        ("quote_missing", quote_status == "missing"),
                        ("quote_failed", quote_status not in {"ok", "missing"}),
                        ("quote_stale", quote_age > 5 * 60 * 1000),
                        ("analysis_stale", analysis_age > 24 * 60 * 60 * 1000),
                    )
                    if active
                ],
            }
        )

    buckets: dict[str, int] = {}
    for item in enriched:
        bucket = str(item.get("bucket") or "OBSERVE")
        buckets[bucket] = buckets.get(bucket, 0) + 1

    return {
        "candidate_batch_id": pool.get("candidate_batch_id"),
        "pool_name": pool.get("pool_name"),
        "status": pool.get("status"),
        "generated_at_ms": pool.get("generated_at_ms"),
        "item_count": len(enriched),
        "buckets": buckets,
        "freshness": {
            "quote_missing_count": quote_missing,
            "quote_failed_count": quote_failed,
        },
        "items": enriched,
        "batches": pool.get("batches", []),
    }


def get_quote_snapshots_model(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return get_latest_quote_snapshots(conn, tickers=tickers, limit=limit)


def get_quote_batch_model(
    conn: sqlite3.Connection,
    *,
    quote_batch_id: str,
    limit: int = 500,
) -> dict[str, Any]:
    return get_quote_batch(conn, quote_batch_id=quote_batch_id, limit=limit)


def get_ticker_detail_model(conn: sqlite3.Connection, *, ticker: str) -> dict[str, Any]:
    t = _ticker(ticker)
    decision = _latest_decision(conn, t)
    watcher = None
    if decision and decision.get("watcher_overlay_snapshot_id"):
        row = conn.execute(
            "SELECT * FROM watcher_overlay_snapshots WHERE watcher_overlay_snapshot_id=?",
            (str(decision["watcher_overlay_snapshot_id"]),),
        ).fetchone()
        watcher = dict(row) if row else None
    signal = None if decision else _latest_signal(conn, t)

    latest_decision: dict[str, Any] | None
    if decision:
        latest_decision = {
            "decision_snapshot_id": decision.get("decision_snapshot_id"),
            "action": decision.get("action"),
            "score_final": round(_f(decision.get("score_final")), 4),
            "penalties": _loads(decision.get("penalties_json"), []),
            "gates": _loads(decision.get("gates_json"), []),
            "summary": _loads(decision.get("summary_json"), {}),
            "decision_basis": decision.get("decision_basis"),
            "watcher_overlay": {
                "score_before": round(_f(watcher.get("score_before") if watcher else None), 4),
                "score_after": round(_f(watcher.get("score_after") if watcher else None), 4),
                "votes": _loads(watcher.get("votes_json") if watcher else None, []),
            } if watcher else None,
        }
    elif signal:
        latest_decision = {
            "decision_snapshot_id": None,
            "action": signal.get("action"),
            "score_final": round(_f(signal.get("score_final")), 4),
            "penalties": _loads(signal.get("penalties_json"), []),
            "gates": _loads(signal.get("gates_json"), []),
            "summary": _loads(signal.get("summary_json"), {}),
            "decision_basis": "legacy_signal",
            "watcher_overlay": None,
        }
    else:
        latest_decision = None

    return {
        "ticker": t,
        "latest_decision": latest_decision,
        "position": _position_by_ticker(conn, t),
        "open_intents": get_orders_model(conn, ticker=t, status="open", limit=20)["items"],
        "recent_orders": get_orders_model(conn, ticker=t, status="all", limit=20)["items"],
        "recent_fills": _recent_fills(conn, t, limit=20),
    }


def _recent_fills(conn: sqlite3.Connection, ticker: str, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM paper_fills
        WHERE ticker=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (_ticker(ticker), int(limit)),
    ).fetchall()
    return [
        {
            "fill_id": row["id"],
            "order_id": row["order_id"],
            "ticker": row["ticker"],
            "side": row["side"],
            "qty": _i(row["qty"]),
            "price": round(_f(row["price"]), 4),
            "fee": round(_f(row["fee"]), 4),
            "tax": round(_f(row["tax"]), 4),
            "value": round(_f(row["value"]), 4),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_orders_model(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit_n = max(1, min(500, int(limit)))
    clauses: list[str] = []
    params: list[Any] = []
    if ticker:
        clauses.append("oi.ticker=?")
        params.append(_ticker(ticker))

    status_norm = str(status or "").strip().lower()
    if status_norm == "open":
        clauses.append(f"oi.status IN ({','.join('?' for _ in OPEN_INTENT_STATUSES)})")
        params.extend(OPEN_INTENT_STATUSES)
    elif status_norm and status_norm != "all":
        clauses.append("oi.status=?")
        params.append(status_norm)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            oi.*,
            po.id AS legacy_order_id,
            po.status AS order_status,
            po.order_qty,
            po.filled_qty,
            po.fill_price,
            po.created_at AS order_created_at
        FROM order_intents oi
        LEFT JOIN paper_orders po ON CAST(po.id AS TEXT)=oi.latest_order_id
        {where}
        ORDER BY oi.created_at_ms DESC
        LIMIT ?
        """,
        tuple(params + [limit_n]),
    ).fetchall()

    items = []
    for row in rows:
        order_id = row["latest_order_id"] or row["legacy_order_id"]
        items.append(
            {
                "intent_id": row["intent_id"],
                "order_id": order_id,
                "source": row["source"],
                "status": row["status"],
                "ticker": row["ticker"],
                "side": row["side"],
                "qty": _i(row["qty"]),
                "filled_qty": _i(row["filled_qty"]),
                "avg_fill_price": round(_f(row["fill_price"]), 4) if row["fill_price"] is not None else None,
                "decision_snapshot_id": row["decision_snapshot_id"],
                "rule_check_snapshot_id": row["rule_check_snapshot_id"],
                "reject_code": row["reject_code"],
                "reject_reason": row["reject_reason"],
                "created_at_ms": row["created_at_ms"],
            }
        )
    return {"items": items}


def get_events_model(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    cursor: str | None = None,
    order: str = "desc",
    include_pending: bool = True,
) -> dict[str, Any]:
    try:
        return query_events(
            conn,
            limit=limit,
            cursor=cursor,
            order=order,
            include_pending=include_pending,
        )
    except ValueError as e:
        raise PaperTradeAPIError("INVALID_REQUEST", str(e), {"limit": limit, "order": order}) from e


def get_runtime_status_model(conn: sqlite3.Connection) -> dict[str, Any]:
    runtime = _latest_runtime_session(conn, running_only=True)
    if not runtime:
        raise PaperTradeAPIError(
            "RUNTIME_NOT_RUNNING",
            "papertrade runtime is not running",
            retryable=True,
            status_code=409,
        )
    lease = conn.execute("SELECT * FROM runtime_leases WHERE lease_name='papertrade_runtime'").fetchone()
    loop = _latest_runtime_loop(conn, str(runtime.get("session_id") or ""))
    return {
        "runtime": {
            "status": runtime.get("status"),
            "session_id": runtime.get("session_id"),
            "owner_pid": runtime.get("owner_pid"),
            "heartbeat_at_ms": runtime.get("heartbeat_at_ms"),
            "last_loop_id": runtime.get("last_loop_id"),
        },
        "lease": {
            "lease_name": lease["lease_name"],
            "lease_expires_at_ms": lease["lease_expires_at_ms"],
            "owner_session_id": lease["owner_session_id"],
        } if lease else None,
        "last_loop": {
            "loop_id": loop.get("loop_id"),
            "status": loop.get("status"),
            "duration_ms": loop.get("duration_ms"),
        } if loop else None,
    }


def get_metrics_model(conn: sqlite3.Connection, *, limit: int = 100) -> dict[str, Any]:
    return query_runtime_metrics(conn, limit=limit)


def get_health_model(
    conn: sqlite3.Connection,
    *,
    consistency_threshold: float = 0.01,
    emit_alerts: bool = False,
) -> dict[str, Any]:
    return check_runtime_health(
        conn,
        consistency_threshold=consistency_threshold,
        emit_alerts=emit_alerts,
    )
