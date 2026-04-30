from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.api_common import PaperTradeAPIError  # noqa: E402
from papertrade.candidate_pool import (  # noqa: E402
    archive_candidate_pool_batch,
    create_candidate_pool_batch,
    get_candidate_pool_top,
    list_candidate_pool_batches,
)
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _items() -> list[dict]:
    return [
        {
            "ticker": "600519",
            "name": "Kweichow Moutai",
            "candidate_score": 88.5,
            "action_state": "CANDIDATE_A",
            "reasons": ["quality_score_high", "recent_signal_watch"],
            "source_tags": ["cache", "manual_watchlist"],
            "staleness": {"analysis_age_ms": 1200, "quote_age_ms": 300},
            "analysis_age_ms": 1200,
            "quote_age_ms": 300,
            "metadata": {"note": "pytest"},
        },
        {
            "ticker": "002361.SZ",
            "name": "Shenjian",
            "candidate_score": 62.0,
            "action_state": "PULLBACK_WAIT",
            "reasons": ["agent_reviewed", "avoid_but_watch"],
            "source_tags": ["recent_signal"],
            "staleness": {"analysis_age_ms": 2400},
            "analysis_age_ms": 2400,
        },
    ]


def test_candidate_pool_primitives_write_read_topn_and_archive_batch():
    conn = _conn()
    init_db(conn, initial_cash=1_000_000.0)

    created = create_candidate_pool_batch(
        conn,
        candidate_batch_id="cpb_pytest_001",
        pool_name="p2_pytest",
        source="pytest",
        as_of_date="2026-04-26",
        generated_at_ms=1_000,
        universe=["600519", "002361.SZ"],
        metadata={"generator": "unit"},
        items=_items(),
    )
    assert created["candidate_batch_id"] == "cpb_pytest_001"
    assert created["status"] == "active"
    assert created["item_count"] == 2
    assert created["items"][0]["ticker"] == "600519.SH"
    assert created["items"][0]["rank"] == 1
    assert created["items"][0]["candidate_score"] == 88.5
    assert created["items"][0]["reasons"] == ["quality_score_high", "recent_signal_watch"]
    assert created["universe"] == ["600519.SH", "002361.SZ"]

    top_one = get_candidate_pool_top(conn, pool_name="p2_pytest", limit=1)
    assert top_one["item_count"] == 1
    assert top_one["items"][0]["ticker"] == "600519.SH"

    batches = list_candidate_pool_batches(conn, pool_name="p2_pytest")
    assert [b["candidate_batch_id"] for b in batches] == ["cpb_pytest_001"]

    archived = archive_candidate_pool_batch(
        conn,
        candidate_batch_id="cpb_pytest_001",
        reason="pytest_archive",
        archived_at_ms=2_000,
    )
    assert archived["status"] == "archived"
    assert archived["archive_reason"] == "pytest_archive"

    with pytest.raises(PaperTradeAPIError) as exc:
        get_candidate_pool_top(conn, candidate_batch_id="cpb_pytest_001")
    assert exc.value.error_code == "NOT_FOUND"

    archived_read = get_candidate_pool_top(conn, candidate_batch_id="cpb_pytest_001", include_archived=True)
    assert archived_read["status"] == "archived"


def test_candidate_pool_api_returns_uniform_envelopes_and_routes():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)

    created = api.post_candidate_pool(
        conn,
        payload={
            "candidate_batch_id": "cpb_api_001",
            "pool_name": "api_pool",
            "source": "pytest_api",
            "as_of_date": "2026-04-26",
            "items": _items(),
        },
    )
    assert created["ok"] is True
    assert created["data"]["candidate_batch_id"] == "cpb_api_001"

    read = api.get_candidate_pool(conn, pool_name="api_pool", limit=5)
    assert read["ok"] is True
    assert read["data"]["pool_name"] == "api_pool"
    assert read["data"]["items"][1]["action_state"] == "PULLBACK_WAIT"
    assert read["data"]["batches"][0]["candidate_batch_id"] == "cpb_api_001"

    routed = api.handle_request(
        conn,
        cfg=cfg,
        method="GET",
        path="/api/v1/candidate-pool",
        query={"pool_name": "api_pool", "limit": 1},
    )
    assert routed["ok"] is True
    assert routed["data"]["item_count"] == 1

    archived = api.handle_request(
        conn,
        cfg=cfg,
        method="POST",
        path="/api/v1/candidate-pool/archive",
        body={"candidate_batch_id": "cpb_api_001", "reason": "route_archive"},
    )
    assert archived["ok"] is True
    assert archived["data"]["status"] == "archived"

    missing_active = api.get_candidate_pool(conn, pool_name="api_pool")
    assert missing_active["ok"] is False
    assert missing_active["error"]["error_code"] == "NOT_FOUND"
