from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.intent_service import create_intent  # noqa: E402
from papertrade.ledger import init_db, upsert_position  # noqa: E402
from papertrade.rule_engine import RuleValidationContext, validate_and_record_rule_check  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(dt_s: str) -> int:
    return int(datetime.fromisoformat(dt_s).timestamp() * 1000)


def test_rule_engine_rejects_odd_lot_buy():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    intent = create_intent(
        conn,
        source="manual",
        idempotency_key="rule-odd-lot",
        ticker="600519.SH",
        side="BUY",
        qty=150,
        order_type="LIMIT",
        limit_price=100.0,
    )

    result = validate_and_record_rule_check(
        conn,
        ctx=RuleValidationContext(
            intent_id=intent.intent_id,
            ticker="600519.SH",
            market="A",
            side="BUY",
            qty=150,
            order_type="LIMIT",
            limit_price=100.0,
            trade_date="2026-04-23",
            request_ts_ms=_ts_ms("2026-04-23T10:00:00"),
            quote_price=100.0,
        ),
        commission_bps=2.0,
        stamp_duty_bps=10.0,
    )

    assert result.allow is False
    assert result.reject_code == "RULE_ODD_LOT_BUY"
    row = conn.execute("SELECT result FROM order_rule_checks WHERE intent_id=? ORDER BY attempt_no DESC LIMIT 1", (intent.intent_id,)).fetchone()
    assert row and row["result"] == "reject"


def test_rule_engine_rejects_tplus1_sell():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    upsert_position(
        conn,
        ticker="600519.SH",
        market="A",
        quantity=100,
        avg_cost=100.0,
        last_price=100.0,
        realized_pnl=0.0,
        t1_sellable_date="2026-04-24",
        last_action="BUY",
        meta={},
    )

    intent = create_intent(
        conn,
        source="manual",
        idempotency_key="rule-tplus1",
        ticker="600519.SH",
        side="SELL",
        qty=100,
        order_type="LIMIT",
        limit_price=101.0,
    )

    result = validate_and_record_rule_check(
        conn,
        ctx=RuleValidationContext(
            intent_id=intent.intent_id,
            ticker="600519.SH",
            market="A",
            side="SELL",
            qty=100,
            order_type="LIMIT",
            limit_price=101.0,
            trade_date="2026-04-23",
            request_ts_ms=_ts_ms("2026-04-23T10:03:00"),
            quote_price=101.0,
        ),
        commission_bps=2.0,
        stamp_duty_bps=10.0,
    )

    assert result.allow is False
    assert result.reject_code == "RULE_TPLUS1_VIOLATION"


def test_rule_engine_rejects_market_closed_order():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    intent = create_intent(
        conn,
        source="manual",
        idempotency_key="rule-market-closed",
        ticker="600519.SH",
        side="BUY",
        qty=100,
        order_type="LIMIT",
        limit_price=100.0,
    )

    result = validate_and_record_rule_check(
        conn,
        ctx=RuleValidationContext(
            intent_id=intent.intent_id,
            ticker="600519.SH",
            market="A",
            side="BUY",
            qty=100,
            order_type="LIMIT",
            limit_price=100.0,
            trade_date="2026-04-23",
            request_ts_ms=_ts_ms("2026-04-23T20:00:00"),
            quote_price=100.0,
        ),
        commission_bps=2.0,
        stamp_duty_bps=10.0,
    )

    assert result.allow is False
    assert result.reject_code == "RULE_MARKET_CLOSED"


def test_rule_engine_rejects_when_sellable_qty_is_insufficient():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    upsert_position(
        conn,
        ticker="600519.SH",
        market="A",
        quantity=100,
        avg_cost=100.0,
        last_price=100.0,
        realized_pnl=0.0,
        t1_sellable_date="2026-04-23",
        last_action="BUY",
        meta={},
    )
    conn.execute(
        """
        INSERT INTO positions (
            ticker, qty, sellable_qty, avg_cost, last_price, market_value, unrealized_pnl,
            realized_pnl_cum, lot_count_open, source_lot_watermark, updated_at_ms
        ) VALUES ('600519.SH', 100, 40, 100.0, 100.0, 10000.0, 0.0, 0.0, 1, 1, 1)
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
        """
    )

    intent = create_intent(
        conn,
        source="manual",
        idempotency_key="rule-sellable-insufficient",
        ticker="600519.SH",
        side="SELL",
        qty=60,
        order_type="LIMIT",
        limit_price=101.0,
    )

    result = validate_and_record_rule_check(
        conn,
        ctx=RuleValidationContext(
            intent_id=intent.intent_id,
            ticker="600519.SH",
            market="A",
            side="SELL",
            qty=60,
            order_type="LIMIT",
            limit_price=101.0,
            trade_date="2026-04-23",
            request_ts_ms=_ts_ms("2026-04-23T10:03:00"),
            quote_price=101.0,
        ),
        commission_bps=2.0,
        stamp_duty_bps=10.0,
    )

    assert result.allow is False
    assert result.reject_code == "RULE_INSUFFICIENT_SELLABLE"
