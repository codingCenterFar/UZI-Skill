from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.candidate_generator import generate_candidate_pool  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db, record_signal  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_cache(root: Path, ticker: str) -> None:
    base = root / ticker
    _write_json(
        base / "synthesis.json",
        {
            "overall_score": 78.0,
            "verdict_label": "候选观察",
            "agent_reviewed": True,
            "short_trading": {"setup_score": 82.0, "bias": "偏多"},
        },
    )
    _write_json(
        base / "panel.json",
        {
            "panel_consensus": 72.0,
            "signal_distribution": {"bullish": 20, "neutral": 20, "bearish": 8, "skip": 3},
        },
    )
    _write_json(
        base / "strategy_signals.json",
        {
            "signals": [
                {"strategy_id": "trend", "signal": "bullish"},
                {"strategy_id": "quality", "signal": "bullish"},
                {"strategy_id": "risk", "signal": "neutral"},
            ]
        },
    )
    _write_json(
        base / "raw_data.json",
        {"dimensions": {"0_basic": {"data": {"name": "宁德时代", "price": 188.8}}}},
    )


def _seed_signal(conn: sqlite3.Connection) -> int:
    return record_signal(
        conn,
        run_id="run_recent_signal",
        as_of_date="2026-04-26",
        ticker="600519.SH",
        market="A",
        panel_mode="investor_panel",
        action="PAPER_WATCH_B",
        score_final=68.0,
        score_strategy=66.0,
        score_panel=70.0,
        score_tactical=64.0,
        score_core=72.0,
        bonus_agent=4.0,
        penalties=[],
        gates=[],
        summary={"agent_reviewed": True, "short_bias": "中性", "verdict_label": "观察"},
    )


def _seed_watchlist(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO watchlists (
            watchlist_id, name, is_default, symbols_json, status, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, 'active', ?, ?)
        """,
        ("wl_manual", "Manual", 1, json.dumps(["002361.SZ", "600519.SH"]), 1_000, 1_000),
    )


def test_candidate_generator_combines_recent_signals_watchlist_and_cache(tmp_path: Path):
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)
    signal_id = _seed_signal(conn)
    _seed_watchlist(conn)
    cache_root = tmp_path / "cache"
    _seed_cache(cache_root, "300750.SZ")

    generated = generate_candidate_pool(
        conn,
        cfg=cfg,
        pool_name="p2_generated",
        watchlist_id="wl_manual",
        cache_roots=[cache_root],
        max_candidates=10,
        as_of_date="2026-04-26",
    )

    items = {item["ticker"]: item for item in generated["items"]}
    assert set(items) == {"300750.SZ", "600519.SH", "002361.SZ"}
    assert items["300750.SZ"]["candidate_score"] > items["600519.SH"]["candidate_score"]
    assert "cache" in items["300750.SZ"]["source_tags"]
    assert "cache_synthesis" in items["300750.SZ"]["reasons"]
    assert items["600519.SH"]["signal_id"] == signal_id
    assert {"recent_signal", "manual_watchlist"}.issubset(set(items["600519.SH"]["source_tags"]))
    assert items["002361.SZ"]["candidate_score"] == 50.0
    assert items["002361.SZ"]["action_state"] == "PULLBACK_WAIT"
    assert generated["metadata"]["source_counts"]["cache"] == 1
    assert generated["metadata"]["source_counts"]["manual_watchlist"] == 2


def test_candidate_generator_filters_synthetic_cache_tickers(tmp_path: Path):
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)
    cache_root = tmp_path / "cache"
    _seed_cache(cache_root, "300750.SZ")
    _seed_cache(cache_root, "MOCK.SZ")

    generated = generate_candidate_pool(
        conn,
        cfg=cfg,
        pool_name="p2_generated",
        watchlist_id="wl_missing",
        cache_roots=[cache_root],
        max_candidates=10,
        as_of_date="2026-04-26",
    )

    assert {item["ticker"] for item in generated["items"]} == {"300750.SZ"}
    assert generated["metadata"]["source_counts"]["cache"] == 1


def test_candidate_generator_api_route_archives_previous_batch(tmp_path: Path):
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=1_000_000.0)
    _seed_signal(conn)
    cache_root = tmp_path / "cache"
    _seed_cache(cache_root, "300750.SZ")

    first = api.handle_request(
        conn,
        cfg=cfg,
        method="POST",
        path="/api/v1/candidate-pool/generate",
        body={
            "pool_name": "api_generated",
            "watchlist_id": "wl_missing",
            "cache_roots": [str(cache_root)],
            "max_candidates": 5,
            "as_of_date": "2026-04-26",
        },
    )
    second = api.handle_request(
        conn,
        cfg=cfg,
        method="POST",
        path="/api/v1/candidate-pool/generate",
        body={
            "pool_name": "api_generated",
            "watchlist_id": "wl_missing",
            "cache_roots": [str(cache_root)],
            "max_candidates": 5,
            "as_of_date": "2026-04-26",
        },
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["data"]["candidate_batch_id"] != second["data"]["candidate_batch_id"]

    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n
        FROM candidate_pool
        WHERE pool_name='api_generated'
        GROUP BY status
        """
    ).fetchall()
    counts = {row["status"]: int(row["n"]) for row in rows}
    assert counts == {"active": 1, "archived": 1}

    read = api.get_candidate_pool(conn, pool_name="api_generated", limit=10)
    assert read["ok"] is True
    assert read["data"]["status"] == "active"
    assert {item["ticker"] for item in read["data"]["items"]} == {"300750.SZ", "600519.SH"}
