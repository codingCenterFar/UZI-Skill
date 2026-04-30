from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from papertrade.lot_allocator import (
    allocate_sell_lots,
    create_buy_lot,
    ensure_lot_from_legacy_position,
    project_position_from_lots,
    sellable_qty_from_lots,
)
from papertrade.schema_v2 import init_schema_v2


def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_day(d: str | None) -> str | None:
    if not d:
        return None
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return (dt + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return None


def connect(db_path: Path | str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, initial_cash: float) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS job_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL,
            tickers_json TEXT,
            depth TEXT,
            note TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT,
            panel_mode TEXT,
            action TEXT NOT NULL,
            score_final REAL,
            score_strategy REAL,
            score_panel REAL,
            score_tactical REAL,
            score_core REAL,
            bonus_agent REAL,
            penalties_json TEXT,
            gates_json TEXT,
            summary_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            order_price REAL NOT NULL,
            order_qty REAL NOT NULL,
            filled_qty REAL NOT NULL DEFAULT 0,
            fill_price REAL,
            fee REAL NOT NULL DEFAULT 0,
            tax REAL NOT NULL DEFAULT 0,
            slippage_bps REAL NOT NULL DEFAULT 0,
            reason TEXT,
            policy_snapshot_json TEXT,
            source_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            qty REAL NOT NULL,
            fee REAL NOT NULL,
            tax REAL NOT NULL,
            value REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES paper_orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS paper_positions (
            ticker TEXT PRIMARY KEY,
            market TEXT,
            quantity REAL NOT NULL,
            avg_cost REAL NOT NULL,
            last_price REAL NOT NULL,
            market_value REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            t1_sellable_date TEXT,
            last_action TEXT,
            updated_at TEXT NOT NULL,
            meta_json TEXT
        );

        CREATE TABLE IF NOT EXISTS paper_nav_daily (
            as_of_date TEXT PRIMARY KEY,
            run_id TEXT,
            cash REAL NOT NULL,
            position_market_value REAL NOT NULL,
            equity REAL NOT NULL,
            cumulative_return_pct REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            ticker TEXT,
            level TEXT,
            channel TEXT,
            title TEXT,
            body TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS human_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            side TEXT,
            qty REAL,
            price REAL,
            note TEXT,
            source TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS live_mirror_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            tax REAL NOT NULL DEFAULT 0,
            source TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deviation_daily (
            as_of_date TEXT PRIMARY KEY,
            paper_return_pct REAL NOT NULL,
            live_return_pct REAL NOT NULL,
            return_gap_pct REAL NOT NULL,
            avg_entry_slippage_pct REAL NOT NULL,
            trade_count INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            details_json TEXT
        );
        """
    )

    now = _now_ts()
    row = conn.execute("SELECT cash FROM account_state WHERE id=1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO account_state (id, cash, created_at, updated_at) VALUES (1, ?, ?, ?)",
            (float(initial_cash), now, now),
        )

    # v2 schema is additive and coexists with legacy paper_* tables during migration.
    init_schema_v2(conn)
    conn.commit()


def start_job_run(conn: sqlite3.Connection, tickers: list[str], depth: str) -> str:
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO job_runs (run_id, started_at, status, tickers_json, depth) VALUES (?, ?, ?, ?, ?)",
        (run_id, _now_ts(), "running", json.dumps(tickers, ensure_ascii=False), depth),
    )
    conn.commit()
    return run_id


def finish_job_run(conn: sqlite3.Connection, run_id: str, status: str, note: str = "", error: str = "") -> None:
    conn.execute(
        "UPDATE job_runs SET ended_at=?, status=?, note=?, error=? WHERE run_id=?",
        (_now_ts(), status, note, error, run_id),
    )
    conn.commit()


def get_cash(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT cash FROM account_state WHERE id=1").fetchone()
    return float(row["cash"]) if row else 0.0


def adjust_cash(conn: sqlite3.Connection, delta: float) -> None:
    now = _now_ts()
    conn.execute(
        "UPDATE account_state SET cash = cash + ?, updated_at=? WHERE id=1",
        (float(delta), now),
    )


def get_position(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM paper_positions WHERE ticker=?", (ticker,)).fetchone()


def get_position_qty(conn: sqlite3.Connection, ticker: str) -> float:
    row = conn.execute("SELECT qty FROM positions WHERE ticker=?", (str(ticker).upper(),)).fetchone()
    if row and row["qty"] is not None:
        return float(row["qty"])
    pos = get_position(conn, ticker)
    return float(pos["quantity"]) if pos else 0.0


def get_sellable_qty(conn: sqlite3.Connection, ticker: str, trade_date: str) -> float:
    row = conn.execute("SELECT sellable_qty FROM positions WHERE ticker=?", (str(ticker).upper(),)).fetchone()
    if row and row["sellable_qty"] is not None:
        return float(row["sellable_qty"])

    from_lots = sellable_qty_from_lots(conn, ticker=str(ticker).upper(), trade_date=str(trade_date))
    if from_lots > 0:
        return float(from_lots)

    pos = get_position(conn, ticker)
    if not pos:
        return 0.0
    qty = float(pos["quantity"] or 0.0)
    t1 = str(pos["t1_sellable_date"] or "")
    if t1 and t1 > str(trade_date):
        return 0.0
    return qty


def list_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM paper_positions WHERE quantity > 0")
    return list(cur.fetchall())


def upsert_position(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    market: str,
    quantity: float,
    avg_cost: float,
    last_price: float,
    realized_pnl: float,
    t1_sellable_date: str | None,
    last_action: str,
    meta: dict[str, Any] | None = None,
) -> None:
    quantity = float(quantity)
    avg_cost = float(avg_cost)
    last_price = float(last_price)
    mv = quantity * last_price
    unreal = quantity * (last_price - avg_cost)
    now = _now_ts()
    meta_json = json.dumps(meta or {}, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO paper_positions (
            ticker, market, quantity, avg_cost, last_price, market_value,
            unrealized_pnl, realized_pnl, t1_sellable_date, last_action, updated_at, meta_json
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
            ticker,
            market,
            quantity,
            avg_cost,
            last_price,
            mv,
            unreal,
            float(realized_pnl),
            t1_sellable_date,
            last_action,
            now,
            meta_json,
        ),
    )


def record_signal(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of_date: str,
    ticker: str,
    market: str,
    panel_mode: str,
    action: str,
    score_final: float,
    score_strategy: float,
    score_panel: float,
    score_tactical: float,
    score_core: float,
    bonus_agent: float,
    penalties: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    summary: dict[str, Any],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            run_id, as_of_date, ticker, market, panel_mode, action,
            score_final, score_strategy, score_panel, score_tactical, score_core,
            bonus_agent, penalties_json, gates_json, summary_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            as_of_date,
            ticker,
            market,
            panel_mode,
            action,
            float(score_final),
            float(score_strategy),
            float(score_panel),
            float(score_tactical),
            float(score_core),
            float(bonus_agent),
            json.dumps(penalties or [], ensure_ascii=False),
            json.dumps(gates or [], ensure_ascii=False),
            json.dumps(summary or {}, ensure_ascii=False),
            _now_ts(),
        ),
    )
    return int(cur.lastrowid)


def create_order(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of_date: str,
    ticker: str,
    side: str,
    action: str,
    order_price: float,
    order_qty: float,
    slippage_bps: float,
    reason: str,
    policy_snapshot: dict[str, Any],
    source: dict[str, Any],
) -> int:
    now = _now_ts()
    cur = conn.execute(
        """
        INSERT INTO paper_orders (
            run_id, as_of_date, ticker, side, action, status,
            order_price, order_qty, slippage_bps, reason,
            policy_snapshot_json, source_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            as_of_date,
            ticker,
            side,
            action,
            float(order_price),
            float(order_qty),
            float(slippage_bps),
            reason,
            json.dumps(policy_snapshot or {}, ensure_ascii=False),
            json.dumps(source or {}, ensure_ascii=False),
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def fill_order(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    intent_id: str | None = None,
    run_id: str,
    as_of_date: str,
    ticker: str,
    market: str,
    side: str,
    fill_price: float,
    fill_qty: float,
    fee: float,
    tax: float,
) -> dict[str, float]:
    now = _now_ts()
    side_u = str(side).upper()
    req_qty = max(0.0, float(fill_qty))
    if req_qty <= 0:
        return {"cash_delta": 0.0, "realized_delta": 0.0}

    pos = get_position(conn, ticker)
    qty_old = float(pos["quantity"]) if pos else 0.0

    exec_qty = req_qty
    exec_fee = float(fee)
    exec_tax = float(tax)
    if side_u == "SELL":
        ensure_lot_from_legacy_position(
            conn,
            ticker=str(ticker).upper(),
            intent_id=str(intent_id or f"legacy_order_{order_id}"),
            as_of_date=as_of_date,
        )
        sellable = get_sellable_qty(conn, str(ticker).upper(), as_of_date)
        exec_qty = min(req_qty, sellable if sellable > 0 else qty_old)
        if exec_qty <= 0:
            return {"cash_delta": 0.0, "realized_delta": 0.0}
        ratio = float(exec_qty) / float(req_qty) if req_qty > 0 else 0.0
        exec_fee = float(fee) * ratio
        exec_tax = float(tax) * ratio

    value = float(fill_price) * float(exec_qty)
    cur_fill = conn.execute(
        """
        INSERT INTO paper_fills (
            order_id, run_id, as_of_date, ticker, side, price, qty, fee, tax, value, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(order_id),
            run_id,
            as_of_date,
            ticker,
            side_u,
            float(fill_price),
            float(exec_qty),
            float(exec_fee),
            float(exec_tax),
            float(value),
            now,
        ),
    )
    fill_row_id = int(cur_fill.lastrowid)
    fill_ref = f"pf_{fill_row_id}"

    conn.execute(
        """
        UPDATE paper_orders
        SET status='filled', filled_qty=?, fill_price=?, fee=?, tax=?, updated_at=?
        WHERE id=?
        """,
        (float(exec_qty), float(fill_price), float(exec_fee), float(exec_tax), now, int(order_id)),
    )

    realized_delta = 0.0
    if side_u == "BUY":
        cost_with_fee = value + float(exec_fee) + float(exec_tax)
        adjust_cash(conn, -cost_with_fee)
        create_buy_lot(
            conn,
            ticker=str(ticker).upper(),
            buy_fill_id=fill_ref,
            buy_intent_id=str(intent_id or f"legacy_order_{order_id}"),
            qty=int(exec_qty),
            price=float(fill_price),
            fee=float(exec_fee),
            tax=float(exec_tax),
            trade_date=as_of_date,
        )
        project_position_from_lots(
            conn,
            ticker=str(ticker).upper(),
            market=market,
            trade_date=as_of_date,
            last_price=float(fill_price),
            last_action="BUY",
        )
    else:
        alloc = allocate_sell_lots(
            conn,
            ticker=str(ticker).upper(),
            sell_fill_id=fill_ref,
            qty=int(exec_qty),
            close_price=float(fill_price),
            fee=float(exec_fee),
            tax=float(exec_tax),
            trade_date=as_of_date,
        )
        qty_sell = float(alloc["allocated_qty"])
        if qty_sell <= 0:
            return {"cash_delta": 0.0, "realized_delta": 0.0}
        realized_delta = float(alloc["realized_pnl"])
        proceeds = qty_sell * float(fill_price) - float(exec_fee) - float(exec_tax)
        adjust_cash(conn, proceeds)
        project_position_from_lots(
            conn,
            ticker=str(ticker).upper(),
            market=market,
            trade_date=as_of_date,
            last_price=float(fill_price),
            last_action="SELL",
        )

    return {
        "cash_delta": -(value + float(exec_fee) + float(exec_tax))
        if side_u == "BUY"
        else (value - float(exec_fee) - float(exec_tax)),
        "realized_delta": float(realized_delta),
    }


def mark_to_market(conn: sqlite3.Connection, as_of_date: str, latest_prices: dict[str, float]) -> None:
    now = _now_ts()
    for pos in list_positions(conn):
        ticker = str(pos["ticker"])
        px = float(latest_prices.get(ticker, pos["last_price"] or 0.0))
        if px <= 0:
            continue
        lot_cnt_row = conn.execute(
            "SELECT COUNT(*) AS n FROM position_lots WHERE ticker=? AND status='open' AND remaining_qty > 0",
            (ticker,),
        ).fetchone()
        if lot_cnt_row and int(lot_cnt_row["n"]) > 0:
            project_position_from_lots(
                conn,
                ticker=ticker,
                market=str(pos["market"] or "A"),
                trade_date=as_of_date,
                last_price=px,
                last_action=str(pos["last_action"] or "MARK"),
            )
        else:
            qty = float(pos["quantity"])
            avg = float(pos["avg_cost"])
            mv = qty * px
            unreal = qty * (px - avg)
            conn.execute(
                """
                UPDATE paper_positions
                SET last_price=?, market_value=?, unrealized_pnl=?, updated_at=?
                WHERE ticker=?
                """,
                (px, mv, unreal, now, ticker),
            )


def record_nav(
    conn: sqlite3.Connection,
    run_id: str,
    as_of_date: str,
    initial_cash: float,
    *,
    session_id: str | None = None,
    loop_id: str | None = None,
) -> dict[str, float]:
    cash = get_cash(conn)
    row = conn.execute("SELECT COALESCE(SUM(market_value),0) AS mv FROM paper_positions WHERE quantity > 0").fetchone()
    mv = float(row["mv"]) if row else 0.0
    equity = cash + mv
    ret_pct = (equity / float(initial_cash) - 1.0) * 100.0 if initial_cash > 0 else 0.0

    peak_row = conn.execute("SELECT MAX(equity) AS peak FROM paper_nav_daily").fetchone()
    peak = float(peak_row["peak"]) if peak_row and peak_row["peak"] is not None else equity
    peak = max(peak, equity)
    dd_pct = (equity / peak - 1.0) * 100.0 if peak > 0 else 0.0

    conn.execute(
        """
        INSERT INTO paper_nav_daily (
            as_of_date, run_id, cash, position_market_value, equity,
            cumulative_return_pct, drawdown_pct, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(as_of_date) DO UPDATE SET
            run_id=excluded.run_id,
            cash=excluded.cash,
            position_market_value=excluded.position_market_value,
            equity=excluded.equity,
            cumulative_return_pct=excluded.cumulative_return_pct,
            drawdown_pct=excluded.drawdown_pct,
            updated_at=excluded.updated_at
        """,
        (
            as_of_date,
            run_id,
            cash,
            mv,
            equity,
            ret_pct,
            dd_pct,
            _now_ts(),
        ),
    )
    now_ms = int(datetime.now().timestamp() * 1000)
    cash_drift_check = 0.0
    equity_recompute_diff = round((cash + mv) - equity, 6)
    nav_snapshot_id = f"nav_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO portfolio_nav_snapshots (
            nav_snapshot_id, as_of_date, as_of_ts_ms, session_id, loop_id,
            cash, position_market_value, equity, cumulative_return_pct, drawdown_pct,
            cash_drift_check, equity_recompute_diff, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(loop_id) DO UPDATE SET
            as_of_date=excluded.as_of_date,
            as_of_ts_ms=excluded.as_of_ts_ms,
            session_id=excluded.session_id,
            cash=excluded.cash,
            position_market_value=excluded.position_market_value,
            equity=excluded.equity,
            cumulative_return_pct=excluded.cumulative_return_pct,
            drawdown_pct=excluded.drawdown_pct,
            cash_drift_check=excluded.cash_drift_check,
            equity_recompute_diff=excluded.equity_recompute_diff,
            created_at_ms=excluded.created_at_ms
        """,
        (
            nav_snapshot_id,
            as_of_date,
            now_ms,
            session_id,
            loop_id,
            cash,
            mv,
            equity,
            ret_pct,
            dd_pct,
            cash_drift_check,
            equity_recompute_diff,
            now_ms,
        ),
    )
    return {
        "cash": round(cash, 2),
        "position_market_value": round(mv, 2),
        "equity": round(equity, 2),
        "cumulative_return_pct": round(ret_pct, 3),
        "drawdown_pct": round(dd_pct, 3),
        "cash_drift_check": round(cash_drift_check, 6),
        "equity_recompute_diff": round(equity_recompute_diff, 6),
    }


def record_alert(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    as_of_date: str,
    ticker: str,
    level: str,
    channel: str,
    title: str,
    body: str,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO alerts (
            run_id, as_of_date, ticker, level, channel, title, body, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            as_of_date,
            ticker,
            level,
            channel,
            title,
            body,
            json.dumps(payload, ensure_ascii=False),
            _now_ts(),
        ),
    )


def append_jsonl(path: Path | str, obj: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def count_new_buys_today(conn: sqlite3.Connection, as_of_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_orders WHERE as_of_date=? AND side='BUY' AND status='filled'",
        (as_of_date,),
    ).fetchone()
    return int(row["n"]) if row else 0


def recent_nav(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT as_of_date, cash, position_market_value, equity, cumulative_return_pct, drawdown_pct "
        "FROM paper_nav_daily ORDER BY as_of_date DESC LIMIT ?",
        (int(limit),),
    )
    return [dict(r) for r in cur.fetchall()]
