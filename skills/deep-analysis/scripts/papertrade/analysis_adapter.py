from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.cache import read_task_output  # noqa: E402
from lib.market_router import parse_ticker  # noqa: E402
from run_real_test import stage1, stage2  # noqa: E402


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_task_output_tiered(ticker: str, task_name: str) -> dict[str, Any] | None:
    data = read_task_output(ticker, task_name)
    if isinstance(data, dict) and data:
        return data

    p = SCRIPTS_DIR / ".cache" / ticker / f"{task_name}.json"
    return _read_json(p)


def _read_review_issues_tiered(ticker: str) -> dict[str, Any] | None:
    candidates = [
        Path(".cache") / ticker / "_review_issues.json",
        SCRIPTS_DIR / ".cache" / ticker / "_review_issues.json",
    ]
    for p in candidates:
        out = _read_json(p)
        if isinstance(out, dict):
            return out
    return None


def _read_cached_bundle(ticker: str) -> dict[str, Any]:
    ti = parse_ticker(ticker)
    raw = _read_task_output_tiered(ti.full, "raw_data") or {}
    dims = _read_task_output_tiered(ti.full, "dimensions") or {}
    panel = _read_task_output_tiered(ti.full, "panel") or {}
    strategy_signals = _read_task_output_tiered(ti.full, "strategy_signals") or {}
    synthesis = _read_task_output_tiered(ti.full, "synthesis") or {}
    review_issues = _read_review_issues_tiered(ti.full)
    if not (raw and dims and panel and synthesis):
        return {
            "status": "cache_missing",
            "ticker": ti.full,
            "market": ti.market,
            "missing": [
                k
                for k, v in {
                    "raw_data": raw,
                    "dimensions": dims,
                    "panel": panel,
                    "synthesis": synthesis,
                }.items()
                if not v
            ],
        }
    return {
        "status": "ok",
        "ticker": ti.full,
        "market": ti.market,
        "report_path": "",
        "raw": raw,
        "dimensions": dims,
        "panel": panel,
        "strategy_signals": strategy_signals,
        "synthesis": synthesis,
        "review_issues": review_issues,
    }


def analyze_ticker(
    ticker: str,
    depth: str = "medium",
    no_resume: bool = False,
    from_cache_only: bool = False,
) -> dict[str, Any]:
    if from_cache_only:
        return _read_cached_bundle(ticker)

    os.environ["UZI_DEPTH"] = depth
    os.environ.setdefault("UZI_NO_AUTO_OPEN", "1")
    os.environ.setdefault("UZI_CLI_ONLY", "1")

    if no_resume:
        os.environ["UZI_NO_RESUME"] = "1"

    s1 = stage1(ticker)

    if isinstance(s1, dict) and s1.get("status") in ("name_not_resolved", "non_stock_security"):
        return {
            "status": s1.get("status"),
            "ticker": ticker,
            "stage1": s1,
        }

    resolved = ticker
    if isinstance(s1, dict) and s1.get("ticker"):
        resolved = str(s1.get("ticker"))

    report_path = stage2(resolved)

    ti = parse_ticker(resolved)
    raw = _read_task_output_tiered(ti.full, "raw_data") or {}
    dims = _read_task_output_tiered(ti.full, "dimensions") or {}
    panel = _read_task_output_tiered(ti.full, "panel") or {}
    strategy_signals = _read_task_output_tiered(ti.full, "strategy_signals") or {}
    synthesis = _read_task_output_tiered(ti.full, "synthesis") or {}
    review_issues = _read_review_issues_tiered(ti.full)

    return {
        "status": "ok",
        "ticker": ti.full,
        "market": ti.market,
        "report_path": report_path,
        "raw": raw,
        "dimensions": dims,
        "panel": panel,
        "strategy_signals": strategy_signals,
        "synthesis": synthesis,
        "review_issues": review_issues,
    }
