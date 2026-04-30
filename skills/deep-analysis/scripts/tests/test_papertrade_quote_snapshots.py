from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
import papertrade.market_snapshot as market_snapshot  # noqa: E402
from papertrade.api_common import now_ms  # noqa: E402
from papertrade.candidate_pool import create_candidate_pool_batch  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.quote_snapshots import (  # noqa: E402
    get_latest_quote_snapshots,
    get_quote_batch,
    record_quote_snapshot,
    refresh_quote_snapshots,
    resolve_quote_universe,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_candidate_pool(conn: sqlite3.Connection) -> None:
    create_candidate_pool_batch(
        conn,
        candidate_batch_id="cpb_quote_001",
        pool_name="p2_quote",
        source="pytest",
        as_of_date="2026-04-26",
        generated_at_ms=1_000,
        items=[
            {
                "ticker": "600519",
                "name": "Moutai",
                "candidate_score": 91.0,
                "action_state": "CANDIDATE_A",
                "reasons": ["pytest_candidate"],
            },
            {
                "ticker": "002361.SZ",
                "name": "Shenjian",
                "candidate_score": 63.0,
                "action_state": "PULLBACK_WAIT",
                "reasons": ["pytest_candidate"],
            },
        ],
    )


def _seed_position(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            ticker, qty, sellable_qty, avg_cost, last_price, market_value,
            unrealized_pnl, realized_pnl_cum, lot_count_open, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("300750.SZ", 100, 100, 180.0, 188.8, 18_880.0, 880.0, 0.0, 1, 1_500),
    )


def _provider(ticker: str) -> dict[str, Any]:
    if ticker == "002361.SZ":
        return {
            "ok": False,
            "ticker": ticker,
            "market": "A",
            "source": "pytest_provider",
            "ts_ms": now_ms() - 7_000,
            "error_code": "PYTEST_PROVIDER_DOWN",
            "reason": "provider down",
        }
    return {
        "ok": True,
        "ticker": ticker,
        "market": "A",
        "name": f"name-{ticker}",
        "price": 100.0 + len(ticker),
        "change_pct": 1.23,
        "source": "pytest_provider",
        "ts_ms": now_ms() - 5_000,
    }


def test_quote_snapshots_refreshes_explicit_candidates_and_positions():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)
    _seed_candidate_pool(conn)
    _seed_position(conn)

    universe = resolve_quote_universe(
        conn,
        tickers=["000001.SZ", "600519.SH"],
        include_positions=True,
        pool_name="p2_quote",
        candidate_limit=10,
    )
    assert universe == ["000001.SZ", "600519.SH", "002361.SZ", "300750.SZ"]

    refreshed = refresh_quote_snapshots(
        conn,
        tickers=["000001.SZ", "600519.SH"],
        include_positions=True,
        pool_name="p2_quote",
        candidate_limit=10,
        provider=_provider,
        source="pytest_provider",
        quote_batch_id="qbatch_pytest_001",
    )

    assert refreshed["quote_batch_id"] == "qbatch_pytest_001"
    assert refreshed["ticker_count"] == 4
    assert refreshed["ok_count"] == 3
    assert refreshed["failed_count"] == 1

    by_ticker = {item["ticker"]: item for item in refreshed["items"]}
    assert by_ticker["000001.SZ"]["status"] == "ok"
    assert by_ticker["000001.SZ"]["price"] > 0
    assert by_ticker["000001.SZ"]["quote_age_ms"] >= 0
    assert by_ticker["002361.SZ"]["status"] == "failed"
    assert by_ticker["002361.SZ"]["error_code"] == "PYTEST_PROVIDER_DOWN"
    assert by_ticker["002361.SZ"]["error_reason"] == "provider down"
    assert by_ticker["300750.SZ"]["request_context"]["include_positions"] is True

    latest = get_latest_quote_snapshots(conn, tickers=["002361.SZ", "300750.SZ"])
    latest_by_ticker = {item["ticker"]: item for item in latest["items"]}
    assert set(latest_by_ticker) == {"002361.SZ", "300750.SZ"}
    assert latest_by_ticker["002361.SZ"]["status"] == "failed"

    batch = get_quote_batch(conn, quote_batch_id="qbatch_pytest_001")
    assert batch["ticker_count"] == 4
    assert batch["ok_count"] == 3
    assert batch["failed_count"] == 1


