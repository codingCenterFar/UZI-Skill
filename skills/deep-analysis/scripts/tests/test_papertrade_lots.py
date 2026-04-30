from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.ledger import create_order, fill_order, get_cash, init_db, record_nav  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_buy_fill_creates_lot_and_same_day_sellable_is_zero():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    order_id = create_order(
        conn,
        run_id="run_lot_buy_001",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        side="BUY",
        action="MANUAL_ORDER",
        order_price=100.0,
        order_qty=100,
        slippage_bps=0.0,
        reason="buy for lot projection",
        policy_snapshot={},
        source={},
    )
    fill_order(
        conn,
        order_id=order_id,
        intent_id="it_lot_buy_001",
        run_id="run_lot_buy_001",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        side="BUY",
        fill_price=100.0,
        fill_qty=100,
        fee=2.0,
        tax=0.0,
    )

    lot = conn.execute("SELECT * FROM position_lots WHERE ticker='600519.SH'").fetchone()
    pos = conn.execute("SELECT * FROM positions WHERE ticker='600519.SH'").fetchone()

    assert lot is not None
    assert int(lot["open_qty"]) == 100
    assert int(lot["remaining_qty"]) == 100
    assert str(lot["t1_sellable_date"]) == "2026-04-24"

    assert pos is not None
    assert int(pos["qty"]) == 100
    assert int(pos["sellable_qty"]) == 0


def test_lot_fifo_allocation_updates_realized_and_unrealized():
    conn = _conn()
    init_db(conn, initial_cash=10_000.0)

    order_buy_1 = create_order(
        conn,
        run_id="run_lot_fifo_001",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        side="BUY",
        action="MANUAL_ORDER",
        order_price=10.0,
        order_qty=100,
        slippage_bps=0.0,
        reason="buy lot1",
        policy_snapshot={},
        source={},
    )
    fill_order(
        conn,
        order_id=order_buy_1,
        intent_id="it_lot_fifo_buy_1",
        run_id="run_lot_fifo_001",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        side="BUY",
        fill_price=10.0,
        fill_qty=100,
        fee=1.0,
        tax=0.0,
    )

    order_buy_2 = create_order(
        conn,
        run_id="run_lot_fifo_001",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        side="BUY",
        action="MANUAL_ORDER",
        order_price=12.0,
        order_qty=100,
        slippage_bps=0.0,
        reason="buy lot2",
        policy_snapshot={},
        source={},
    )
    fill_order(
        conn,
        order_id=order_buy_2,
        intent_id="it_lot_fifo_buy_2",
        run_id="run_lot_fifo_001",
        as_of_date="2026-04-23",
        ticker="600519.SH",
        market="A",
        side="BUY",
        fill_price=12.0,
        fill_qty=100,
        fee=1.2,
        tax=0.0,
    )

    order_sell = create_order(
        conn,
        run_id="run_lot_fifo_001",
        as_of_date="2026-04-24",
        ticker="600519.SH",
        side="SELL",
        action="MANUAL_ORDER",
        order_price=11.0,
        order_qty=150,
        slippage_bps=0.0,
        reason="sell fifo lots",
        policy_snapshot={},
        source={},
    )
    fill_order(
        conn,
        order_id=order_sell,
        intent_id="it_lot_fifo_sell_1",
        run_id="run_lot_fifo_001",
        as_of_date="2026-04-24",
        ticker="600519.SH",
        market="A",
        side="SELL",
        fill_price=11.0,
        fill_qty=150,
        fee=1.5,
        tax=0.0,
    )

    alloc_rows = conn.execute(
        "SELECT qty, realized_pnl FROM lot_allocations ORDER BY qty DESC"
    ).fetchall()
    pos = conn.execute("SELECT * FROM positions WHERE ticker='600519.SH'").fetchone()
    cash = get_cash(conn)
    nav = record_nav(conn, run_id="run_lot_fifo_001", as_of_date="2026-04-24", initial_cash=10_000.0)

    assert len(alloc_rows) == 2
    assert int(alloc_rows[0]["qty"]) == 100
    assert int(alloc_rows[1]["qty"]) == 50

    assert pos is not None
    assert int(pos["qty"]) == 50
    assert int(pos["sellable_qty"]) == 50
    assert abs(float(pos["realized_pnl_cum"]) - 46.9) < 1e-6
    assert abs(float(pos["unrealized_pnl"]) - (-50.6)) < 1e-6

    assert abs((cash + float(pos["market_value"])) - float(nav["equity"])) < 1e-6
