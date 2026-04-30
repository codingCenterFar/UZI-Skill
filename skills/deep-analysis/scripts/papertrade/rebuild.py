from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any

from lib.market_router import parse_ticker

from papertrade.api_common import PaperTradeAPIError, new_id, now_ms
from papertrade.ledger import get_cash
from papertrade.lot_allocator import project_position_from_lots

FACT_TABLES = (
    "signals",
    "order_intents",
    "paper_orders",
    "paper_fills",
    "position_lots",
    "lot_allocations",
    "event_outbox",
    "runtime_loops",
)


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


def _today() -> str:
    return date.today().isoformat()


def _normalize_tickers(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw]
    else:
        items = [str(raw).strip()]

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        ticker = parse_ticker(item).full.upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(ticker)
    return out


def _table_counts(conn: sqlite3.Connection, tables: tuple[str, ...] = FACT_TABLES) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        counts[table] = int(row["n"]) if row else 0
    return counts


def _discover_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT ticker FROM position_lots
        UNION
        SELECT ticker FROM positions
        UNION
        SELECT ticker FROM paper_positions
        UNION
        SELECT ticker FROM paper_fills
        ORDER BY ticker ASC
        """
    ).fetchall()
    return _normalize_tickers([row["ticker"] for row in rows])


def _open_lot_qty(conn: sqlite3.Connection, ticker: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(remaining_qty), 0) AS qty
        FROM position_lots
        WHERE ticker=? AND status='open' AND remaining_qty > 0
        """,
        (str(ticker).upper(),),
    ).fetchone()
    return _i(row["qty"]) if row else 0


def _last_price_and_market(conn: sqlite3.Connection, ticker: str) -> tuple[float, str]:
    tk = str(ticker).upper()
    row = conn.execute("SELECT last_price FROM positions WHERE ticker=?", (tk,)).fetchone()
    if row and _f(row["last_price"]) > 0:
        market = parse_ticker(tk).market or "A"
        legacy = conn.execute("SELECT market FROM paper_positions WHERE ticker=?", (tk,)).fetchone()
        if legacy and legacy["market"]:
            market = str(legacy["market"])
        return _f(row["last_price"]), market

    legacy = conn.execute("SELECT market, last_price FROM paper_positions WHERE ticker=?", (tk,)).fetchone()
    if legacy and _f(legacy["last_price"]) > 0:
        return _f(legacy["last_price"]), str(legacy["market"] or parse_ticker(tk).market or "A")

    fill = conn.execute(
        """
        SELECT price
        FROM paper_fills
        WHERE ticker=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (tk,),
    ).fetchone()
    if fill and _f(fill["price"]) > 0:
        return _f(fill["price"]), parse_ticker(tk).market or "A"

    lot = conn.execute(
        """
        SELECT
            COALESCE(SUM(open_price * remaining_qty), 0) AS value,
            COALESCE(SUM(remaining_qty), 0) AS qty
        FROM position_lots
        WHERE ticker=? AND status='open' AND remaining_qty > 0
        """,
        (tk,),
    ).fetchone()
    qty = _f(lot["qty"]) if lot else 0.0
    if qty > 0:
        return _f(lot["value"]) / qty, parse_ticker(tk).market or "A"

    return 0.0, parse_ticker(tk).market or "A"


def _delete_projection(conn: sqlite3.Connection, ticker: str) -> None:
    tk = str(ticker).upper()
    conn.execute("DELETE FROM positions WHERE ticker=?", (tk,))
    conn.execute("DELETE FROM paper_positions WHERE ticker=?", (tk,))


def _record_nav_projection(
    conn: sqlite3.Connection,
    *,
    trade_date: str,
    initial_cash: float,
    rebuild_id: str,
) -> dict[str, Any]:
    cash = get_cash(conn)
    mv_row = conn.execute("SELECT COALESCE(SUM(market_value), 0) AS mv FROM positions WHERE qty > 0").fetchone()
    mv = _f(mv_row["mv"]) if mv_row else 0.0
    equity = cash + mv
    ret_pct = (equity / float(initial_cash) - 1.0) * 100.0 if initial_cash > 0 else 0.0

    peak_row = conn.execute("SELECT MAX(equity) AS peak FROM portfolio_nav_snapshots").fetchone()
    peak = _f(peak_row["peak"], equity) if peak_row else equity
    peak = max(peak, equity)
    drawdown_pct = (equity / peak - 1.0) * 100.0 if peak > 0 else 0.0
    ts_ms = now_ms()
    loop_id = f"rebuild:{rebuild_id}"
    conn.execute(
        """
        INSERT INTO portfolio_nav_snapshots (
            nav_snapshot_id, as_of_date, as_of_ts_ms, session_id, loop_id,
            cash, position_market_value, equity, cumulative_return_pct, drawdown_pct,
            cash_drift_check, equity_recompute_diff, created_at_ms
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, 0, ?)
        ON CONFLICT(loop_id) DO UPDATE SET
            as_of_date=excluded.as_of_date,
            as_of_ts_ms=excluded.as_of_ts_ms,
            cash=excluded.cash,
            position_market_value=excluded.position_market_value,
            equity=excluded.equity,
            cumulative_return_pct=excluded.cumulative_return_pct,
            drawdown_pct=excluded.drawdown_pct,
            created_at_ms=excluded.created_at_ms
        """,
        (
            new_id("nav_rebuild"),
            str(trade_date),
            ts_ms,
            loop_id,
            cash,
            mv,
            equity,
            ret_pct,
            drawdown_pct,
            ts_ms,
        ),
    )
    return {
        "loop_id": loop_id,
        "cash": round(cash, 2),
        "position_market_value": round(mv, 2),
        "equity": round(equity, 2),
        "cumulative_return_pct": round(ret_pct, 3),
        "drawdown_pct": round(drawdown_pct, 3),
    }


def rebuild_read_models(
    conn: sqlite3.Connection,
    *,
    trade_date: str | None = None,
    tickers: Any = None,
    initial_cash: float = 1_000_000.0,
    clear_missing: bool = True,
) -> dict[str, Any]:
    as_of = str(trade_date or _today())
    target_tickers = _normalize_tickers(tickers) or _discover_tickers(conn)
    if not target_tickers:
        raise PaperTradeAPIError("INVALID_REQUEST", "no tickers available to rebuild", {"field": "tickers"})

    before_counts = _table_counts(conn)
    rebuilt: list[dict[str, Any]] = []
    removed: list[str] = []
    for ticker in target_tickers:
        if _open_lot_qty(conn, ticker) <= 0:
            if clear_missing:
                _delete_projection(conn, ticker)
                removed.append(ticker)
            continue
        last_price, market = _last_price_and_market(conn, ticker)
        projection = project_position_from_lots(
            conn,
            ticker=ticker,
            market=market,
            trade_date=as_of,
            last_price=last_price,
            last_action="REBUILD",
        )
        rebuilt.append(projection)

    rebuild_id = new_id("rb")
    nav = _record_nav_projection(
        conn,
        trade_date=as_of,
        initial_cash=float(initial_cash),
        rebuild_id=rebuild_id,
    )
    after_counts = _table_counts(conn)
    return {
        "rebuild_id": rebuild_id,
        "trade_date": as_of,
        "rebuilt_count": len(rebuilt),
        "removed_count": len(removed),
        "items": rebuilt,
        "removed_tickers": removed,
        "nav": nav,
        "fact_counts_before": before_counts,
        "fact_counts_after": after_counts,
        "facts_unchanged": before_counts == after_counts,
        "read_models_written": ["positions", "paper_positions", "portfolio_nav_snapshots"],
    }
