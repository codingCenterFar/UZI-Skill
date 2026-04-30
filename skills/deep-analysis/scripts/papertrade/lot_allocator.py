from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _next_day(d: str | None) -> str | None:
    if not d:
        return None
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return None


def _unit_cost(row: sqlite3.Row | dict[str, Any]) -> float:
    data = row if isinstance(row, dict) else dict(row)
    open_qty = int(data.get("open_qty") or 0)
    if open_qty <= 0:
        return 0.0
    return float(data.get("open_price") or 0.0) + (
        float(data.get("open_fee_alloc") or 0.0) + float(data.get("open_tax_alloc") or 0.0)
    ) / float(open_qty)


def create_buy_lot(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    buy_fill_id: str,
    buy_intent_id: str,
    qty: int,
    price: float,
    fee: float,
    tax: float,
    trade_date: str,
    now_ms: int | None = None,
) -> str:
    now = int(now_ms if now_ms is not None else _now_ms())
    qty_i = max(0, int(qty))
    if qty_i <= 0:
        raise ValueError("buy lot qty must be > 0")
    lot_id = _new_id("lot")
    t1 = _next_day(trade_date) or str(trade_date)
    conn.execute(
        """
        INSERT INTO position_lots (
            lot_id, ticker, buy_fill_id, buy_intent_id,
            open_qty, remaining_qty, open_price, open_fee_alloc, open_tax_alloc,
            open_trade_date, t1_sellable_date, status, opened_at_ms, closed_at_ms,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL, ?, ?)
        """,
        (
            lot_id,
            str(ticker).upper(),
            str(buy_fill_id),
            str(buy_intent_id),
            qty_i,
            qty_i,
            float(price),
            float(fee),
            float(tax),
            str(trade_date),
            t1,
            now,
            now,
            now,
        ),
    )
    return lot_id


def ensure_lot_from_legacy_position(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    intent_id: str,
    as_of_date: str,
) -> int:
    cnt_row = conn.execute(
        "SELECT COUNT(*) AS n FROM position_lots WHERE ticker=? AND status='open' AND remaining_qty > 0",
        (str(ticker).upper(),),
    ).fetchone()
    if cnt_row and int(cnt_row["n"]) > 0:
        return int(cnt_row["n"])

    pos = conn.execute("SELECT * FROM paper_positions WHERE ticker=?", (str(ticker).upper(),)).fetchone()
    if not pos:
        return 0
    qty = int(float(pos["quantity"] or 0.0))
    if qty <= 0:
        return 0

    t1 = str(pos["t1_sellable_date"] or "") or (str(as_of_date))
    now = _now_ms()
    lot_id = _new_id("lot_legacy")
    conn.execute(
        """
        INSERT INTO position_lots (
            lot_id, ticker, buy_fill_id, buy_intent_id,
            open_qty, remaining_qty, open_price, open_fee_alloc, open_tax_alloc,
            open_trade_date, t1_sellable_date, status, opened_at_ms, closed_at_ms,
            created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'open', ?, NULL, ?, ?)
        """,
        (
            lot_id,
            str(ticker).upper(),
            f"legacy_fill_{ticker}",
            str(intent_id),
            qty,
            qty,
            float(pos["avg_cost"] or 0.0),
            str(as_of_date),
            t1,
            now,
            now,
            now,
        ),
    )
    return 1


