from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.api_common import now_ms  # noqa: E402
from papertrade.candidate_pool import DEFAULT_POOL_NAME, create_candidate_pool_batch  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.quote_snapshots import get_quote_batch  # noqa: E402
from papertrade.run_realtime import (  # noqa: E402
    realtime_refresh_mode,
    resolve_realtime_universe,
    run_quote_only_refresh,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_candidate_pool(conn: sqlite3.Connection, *, pool_name: str = "rt_pool") -> None:
    create_candidate_pool_batch(
        conn,
        candidate_batch_id=f"cpb_{pool_name}",
        pool_name=pool_name,
        source="pytest",
        as_of_date="2026-04-26",
        generated_at_ms=1_000,
        items=[
            {
                "ticker": "600519",
                "name": "Moutai",
                "candidate_score": 88.0,
                "action_state": "CANDIDATE_A",
            },
            {
                "ticker": "002361.SZ",
                "name": "Shenjian",
                "candidate_score": 66.0,
                "action_state": "PULLBACK_WAIT",
            },
        ],
    )


def _seed_watchlist(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO watchlists (
            watchlist_id, name, is_default, symbols_json, status, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        ("wl_rt", "Realtime", 1, json.dumps(["002361.SZ", "300750.SZ"]), 1_000, 1_000),
    )


def _seed_position(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            ticker, qty, sellable_qty, avg_cost, last_price, market_value,
            unrealized_pnl, realized_pnl_cum, lot_count_open, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("601318.SH", 100, 100, 42.0, 43.0, 4_300.0, 100.0, 0.0, 1, 1_500),
    )


def _provider(ticker: str) -> dict[str, Any]:
    return {
        "ok": True,
        "ticker": ticker,
        "market": "A",
        "name": f"name-{ticker}",
        "price": 10.0 + len(ticker),
        "change_pct": 0.25,
        "source": "pytest_provider",
        "ts_ms": now_ms() - 1000,
    }


def test_realtime_universe_merges_explicit_candidate_watchlist_and_positions():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)
    _seed_candidate_pool(conn, pool_name="rt_pool")
    _seed_watchlist(conn)
    _seed_position(conn)

    universe = resolve_realtime_universe(
        conn,
        explicit_tickers=["000001.SZ", "600519.SH"],
        candidate_pool_name="rt_pool",
        candidate_limit=10,
        watchlist_id="wl_rt",
        watchlist_limit=10,
        include_positions=True,
    )

    assert universe["tickers"] == ["000001.SZ", "600519.SH", "002361.SZ", "300750.SZ", "601318.SH"]
    assert universe["sources"]["explicit"] == ["000001.SZ", "600519.SH"]
    assert universe["sources"]["candidate_pool"] == ["600519.SH", "002361.SZ"]
    assert universe["sources"]["watchlist"] == ["002361.SZ", "300750.SZ"]
    assert universe["sources"]["positions"] == ["601318.SH"]


def test_realtime_universe_defaults_to_active_candidate_pool_without_tickers():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)
    _seed_candidate_pool(conn, pool_name=DEFAULT_POOL_NAME)

    universe = resolve_realtime_universe(conn, explicit_tickers=[])

    assert universe["tickers"] == ["600519.SH", "002361.SZ"]
    assert universe["candidate_pool_name"] == DEFAULT_POOL_NAME
    assert universe["sources"]["watchlist"] == []


def test_realtime_refresh_mode_separates_full_cached_and_quote_only_loops():
    assert realtime_refresh_mode(loop_no=2, refresh_every_loops=1)["mode"] == "full_refresh"
    assert realtime_refresh_mode(loop_no=1, refresh_every_loops=3)["mode"] == "full_refresh"
    assert realtime_refresh_mode(loop_no=2, refresh_every_loops=3)["mode"] == "cached_cycle"
    assert realtime_refresh_mode(loop_no=4, refresh_every_loops=3)["mode"] == "full_refresh"
    quote_only = realtime_refresh_mode(loop_no=2, refresh_every_loops=3, quote_only_between_full=True)
    assert quote_only["mode"] == "quote_only"
    assert quote_only["cycle_from_cache_only"] is True
    cache_forced = realtime_refresh_mode(
        loop_no=1,
        refresh_every_loops=3,
        from_cache_only=True,
        quote_only_between_full=True,
    )
    assert cache_forced["mode"] == "quote_only"
    assert cache_forced["is_full_refresh"] is False


def test_quote_only_refresh_writes_quote_snapshots_without_running_cycle():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    refreshed = run_quote_only_refresh(
        conn,
        tickers=["000001.SZ", "600519.SH"],
        session_id="session_pytest_001",
        loop_no=2,
        provider=_provider,
    )

    assert refreshed["quote_batch_id"] == "qbatch_session_pyte_000002"
    assert refreshed["ticker_count"] == 2
    assert refreshed["ok_count"] == 2

    batch = get_quote_batch(conn, quote_batch_id="qbatch_session_pyte_000002")
    assert [item["ticker"] for item in batch["items"]] == ["000001.SZ", "600519.SH"]
