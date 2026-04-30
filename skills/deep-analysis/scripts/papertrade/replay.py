from __future__ import annotations

import json
import sqlite3
from typing import Any

from papertrade.api_common import PaperTradeAPIError
from papertrade.outbox import outbox_row_to_envelope


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


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def _intent_rows(conn: sqlite3.Connection, *, loop_id: str, signal_ids: list[int]) -> list[dict[str, Any]]:
    clauses = ["loop_id=?", "idempotency_key LIKE ?"]
    params: list[Any] = [loop_id, f"auto:{loop_id}:%"]
    if signal_ids:
        clauses.append(f"signal_id IN ({_placeholders(signal_ids)})")
        params.extend(signal_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM order_intents
        WHERE {' OR '.join(clauses)}
        ORDER BY created_at_ms ASC
        """,
        tuple(params),
    ).fetchall()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        intent_id = str(data.get("intent_id") or "")
        if intent_id in seen:
            continue
        seen.add(intent_id)
        out.append(data)
    return out


def _fill_rows(conn: sqlite3.Connection, *, loop_id: str, order_ids: list[int]) -> list[dict[str, Any]]:
    clauses = ["run_id=?"]
    params: list[Any] = [loop_id]
    if order_ids:
        clauses.append(f"order_id IN ({_placeholders(order_ids)})")
        params.extend(order_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM paper_fills
        WHERE {' OR '.join(clauses)}
        ORDER BY id ASC
        """,
        tuple(params),
    ).fetchall()
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        fill_id = int(data.get("id") or 0)
        if fill_id in seen:
            continue
        seen.add(fill_id)
        out.append(data)
    return out


def _event_rows(conn: sqlite3.Connection, *, loop_id: str, event_batch_id: str | None) -> list[dict[str, Any]]:
    clauses = ["entity_id=?", "causation_id=?"]
    params: list[Any] = [loop_id, loop_id]
    if event_batch_id:
        clauses.append("event_batch_id=?")
        params.append(event_batch_id)
    rows = conn.execute(
        f"""
        SELECT *
        FROM event_outbox
        WHERE {' OR '.join(clauses)}
        ORDER BY created_at_ms ASC, event_id ASC
        """,
        tuple(params),
    ).fetchall()
    return [outbox_row_to_envelope(row) for row in rows]


def _positions_from_loop_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for fill in fills:
        ticker = str(fill.get("ticker") or "").upper()
        if not ticker:
            continue
        side = str(fill.get("side") or "").upper()
        qty = _i(fill.get("qty"))
        price = _f(fill.get("price"))
        fee = _f(fill.get("fee"))
        tax = _f(fill.get("tax"))
        item = state.setdefault(
            ticker,
            {
                "ticker": ticker,
                "qty": 0,
                "cost_basis": 0.0,
                "last_price": price,
                "buy_qty": 0,
                "sell_qty": 0,
                "fill_count": 0,
            },
        )
        item["last_price"] = price or item["last_price"]
        item["fill_count"] += 1
        if side == "BUY":
            item["qty"] += qty
            item["buy_qty"] += qty
            item["cost_basis"] += qty * price + fee + tax
        elif side == "SELL":
            old_qty = int(item["qty"] or 0)
            avg_cost = float(item["cost_basis"] or 0.0) / old_qty if old_qty > 0 else 0.0
            sell_qty = min(qty, old_qty) if old_qty > 0 else qty
            item["qty"] -= sell_qty
            item["sell_qty"] += sell_qty
            item["cost_basis"] = max(0.0, float(item["cost_basis"] or 0.0) - avg_cost * sell_qty)

    out: list[dict[str, Any]] = []
    for item in state.values():
        qty = int(item["qty"] or 0)
        last_price = _f(item.get("last_price"))
        cost_basis = _f(item.get("cost_basis"))
        avg_cost = cost_basis / qty if qty > 0 else 0.0
        market_value = qty * last_price
        out.append(
            {
                "ticker": item["ticker"],
                "qty": qty,
                "avg_cost": round(avg_cost, 4),
                "last_price": round(last_price, 4),
                "market_value": round(market_value, 4),
                "unrealized_pnl": round(market_value - cost_basis, 4),
                "buy_qty": int(item["buy_qty"] or 0),
                "sell_qty": int(item["sell_qty"] or 0),
                "fill_count": int(item["fill_count"] or 0),
            }
        )
    return sorted(out, key=lambda x: x["ticker"])


