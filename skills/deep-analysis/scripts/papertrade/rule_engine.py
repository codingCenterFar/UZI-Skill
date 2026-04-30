from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from papertrade.instrument_metadata_provider import InstrumentMetadataProvider
from papertrade.ledger import get_cash, get_position, get_position_qty, get_sellable_qty
from papertrade.market_calendar_service import MarketCalendarService


RULE_ENGINE_VERSION = "ashare-rule-v1"


@dataclass(frozen=True)
class RuleValidationContext:
    intent_id: str
    ticker: str
    market: str
    side: str
    qty: int
    order_type: str
    limit_price: float | None
    trade_date: str
    request_ts_ms: int | None = None
    instrument_name: str | None = None
    decision_basis: str | None = None
    analysis_age_ms: int = 0
    quote_age_ms: int = 0
    quote_price: float | None = None


@dataclass(frozen=True)
class RuleValidationResult:
    allow: bool
    reject_code: str | None
    reject_reason: str | None
    checks: list[dict[str, Any]]
    rule_check_snapshot_id: str
    attempt_no: int
    market_phase: str
    trade_date: str


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def validate_and_record_rule_check(
    conn: sqlite3.Connection,
    *,
    ctx: RuleValidationContext,
    commission_bps: float,
    stamp_duty_bps: float,
    calendar_service: MarketCalendarService | None = None,
    instrument_provider: InstrumentMetadataProvider | None = None,
) -> RuleValidationResult:
    t0 = _now_ms()
    calendar = calendar_service or MarketCalendarService()
    instruments = instrument_provider or InstrumentMetadataProvider()

    side = str(ctx.side or "").upper().strip()
    qty = int(ctx.qty)
    limit_price = _f(ctx.limit_price, 0.0)

    clock = calendar.get_clock(ts_ms=ctx.request_ts_ms, trade_date=ctx.trade_date)
    meta = instruments.resolve(ctx.ticker, market=ctx.market, name=ctx.instrument_name)

    checks: list[dict[str, Any]] = []
    reject_code: str | None = None
    reject_reason: str | None = None

    pos = get_position(conn, ctx.ticker)
    pos_qty = _f(get_position_qty(conn, ctx.ticker), 0.0)
    sellable_qty = _f(get_sellable_qty(conn, ctx.ticker, clock.trade_date), 0.0)

    def add_check(name: str, passed: bool, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        nonlocal reject_code, reject_reason
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "code": code,
                "message": message,
                "details": details or {},
            }
        )
        if (not passed) and reject_code is None:
            reject_code = code
            reject_reason = message

    market_open_ok = bool(clock.is_trade_day and clock.is_open_session)
    add_check(
        "market_open",
        market_open_ok,
        "RULE_MARKET_CLOSED",
        "A股当前不在连续竞价时段（MVP 盘外挂单拒绝）",
        {"phase": clock.phase, "trade_date": clock.trade_date},
    )

    add_check(
        "valid_side",
        side in {"BUY", "SELL"},
        "INVALID_REQUEST",
        "side must be BUY or SELL",
        {"side": side},
    )

    add_check(
        "valid_qty",
        qty > 0,
        "INVALID_REQUEST",
        "qty must be > 0",
        {"qty": qty},
    )

    add_check(
        "valid_price",
        limit_price > 0,
        "INVALID_REQUEST",
        "limit price must be > 0",
        {"limit_price": limit_price},
    )

    if side == "BUY":
        add_check(
            "lot_size",
            qty % int(meta.lot_size) == 0,
            "RULE_ODD_LOT_BUY",
            f"A股买入数量需为 {meta.lot_size} 股整数倍",
            {"lot_size": meta.lot_size, "qty": qty},
        )

        value = limit_price * qty
        fee = value * float(commission_bps) / 10000.0
        tax = 0.0
        cash = float(get_cash(conn))
        required = value + fee + tax
        add_check(
            "cash",
            required <= cash + 1e-6,
            "RULE_INSUFFICIENT_CASH",
            "available cash is insufficient",
            {"required": round(required, 4), "cash": round(cash, 4)},
        )

    if side == "SELL":
        add_check(
            "no_short",
            pos_qty > 0,
            "RULE_NO_SHORT_ALLOWED",
            "A股模拟盘不允许裸卖空",
            {"position_qty": round(pos_qty, 4), "request_qty": qty},
        )

        t1_violation = pos_qty + 1e-6 >= qty and sellable_qty <= 1e-6 and qty > 0
        add_check(
            "tplus1",
            not t1_violation,
            "RULE_TPLUS1_VIOLATION",
            "today-bought lots are not sellable yet",
            {
                "position_qty": round(pos_qty, 4),
                "sellable_qty": round(sellable_qty, 4),
                "trade_date": clock.trade_date,
            },
        )

        add_check(
            "sellable_qty",
            sellable_qty + 1e-6 >= qty,
            "RULE_INSUFFICIENT_SELLABLE",
            "sellable quantity is insufficient",
            {"position_qty": round(pos_qty, 4), "sellable_qty": round(sellable_qty, 4), "request_qty": qty},
        )

    quote_price = _f(ctx.quote_price, 0.0)
    if quote_price > 0 and limit_price > 0 and side in {"BUY", "SELL"}:
        upper = quote_price * (1.0 + float(meta.price_limit_pct) / 100.0)
        lower = quote_price * (1.0 - float(meta.price_limit_pct) / 100.0)
        within_limit = (limit_price <= upper + 1e-6) if side == "BUY" else (limit_price >= lower - 1e-6)
        add_check(
            "price_limit",
            within_limit,
            "RULE_PRICE_LIMIT_EXCEEDED",
            "order price exceeds board price limit",
            {
                "limit_price": round(limit_price, 4),
                "quote_price": round(quote_price, 4),
                "limit_up": round(upper, 4),
                "limit_down": round(lower, 4),
                "board": meta.board,
                "price_limit_pct": meta.price_limit_pct,
            },
        )
    else:
        add_check(
            "price_limit",
            True,
            "RULE_PRICE_LIMIT_EXCEEDED",
            "price-limit check skipped (quote unavailable)",
            {"quote_price": quote_price, "limit_price": limit_price},
        )

    allow = reject_code is None
    checked_at_ms = _now_ms()
    latency_ms = max(0, checked_at_ms - t0)

    snapshot_id = _new_id("rcs")
    conn.execute(
        """
        INSERT INTO rule_check_snapshots (
            rule_check_snapshot_id, ticker, side, qty, order_type, limit_price,
            market_phase, trade_date, calendar_version, instrument_version, rule_engine_version,
            decision_basis, analysis_age_ms, quote_age_ms,
            allow, reject_code, reject_reason, checks_json, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            str(ctx.ticker).upper(),
            side,
            qty,
            str(ctx.order_type or "LIMIT").upper(),
            limit_price if limit_price > 0 else None,
            clock.phase,
            clock.trade_date,
            calendar.version,
            instruments.version,
            RULE_ENGINE_VERSION,
            str(ctx.decision_basis or ""),
            int(ctx.analysis_age_ms),
            int(ctx.quote_age_ms),
            1 if allow else 0,
            reject_code,
            reject_reason,
            json.dumps(checks, ensure_ascii=False),
            checked_at_ms,
        ),
    )

    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM order_rule_checks WHERE intent_id=?",
        (str(ctx.intent_id),),
    ).fetchone()
    attempt_no = int(row["n"]) + 1 if row else 1

    conn.execute(
        """
        INSERT INTO order_rule_checks (
            check_id, intent_id, attempt_no, rule_check_snapshot_id, result,
            reject_code, reject_reason, latency_ms, checked_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _new_id("orc"),
            str(ctx.intent_id),
            attempt_no,
            snapshot_id,
            "allow" if allow else "reject",
            reject_code,
            reject_reason,
            latency_ms,
            checked_at_ms,
        ),
    )

    return RuleValidationResult(
        allow=allow,
        reject_code=reject_code,
        reject_reason=reject_reason,
        checks=checks,
        rule_check_snapshot_id=snapshot_id,
        attempt_no=attempt_no,
        market_phase=clock.phase,
        trade_date=clock.trade_date,
    )
