from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Callable

from lib.market_router import parse_ticker

from papertrade.api_common import PaperTradeAPIError, new_id, now_ms
from papertrade.candidate_pool import get_candidate_pool_top
from papertrade.market_snapshot import DEFAULT_QUOTE_TIMEOUT_SECONDS, fetch_realtime_snapshot


QuoteProvider = Callable[[str], dict[str, Any]]


def _json_dumps(value: Any, default: Any) -> str:
    data = default if value is None else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        data = json.loads(raw)
    except Exception:
        return default
    return data


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


def _parse_ts_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10_000_000_000 else v * 1000
    text = str(value).strip()
    if text.isdigit():
        v = int(text)
        return v if v > 10_000_000_000 else v * 1000
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _ticker(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise PaperTradeAPIError("INVALID_REQUEST", "ticker is required", {"field": "ticker"})
    parsed = parse_ticker(raw)
    return str(parsed.full or raw).upper(), str(parsed.market or "").upper()


def _unique_tickers(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker, _ = _ticker(value)
        if ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def _position_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT ticker FROM positions WHERE qty > 0
        UNION
        SELECT ticker FROM paper_positions WHERE quantity > 0
        ORDER BY ticker
        """
    ).fetchall()
    return [str(row["ticker"]) for row in rows]


def _candidate_tickers(
    conn: sqlite3.Connection,
    *,
    candidate_batch_id: str | None = None,
    pool_name: str | None = None,
    limit: int = 100,
) -> list[str]:
    if not candidate_batch_id and not pool_name:
        return []
    pool = get_candidate_pool_top(
        conn,
        pool_name=pool_name,
        candidate_batch_id=candidate_batch_id,
        limit=limit,
    )
    return [str(item.get("ticker") or "") for item in pool.get("items", []) if item.get("ticker")]


def resolve_quote_universe(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    include_positions: bool = False,
    candidate_batch_id: str | None = None,
    pool_name: str | None = None,
    candidate_limit: int = 100,
) -> list[str]:
    raw: list[Any] = []
    raw.extend(tickers or [])
    raw.extend(_candidate_tickers(conn, candidate_batch_id=candidate_batch_id, pool_name=pool_name, limit=candidate_limit))
    if include_positions:
        raw.extend(_position_tickers(conn))
    return _unique_tickers(raw)


def _row_to_quote(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "quote_snapshot_id": data.get("quote_snapshot_id"),
        "quote_batch_id": data.get("quote_batch_id"),
        "ticker": str(data.get("ticker") or "").upper(),
        "market": data.get("market"),
        "name": data.get("name"),
        "price": _f(data.get("price")) if data.get("price") is not None else None,
        "change_pct": _f(data.get("change_pct")) if data.get("change_pct") is not None else None,
        "source": data.get("source"),
        "status": data.get("status"),
        "quote_ts_ms": data.get("quote_ts_ms"),
        "quote_age_ms": _i(data.get("quote_age_ms")),
        "error_code": data.get("error_code"),
        "error_reason": data.get("error_reason"),
        "payload": _json_loads(data.get("payload_json"), {}),
        "request_context": _json_loads(data.get("request_context_json"), {}),
        "created_at_ms": data.get("created_at_ms"),
    }


def _store_quote_snapshot(
    conn: sqlite3.Connection,
    *,
    quote_batch_id: str,
    ticker: str,
    snapshot: dict[str, Any],
    request_context: dict[str, Any],
    created_at_ms: int,
) -> dict[str, Any]:
    ticker_norm, market = _ticker(snapshot.get("ticker") or ticker)
    quote_ts_ms = _parse_ts_ms(snapshot.get("ts_ms") or snapshot.get("ts"))
    quote_age_ms = max(0, created_at_ms - quote_ts_ms) if quote_ts_ms is not None else 0
    ok = bool(snapshot.get("ok"))
    source = str(snapshot.get("source") or request_context.get("source") or "fetch_basic")
    price = _f(snapshot.get("price"), 0.0)
    row_id = new_id("qts")
    conn.execute(
        """
        INSERT INTO quote_snapshots (
            quote_snapshot_id, quote_batch_id, ticker, market, name,
            price, change_pct, source, status, quote_ts_ms, quote_age_ms,
            error_code, error_reason, payload_json, request_context_json, created_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            quote_batch_id,
            ticker_norm,
            str(snapshot.get("market") or market or "").upper() or market,
            snapshot.get("name"),
            price if ok and price > 0 else None,
            snapshot.get("change_pct") if ok else None,
            source,
            "ok" if ok else "failed",
            quote_ts_ms,
            quote_age_ms,
            None if ok else str(snapshot.get("error_code") or "QUOTE_FETCH_FAILED"),
            None if ok else str(snapshot.get("reason") or snapshot.get("error_reason") or "quote fetch failed"),
            _json_dumps(snapshot, {}),
            _json_dumps(request_context, {}),
            created_at_ms,
        ),
    )
    row = conn.execute("SELECT * FROM quote_snapshots WHERE quote_snapshot_id=?", (row_id,)).fetchone()
    return _row_to_quote(row)


def record_quote_snapshot(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    snapshot: dict[str, Any],
    quote_batch_id: str | None = None,
    source: str = "runtime_quote_overlay",
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    batch_id = str(quote_batch_id or new_id("qbatch"))
    context = {"source": source, **(request_context or {})}
    return _store_quote_snapshot(
        conn,
        quote_batch_id=batch_id,
        ticker=ticker,
        snapshot=snapshot,
        request_context=context,
        created_at_ms=now_ms(),
    )


def refresh_quote_snapshots(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    include_positions: bool = False,
    candidate_batch_id: str | None = None,
    pool_name: str | None = None,
    candidate_limit: int = 100,
    provider: QuoteProvider | None = None,
    source: str = "fetch_basic",
    quote_batch_id: str | None = None,
    provider_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    universe = resolve_quote_universe(
        conn,
        tickers=tickers,
        include_positions=include_positions,
        candidate_batch_id=candidate_batch_id,
        pool_name=pool_name,
        candidate_limit=candidate_limit,
    )
    if not universe:
        raise PaperTradeAPIError(
            "INVALID_REQUEST",
            "quote refresh requires tickers, candidate pool, or positions",
            {"field": "tickers"},
        )

    batch_id = str(quote_batch_id or new_id("qbatch"))
    default_provider = provider is None
    provider_fn = provider or fetch_realtime_snapshot
    effective_timeout_seconds = (
        DEFAULT_QUOTE_TIMEOUT_SECONDS
        if default_provider and provider_timeout_seconds is None
        else provider_timeout_seconds
    )
    created = now_ms()
    items: list[dict[str, Any]] = []
    for ticker in universe:
        try:
            if default_provider:
                snapshot = fetch_realtime_snapshot(ticker, timeout_seconds=effective_timeout_seconds) or {}
            else:
                snapshot = provider_fn(ticker) or {}
            if not isinstance(snapshot, dict):
                snapshot = {"ok": False, "ticker": ticker, "reason": "provider returned non-object"}
        except Exception as e:
            snapshot = {"ok": False, "ticker": ticker, "reason": str(e), "error_code": "QUOTE_PROVIDER_EXCEPTION"}
        items.append(
            _store_quote_snapshot(
                conn,
                quote_batch_id=batch_id,
                ticker=ticker,
                snapshot=snapshot,
                request_context={
                    "source": source,
                    "explicit_tickers": bool(tickers),
                    "include_positions": bool(include_positions),
                    "candidate_batch_id": candidate_batch_id,
                    "pool_name": pool_name,
                    "candidate_limit": int(candidate_limit),
                    "provider_timeout_seconds": effective_timeout_seconds,
                    "provider_mode": "default_fetch_basic" if default_provider else "custom",
                },
                created_at_ms=created,
            )
        )

    ok_count = sum(1 for item in items if item.get("status") == "ok")
    failed_count = len(items) - ok_count
    return {
        "quote_batch_id": batch_id,
        "ticker_count": len(items),
        "ok_count": ok_count,
        "failed_count": failed_count,
        "items": items,
    }


def get_latest_quote_snapshots(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    limit_n = max(1, min(500, int(limit)))
    params: list[Any] = []
    where = ""
    if tickers:
        normalized = _unique_tickers(list(tickers))
        where = f"WHERE ticker IN ({','.join('?' for _ in normalized)})"
        params.extend(normalized)
    rows = conn.execute(
        f"""
        SELECT *
        FROM (
            SELECT
                q.*,
                ROW_NUMBER() OVER (
                    PARTITION BY q.ticker
                    ORDER BY q.created_at_ms DESC, q.quote_snapshot_id DESC
                ) AS rn
            FROM quote_snapshots q
            {where}
        )
        WHERE rn=1
        ORDER BY created_at_ms DESC, ticker ASC
        LIMIT ?
        """,
        tuple(params + [limit_n]),
    ).fetchall()
    items = [_row_to_quote(row) for row in rows]
    return {"items": items}


def get_quote_batch(conn: sqlite3.Connection, *, quote_batch_id: str, limit: int = 500) -> dict[str, Any]:
    batch_id = str(quote_batch_id or "").strip()
    if not batch_id:
        raise PaperTradeAPIError("INVALID_REQUEST", "quote_batch_id is required", {"field": "quote_batch_id"})
    rows = conn.execute(
        """
        SELECT *
        FROM quote_snapshots
        WHERE quote_batch_id=?
        ORDER BY created_at_ms DESC, ticker ASC
        LIMIT ?
        """,
        (batch_id, max(1, min(1000, int(limit)))),
    ).fetchall()
    if not rows:
        raise PaperTradeAPIError("NOT_FOUND", "quote batch not found", {"quote_batch_id": batch_id}, status_code=404)
    items = [_row_to_quote(row) for row in rows]
    return {
        "quote_batch_id": batch_id,
        "ticker_count": len(items),
        "ok_count": sum(1 for item in items if item.get("status") == "ok"),
        "failed_count": sum(1 for item in items if item.get("status") != "ok"),
        "items": items,
    }