def _current_positions(conn: sqlite3.Connection, tickers: list[str]) -> list[dict[str, Any]]:
    if not tickers:
        return []
    rows = conn.execute(
        f"""
        SELECT *
        FROM positions
        WHERE ticker IN ({_placeholders(tickers)})
        ORDER BY ticker ASC
        """,
        tuple(tickers),
    ).fetchall()
    return [
        {
            "ticker": row["ticker"],
            "qty": _i(row["qty"]),
            "sellable_qty": _i(row["sellable_qty"]),
            "avg_cost": round(_f(row["avg_cost"]), 4),
            "last_price": round(_f(row["last_price"]), 4),
            "market_value": round(_f(row["market_value"]), 4),
            "unrealized_pnl": round(_f(row["unrealized_pnl"]), 4),
            "realized_pnl_cum": round(_f(row["realized_pnl_cum"]), 4),
        }
        for row in rows
    ]


def replay_loop(conn: sqlite3.Connection, *, loop_id: str) -> dict[str, Any]:
    loop_key = str(loop_id or "").strip()
    if not loop_key:
        raise PaperTradeAPIError("INVALID_REQUEST", "loop_id is required", {"field": "loop_id"})

    loop = _row(conn.execute("SELECT * FROM runtime_loops WHERE loop_id=?", (loop_key,)).fetchone())
    if not loop:
        raise PaperTradeAPIError(
            "LOOP_NOT_FOUND",
            "runtime loop not found",
            {"loop_id": loop_key},
            status_code=404,
        )

    session = _row(
        conn.execute(
            "SELECT * FROM runtime_sessions WHERE session_id=?",
            (str(loop.get("session_id") or ""),),
        ).fetchone()
    )
    signals = _rows(
        conn.execute(
            "SELECT * FROM signals WHERE run_id=? ORDER BY id ASC",
            (loop_key,),
        ).fetchall()
    )
    signal_ids = [int(row["id"]) for row in signals if row.get("id") is not None]
    intents = _intent_rows(conn, loop_id=loop_key, signal_ids=signal_ids)
    orders = _rows(
        conn.execute(
            "SELECT * FROM paper_orders WHERE run_id=? ORDER BY id ASC",
            (loop_key,),
        ).fetchall()
    )
    order_ids = [int(row["id"]) for row in orders if row.get("id") is not None]
    fills = _fill_rows(conn, loop_id=loop_key, order_ids=order_ids)
    touched_tickers = sorted({str(row.get("ticker") or "").upper() for row in fills + orders + signals if row.get("ticker")})
    nav_snapshot = _row(
        conn.execute(
            """
            SELECT *
            FROM portfolio_nav_snapshots
            WHERE loop_id=?
            ORDER BY as_of_ts_ms DESC
            LIMIT 1
            """,
            (loop_key,),
        ).fetchone()
    )
    nav_daily = _row(
        conn.execute(
            """
            SELECT *
            FROM paper_nav_daily
            WHERE run_id=?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (loop_key,),
        ).fetchone()
    )
    event_batch_id = str(loop.get("event_batch_id") or "") or None
    events = _event_rows(conn, loop_id=loop_key, event_batch_id=event_batch_id)

    return {
        "loop": {
            "loop_id": loop.get("loop_id"),
            "session_id": loop.get("session_id"),
            "loop_no": _i(loop.get("loop_no")),
            "status": loop.get("status"),
            "event_batch_id": loop.get("event_batch_id"),
            "ticker_count": _i(loop.get("ticker_count")),
            "success_count": _i(loop.get("success_count")),
            "failed_count": _i(loop.get("failed_count")),
            "metrics": _loads(loop.get("metrics_json"), {}),
            "decision_basis_summary": _loads(loop.get("decision_basis_summary_json"), {}),
            "staleness_summary": _loads(loop.get("staleness_summary_json"), {}),
        },
        "session": {
            "session_id": session.get("session_id"),
            "status": session.get("status"),
            "config_hash": session.get("config_hash"),
        }
        if session
        else None,
        "counts": {
            "signals": len(signals),
            "intents": len(intents),
            "orders": len(orders),
            "fills": len(fills),
            "events": len(events),
        },
        "signals": signals,
        "intents": intents,
        "orders": orders,
        "fills": fills,
        "events": events,
        "nav": {
            "snapshot": nav_snapshot,
            "daily": nav_daily,
        },
        "reconstructed": {
            "basis": "loop_fills_only",
            "tickers": touched_tickers,
            "positions_from_loop_fills": _positions_from_loop_fills(fills),
            "current_positions": _current_positions(conn, touched_tickers),
        },
    }
