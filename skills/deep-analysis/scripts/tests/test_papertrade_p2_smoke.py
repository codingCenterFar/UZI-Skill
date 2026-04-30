from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.api_common import now_ms  # noqa: E402
from papertrade.candidate_pool import create_candidate_pool_batch  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.p2_smoke import run_p2_smoke  # noqa: E402
from papertrade.quote_snapshots import refresh_quote_snapshots  # noqa: E402
from papertrade.session_scheduler import build_session_plan  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_candidate_and_quotes(conn: sqlite3.Connection) -> None:
    create_candidate_pool_batch(
        conn,
        candidate_batch_id="cpb_smoke_001",
        pool_name="default",
        source="pytest",
        as_of_date="2026-04-26",
        generated_at_ms=1_000,
        items=[
            {
                "ticker": "600519.SH",
                "name": "Moutai",
                "candidate_score": 88.0,
                "action_state": "CANDIDATE_A",
                "reasons": ["pytest_candidate"],
                "analysis_age_ms": 1_200,
            },
            {
                "ticker": "002361.SZ",
                "name": "Shenjian",
                "candidate_score": 63.0,
                "action_state": "PULLBACK_WAIT",
                "reasons": ["wait_pullback"],
                "analysis_age_ms": 2_400,
            },
        ],
    )

    def provider(ticker: str) -> dict[str, Any]:
        return {
            "ok": True,
            "ticker": ticker,
            "market": "A",
            "name": f"name-{ticker}",
            "price": 100.0,
            "change_pct": 0.5,
            "source": "pytest_provider",
            "ts_ms": now_ms() - 1_000,
        }

    refresh_quote_snapshots(
        conn,
        pool_name="default",
        provider=provider,
        source="pytest_provider",
        quote_batch_id="qbatch_smoke_001",
    )
    conn.commit()


def test_p2_smoke_covers_candidate_quote_session_and_action_surfaces(tmp_path: Path):
    conn = _conn()
    cfg = load_config(
        overrides={
            "db_path": str(tmp_path / "paper.db"),
            "run_logs_jsonl": str(tmp_path / "run_logs.jsonl"),
        }
    )
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_candidate_and_quotes(conn)

    session_plan = build_session_plan(refresh_mode={"mode": "quote_only"})
    assert session_plan["scheduler_phase"]

    result = run_p2_smoke(
        conn,
        cfg=cfg,
        candidate_limit=5,
        write_log=True,
        db_label="pytest-memory",
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["feature_checks"]["schema_tables_ok"] is True
    assert result["feature_checks"]["candidate_api_ok"] is True
    assert result["feature_checks"]["quote_api_ok"] is True
    assert result["feature_checks"]["session_api_ok"] is True
    assert result["candidate"]["available"] is True
    assert result["candidate"]["item_count"] == 2
    assert result["quotes"]["item_count"] == 2
    assert result["quotes"]["failed_count"] == 0
    assert set(result["action_states"]["candidate_pool_items"]) == {"CANDIDATE_A", "PULLBACK_WAIT"}
    assert result["tables"]["candidate_pool_items"] == 2

    lines = (tmp_path / "run_logs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    logged = json.loads(lines[0])
    assert logged["kind"] == "p2_smoke"
    assert logged["smoke_id"] == result["smoke_id"]
    assert logged["log"]["written"] is True


def test_p2_smoke_accepts_empty_candidate_pool_as_warning(tmp_path: Path):
    conn = _conn()
    cfg = load_config(
        overrides={
            "db_path": str(tmp_path / "paper.db"),
            "run_logs_jsonl": str(tmp_path / "run_logs.jsonl"),
        }
    )
    init_db(conn, initial_cash=cfg.trade.initial_cash)

    result = run_p2_smoke(conn, cfg=cfg, write_log=False, db_label="pytest-empty")

    assert result["ok"] is True
    assert result["candidate"]["available"] is False
    assert result["checks"]["candidate_pool"]["accepted"] is True
    assert result["checks"]["candidate_pool"]["error_code"] == "NOT_FOUND"
    assert "candidate_pool_missing_or_empty" in result["warnings"]
    assert not (tmp_path / "run_logs.jsonl").exists()
