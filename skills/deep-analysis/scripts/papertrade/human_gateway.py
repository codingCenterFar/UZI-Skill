from __future__ import annotations

from datetime import datetime
from typing import Any

from papertrade.command_service import submit_intent_order
from papertrade.config import PaperTradeConfig


def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_human_action(
    conn,
    *,
    as_of_date: str,
    ticker: str,
    action: str,
    side: str | None = None,
    qty: float | None = None,
    price: float | None = None,
    note: str = "",
    source: str = "manual",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO human_actions (
            as_of_date, ticker, action, side, qty, price, note, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (as_of_date, ticker, action, side, qty, price, note, source, _now_ts()),
    )
    return int(cur.lastrowid)


def import_live_mirror_trade(
    conn,
    *,
    as_of_date: str,
    ticker: str,
    side: str,
    qty: float,
    price: float,
    fee: float = 0.0,
    tax: float = 0.0,
    source: str = "manual",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO live_mirror_trades (
            as_of_date, ticker, side, qty, price, fee, tax, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (as_of_date, ticker, side, qty, price, fee, tax, source, _now_ts()),
    )
    return int(cur.lastrowid)


def list_human_actions(conn, as_of_date: str | None = None) -> list[dict[str, Any]]:
    if as_of_date:
        cur = conn.execute(
            "SELECT * FROM human_actions WHERE as_of_date=? ORDER BY id DESC",
            (as_of_date,),
        )
    else:
        cur = conn.execute("SELECT * FROM human_actions ORDER BY id DESC LIMIT 200")
    return [dict(r) for r in cur.fetchall()]


def submit_manual_order(
    conn,
    *,
    cfg: PaperTradeConfig,
    as_of_date: str,
    ticker: str,
    market: str,
    side: str,
    qty: int,
    price: float,
    idempotency_key: str,
    order_type: str = "LIMIT",
    time_in_force: str = "IOC",
    note: str = "",
    run_id: str | None = None,
    operator_id: str = "manual_operator",
    operator_channel: str = "manual_api",
    sim_only: bool = False,
    queue_when_market_closed: bool = False,
    request_ts_ms: int | None = None,
    instrument_name: str | None = None,
) -> dict[str, Any]:
    manual_run_id = run_id or f"manual:{as_of_date}"
    reason = note or "manual order"
    log_human_action(
        conn,
        as_of_date=as_of_date,
        ticker=ticker,
        action="SIM_ORDER_SUBMIT",
        side=side,
        qty=float(qty),
        price=float(price),
        note=reason,
        source="manual",
    )

    result = submit_intent_order(
        conn,
        run_id=manual_run_id,
        as_of_date=as_of_date,
        ticker=ticker,
        market=market,
        source="manual",
        idempotency_key=idempotency_key,
        side=side,
        qty=int(qty),
        price=float(price),
        action="MANUAL_ORDER",
        order_reason=reason,
        order_type=order_type,
        time_in_force=time_in_force,
        operator_id=operator_id,
        operator_channel=operator_channel,
        sim_only=bool(sim_only),
        queue_when_market_closed=bool(queue_when_market_closed),
        request_ts_ms=request_ts_ms,
        instrument_name=instrument_name,
        lot_size=cfg.trade.lot_size,
        commission_bps=cfg.trade.commission_bps,
        stamp_duty_bps=cfg.trade.stamp_duty_bps,
        slippage_bps=cfg.trade.slippage_bps,
    )
    return result