def test_quote_snapshot_api_routes_use_uniform_envelopes(monkeypatch):
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)
    _seed_candidate_pool(conn)

    original_refresh = api.refresh_quote_snapshots

    def fake_refresh(conn_arg: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
        return original_refresh(conn_arg, provider=_provider, **kwargs)

    monkeypatch.setattr(api, "refresh_quote_snapshots", fake_refresh)

    refreshed = api.handle_request(
        conn,
        cfg=cfg,
        method="POST",
        path="/api/v1/quotes/refresh",
        body={
            "tickers": "000001.SZ",
            "pool_name": "p2_quote",
            "candidate_limit": 1,
            "source": "pytest_provider",
            "quote_batch_id": "qbatch_api_001",
        },
    )
    assert refreshed["ok"] is True
    assert refreshed["data"]["ticker_count"] == 2

    latest = api.handle_request(
        conn,
        cfg=cfg,
        method="GET",
        path="/api/v1/quotes/latest",
        query={"tickers": "000001.SZ,600519.SH", "limit": 10},
    )
    assert latest["ok"] is True
    assert {item["ticker"] for item in latest["data"]["items"]} == {"600519.SH", "000001.SZ"}

    batch = api.handle_request(
        conn,
        cfg=cfg,
        method="GET",
        path="/api/v1/quotes/batch",
        query={"quote_batch_id": "qbatch_api_001"},
    )
    assert batch["ok"] is True
    assert batch["data"]["quote_batch_id"] == "qbatch_api_001"


def test_default_realtime_quote_prefers_direct_http_before_full_basic(monkeypatch):
    calls: list[str] = []

    def fake_direct(ticker: str) -> dict[str, Any]:
        calls.append(f"direct:{ticker}")
        return {
            "ok": True,
            "ticker": ticker,
            "market": "A",
            "name": "fast quote",
            "price": 12.3,
            "change_pct": 1.5,
            "source": "direct_http_pytest",
        }

    def fail_basic(*_: Any, **__: Any) -> dict[str, Any]:
        raise AssertionError("full fetch_basic should not run when direct quote succeeds")

    monkeypatch.setattr(market_snapshot, "_fetch_direct_http_snapshot", fake_direct)
    monkeypatch.setattr(market_snapshot, "fetch_basic", fail_basic)

    snap = market_snapshot._fetch_realtime_snapshot_inline("601778.SH")

    assert snap["ok"] is True
    assert snap["ticker"] == "601778.SH"
    assert snap["price"] == 12.3
    assert snap["source"] == "direct_http_pytest"
    assert calls == ["direct:601778.SH"]


def test_runtime_quote_overlay_can_persist_latest_snapshot_for_dashboard():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    recorded = record_quote_snapshot(
        conn,
        ticker="601778.SH",
        quote_batch_id="qbatch_runtime_001",
        snapshot={
            "ok": True,
            "ticker": "601778.SH",
            "market": "A",
            "name": "晶科科技",
            "price": 6.5,
            "change_pct": -6.61,
            "source": "tencent_qt:sh601778",
        },
        request_context={"loop_id": "loop_runtime_001", "overlay": "realtime_quote"},
    )

    assert recorded["status"] == "ok"
    assert recorded["price"] == 6.5
    latest = get_latest_quote_snapshots(conn, tickers=["601778.SH"])
    item = latest["items"][0]
    assert item["ticker"] == "601778.SH"
    assert item["status"] == "ok"
    assert item["source"] == "tencent_qt:sh601778"
    assert item["request_context"]["overlay"] == "realtime_quote"


def test_default_quote_provider_timeout_records_failed_snapshot(monkeypatch):
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    def slow_inline(ticker: str) -> dict[str, Any]:
        time.sleep(2.0)
        return {
            "ok": True,
            "ticker": ticker,
            "market": "A",
            "price": 10.0,
            "source": "slow_pytest_provider",
        }

    monkeypatch.setattr(market_snapshot, "_fetch_realtime_snapshot_inline", slow_inline)

    started = time.monotonic()
    refreshed = refresh_quote_snapshots(
        conn,
        tickers=["000001.SZ"],
        source="pytest_timeout",
        quote_batch_id="qbatch_timeout_001",
        provider_timeout_seconds=0.05,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert refreshed["ticker_count"] == 1
    assert refreshed["ok_count"] == 0
    assert refreshed["failed_count"] == 1
    item = refreshed["items"][0]
    assert item["ticker"] == "000001.SZ"
    assert item["status"] == "failed"
    assert item["error_code"] == "QUOTE_PROVIDER_TIMEOUT"
    assert "timed out" in item["error_reason"]
    assert item["request_context"]["provider_timeout_seconds"] == 0.05