def allocate_sell_lots(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    sell_fill_id: str,
    qty: int,
    close_price: float,
    fee: float,
    tax: float,
    trade_date: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now = int(now_ms if now_ms is not None else _now_ms())
    req_qty = max(0, int(qty))
    if req_qty <= 0:
        return {"allocated_qty": 0, "realized_pnl": 0.0}

    lots = conn.execute(
        """
        SELECT *
        FROM position_lots
        WHERE ticker=? AND status='open' AND remaining_qty > 0 AND t1_sellable_date <= ?
        ORDER BY open_trade_date ASC, opened_at_ms ASC, created_at_ms ASC, rowid ASC
        """,
        (str(ticker).upper(), str(trade_date)),
    ).fetchall()

    remain = req_qty
    realized_total = 0.0
    for lot in lots:
        if remain <= 0:
            break
        lot_remaining = int(lot["remaining_qty"] or 0)
        if lot_remaining <= 0:
            continue
        alloc_qty = min(remain, lot_remaining)
        ratio = float(alloc_qty) / float(req_qty)
        sell_cost_alloc = (float(fee) + float(tax)) * ratio
        unit_cost = _unit_cost(lot)
        realized = (float(close_price) - unit_cost) * float(alloc_qty) - sell_cost_alloc
        realized_total += realized

        conn.execute(
            """
            INSERT INTO lot_allocations (
                allocation_id, sell_fill_id, lot_id, qty, open_price, close_price, realized_pnl, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("la"),
                str(sell_fill_id),
                str(lot["lot_id"]),
                int(alloc_qty),
                float(lot["open_price"] or 0.0),
                float(close_price),
                float(realized),
                now,
            ),
        )

        new_remaining = lot_remaining - alloc_qty
        new_status = "closed" if new_remaining <= 0 else "open"
        conn.execute(
            """
            UPDATE position_lots
            SET remaining_qty=?, status=?, closed_at_ms=CASE WHEN ?='closed' THEN ? ELSE closed_at_ms END, updated_at_ms=?
            WHERE lot_id=?
            """,
            (
                int(new_remaining),
                new_status,
                new_status,
                now,
                now,
                str(lot["lot_id"]),
            ),
        )
        remain -= alloc_qty

    return {"allocated_qty": req_qty - remain, "realized_pnl": float(realized_total)}


def project_position_from_lots(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    market: str,
    trade_date: str,
    last_price: float,
    last_action: str,
) -> dict[str, Any]:
    tk = str(ticker).upper()
    lots = conn.execute(
        """
        SELECT *
        FROM position_lots
        WHERE ticker=? AND status='open' AND remaining_qty > 0
        """,
        (tk,),
    ).fetchall()

    qty = 0
    sellable_qty = 0
    cost_total = 0.0
    lot_count_open = 0
    min_t1: str | None = None
    for lot in lots:
        rem = int(lot["remaining_qty"] or 0)
        if rem <= 0:
            continue
        lot_count_open += 1
        qty += rem
        unit_cost = _unit_cost(lot)
        cost_total += unit_cost * rem
        lot_t1 = str(lot["t1_sellable_date"] or "")
        if lot_t1 and lot_t1 <= str(trade_date):
            sellable_qty += rem
        if lot_t1 and (min_t1 is None or lot_t1 < min_t1):
            min_t1 = lot_t1

    avg_cost = (cost_total / float(qty)) if qty > 0 else 0.0
    px = float(last_price if last_price is not None else 0.0)
    market_value = float(qty) * px
    unrealized = market_value - cost_total

    realized_row = conn.execute(
        """
        SELECT COALESCE(SUM(la.realized_pnl), 0) AS rp
        FROM lot_allocations la
        JOIN position_lots pl ON pl.lot_id = la.lot_id
        WHERE pl.ticker=?
        """,
        (tk,),
    ).fetchone()
    realized = float(realized_row["rp"]) if realized_row else 0.0
    now = _now_ms()
    watermark = now

    conn.execute(
        """
        INSERT INTO positions (
            ticker, qty, sellable_qty, avg_cost, last_price, market_value, unrealized_pnl,
            realized_pnl_cum, lot_count_open, source_lot_watermark, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            qty=excluded.qty,
            sellable_qty=excluded.sellable_qty,
            avg_cost=excluded.avg_cost,
            last_price=excluded.last_price,
            market_value=excluded.market_value,
            unrealized_pnl=excluded.unrealized_pnl,
            realized_pnl_cum=excluded.realized_pnl_cum,
            lot_count_open=excluded.lot_count_open,
            source_lot_watermark=excluded.source_lot_watermark,
            updated_at_ms=excluded.updated_at_ms
        """,
        (
            tk,
            int(qty),
            int(sellable_qty),
            float(avg_cost),
            float(px),
            float(market_value),
            float(unrealized),
            float(realized),
            int(lot_count_open),
            int(watermark),
            now,
        ),
    )

    meta = {
        "sellable_qty": int(sellable_qty),
        "lot_count_open": int(lot_count_open),
        "source": "lot_projection",
    }
    conn.execute(
        """
        INSERT INTO paper_positions (
            ticker, market, quantity, avg_cost, last_price, market_value, unrealized_pnl,
            realized_pnl, t1_sellable_date, last_action, updated_at, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            market=excluded.market,
            quantity=excluded.quantity,
            avg_cost=excluded.avg_cost,
            last_price=excluded.last_price,
            market_value=excluded.market_value,
            unrealized_pnl=excluded.unrealized_pnl,
            realized_pnl=excluded.realized_pnl,
            t1_sellable_date=excluded.t1_sellable_date,
            last_action=excluded.last_action,
            updated_at=excluded.updated_at,
            meta_json=excluded.meta_json
        """,
        (
            tk,
            str(market),
            float(qty),
            float(avg_cost),
            float(px),
            float(market_value),
            float(unrealized),
            float(realized),
            min_t1,
            str(last_action),
            datetime.now().isoformat(timespec="seconds"),
            json.dumps(meta, ensure_ascii=False),
        ),
    )

    return {
        "ticker": tk,
        "qty": int(qty),
        "sellable_qty": int(sellable_qty),
        "avg_cost": float(avg_cost),
        "last_price": float(px),
        "market_value": float(market_value),
        "unrealized_pnl": float(unrealized),
        "realized_pnl": float(realized),
        "lot_count_open": int(lot_count_open),
    }


def sellable_qty_from_lots(conn: sqlite3.Connection, *, ticker: str, trade_date: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(remaining_qty), 0) AS qty
        FROM position_lots
        WHERE ticker=? AND status='open' AND remaining_qty > 0 AND t1_sellable_date <= ?
        """,
        (str(ticker).upper(), str(trade_date)),
    ).fetchone()
    return int(row["qty"]) if row else 0
