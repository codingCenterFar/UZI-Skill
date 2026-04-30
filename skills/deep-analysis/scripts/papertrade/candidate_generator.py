from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from lib.market_router import parse_ticker

from papertrade.api_common import now_ms
from papertrade.candidate_pool import DEFAULT_POOL_NAME, create_candidate_pool_batch
from papertrade.config import PaperTradeConfig
from papertrade.decision_policy import (
    ACTION_AVOID,
    ACTION_BREAKOUT_WAIT,
    ACTION_CANDIDATE_A,
    ACTION_NO_TRADE_AVOID,
    ACTION_PAPER_BUY_A,
    ACTION_PAPER_WATCH_B,
    ACTION_PULLBACK_WAIT,
)


HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
GENERATOR_VERSION = "candidate-generator-mvp-v1"


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        data = json.loads(raw)
    except Exception:
        return default
    return data


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


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


def _age_ms(value: Any, *, now: int) -> int:
    ts = _parse_ts_ms(value)
    return max(0, now - ts) if ts is not None else 0


def _ticker(value: Any) -> tuple[str, str]:
    parsed = parse_ticker(str(value or ""))
    return str(parsed.full or value).upper(), str(parsed.market or "").upper()


def _is_real_candidate_ticker(ticker: Any) -> bool:
    text = str(ticker or "").strip().upper()
    if not text:
        return False
    symbol = text.split(".", 1)[0]
    if symbol in {"MOCK", "TEST", "DUMMY"} or symbol.startswith(("MOCK_", "TEST_", "DUMMY_")):
        return False
    return True


def _unique_append(target: list[str], *values: Any) -> None:
    existing = set(target)
    for value in values:
        text = str(value or "").strip()
        if text and text not in existing:
            target.append(text)
            existing.add(text)


def _score_from_strategy(strategy_doc: dict[str, Any]) -> tuple[float | None, dict[str, int]]:
    signals = strategy_doc.get("signals") if isinstance(strategy_doc, dict) else None
    if not isinstance(signals, list):
        return None, {"bullish": 0, "bearish": 0, "neutral": 0, "skip": 0}
    dist = {"bullish": 0, "bearish": 0, "neutral": 0, "skip": 0}
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        k = str(sig.get("signal") or "neutral").lower()
        dist[k if k in dist else "neutral"] += 1
    edge = max(-20.0, min(20.0, (dist["bullish"] - dist["bearish"]) * 4.0))
    return 50.0 + edge, dist


def _extract_short_trading(synthesis: dict[str, Any]) -> dict[str, Any]:
    st = synthesis.get("short_trading")
    if isinstance(st, dict):
        return st
    friendly = synthesis.get("friendly")
    if isinstance(friendly, dict) and isinstance(friendly.get("short_trading"), dict):
        return friendly["short_trading"]
    return {}


def _candidate_action_state(
    action: str | None,
    score: float,
    source_tags: list[str],
    action_layer: dict[str, Any] | None = None,
) -> str:
    action_u = str(action or "").strip().upper()
    if action_u == ACTION_AVOID:
        if isinstance(action_layer, dict) and action_layer.get("reason") == "hard_bearish_below_force_exit":
            return ACTION_AVOID
        return ACTION_NO_TRADE_AVOID
    if action_u in {
        ACTION_PAPER_BUY_A,
        ACTION_PAPER_WATCH_B,
        ACTION_CANDIDATE_A,
        ACTION_PULLBACK_WAIT,
        ACTION_BREAKOUT_WAIT,
        ACTION_NO_TRADE_AVOID,
    }:
        return action_u
    if action_u == "OBSERVE":
        return ACTION_CANDIDATE_A
    if score >= 75.0:
        return ACTION_CANDIDATE_A
    if score >= 65.0:
        return ACTION_PAPER_WATCH_B
    if score >= 45.0 or any(tag in {"manual_watchlist", "cache", "recent_signal"} for tag in source_tags):
        return ACTION_PULLBACK_WAIT
    return ACTION_NO_TRADE_AVOID


def _normalise_cache_roots(cfg: PaperTradeConfig, cache_roots: list[str | Path] | None) -> list[Path]:
    raw_roots: list[str | Path] = list(cache_roots or [])
    if not raw_roots:
        raw_roots = [cfg.cache_root, SCRIPTS_DIR / ".cache"]
    roots: list[Path] = []
    seen: set[str] = set()
    for root in raw_roots:
        path = Path(root)
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            roots.append(path)
            seen.add(key)
    return roots


def _cache_paths_for_ticker(roots: list[Path], ticker: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for root in roots:
        base = root / ticker
        if not base.exists():
            continue
        for name in ("synthesis", "panel", "strategy_signals", "raw_data"):
            path = base / f"{name}.json"
            if path.exists() and name not in paths:
                paths[name] = path
    return paths


def _discover_cached_tickers(roots: list[Path], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if len(out) >= limit:
                return out
            if not child.is_dir() or child.name in {"_global", "paper_trade", "api_cache"}:
                continue
            if not any((child / f"{name}.json").exists() for name in ("synthesis", "panel", "strategy_signals", "raw_data")):
                continue
            ticker, _ = _ticker(child.name)
            if not _is_real_candidate_ticker(ticker):
                continue
            if ticker not in seen:
                out.append(ticker)
                seen.add(ticker)
    return out


def _signal_candidates(conn: sqlite3.Connection, *, limit: int, now: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*
        FROM signals s
        JOIN (
            SELECT ticker, MAX(id) AS max_id
            FROM signals
            GROUP BY ticker
        ) latest ON latest.max_id = s.id
        ORDER BY s.id DESC
        LIMIT ?
        """,
        (max(1, min(500, int(limit))),),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticker, market = _ticker(row["ticker"])
        if not _is_real_candidate_ticker(ticker):
            continue
        summary = _loads(row["summary_json"], {})
        score = _f(row["score_final"], 0.0)
        reasons = [f"latest_signal:{row['action']}"]
        if summary.get("agent_reviewed"):
            reasons.append("agent_reviewed")
        if summary.get("short_bias"):
            reasons.append(f"short_bias:{summary.get('short_bias')}")
        out[ticker] = {
            "ticker": ticker,
            "market": str(row["market"] or market or "").upper() or market,
            "candidate_score": score,
            "action": row["action"],
            "reasons": reasons,
            "source_tags": ["recent_signal"],
            "staleness": {"signal_age_ms": _age_ms(row["created_at"], now=now)},
            "analysis_age_ms": _age_ms(row["created_at"], now=now),
            "signal_id": int(row["id"]),
            "action_layer": summary.get("action_layer") if isinstance(summary, dict) else None,
            "metadata": {
                "run_id": row["run_id"],
                "panel_mode": row["panel_mode"],
                "summary": summary,
            },
        }
    return out


def _watchlist_symbols(conn: sqlite3.Connection, *, watchlist_id: str | None) -> list[str]:
    wl_id = str(watchlist_id or "wl_default")
    rows = conn.execute(
        """
        SELECT *
        FROM watchlists
        WHERE status='active' AND (?='' OR watchlist_id=?)
        ORDER BY is_default DESC, updated_at_ms DESC
        """,
        ("" if watchlist_id is None else wl_id, wl_id),
    ).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        symbols = _loads(row["symbols_json"], [])
        for symbol in symbols if isinstance(symbols, list) else []:
            ticker, _ = _ticker(symbol)
            if not _is_real_candidate_ticker(ticker):
                continue
            if ticker not in seen:
                out.append(ticker)
                seen.add(ticker)
    return out


def _cache_candidate(ticker: str, *, roots: list[Path], now: int) -> dict[str, Any] | None:
    paths = _cache_paths_for_ticker(roots, ticker)
    if not paths:
        return None

    synthesis = _read_json(paths.get("synthesis", Path())) or {}
    panel = _read_json(paths.get("panel", Path())) or {}
    strategy = _read_json(paths.get("strategy_signals", Path())) or {}
    raw = _read_json(paths.get("raw_data", Path())) or {}

    score_parts: list[tuple[float, float]] = []
    overall = _f(synthesis.get("overall_score"), 0.0)
    if overall > 0:
        score_parts.append((overall, 0.40))
    panel_consensus = _f(panel.get("panel_consensus"), 0.0)
    if panel_consensus > 0:
        score_parts.append((panel_consensus, 0.25))
    short_trading = _extract_short_trading(synthesis)
    setup_score = _f(short_trading.get("setup_score"), 0.0)
    if setup_score > 0:
        score_parts.append((setup_score, 0.20))
    strategy_score, strategy_dist = _score_from_strategy(strategy)
    if strategy_score is not None:
        score_parts.append((strategy_score, 0.15))

    if not score_parts:
        return None

    weight_sum = sum(w for _, w in score_parts) or 1.0
    score = sum(v * w for v, w in score_parts) / weight_sum
    mtimes = [int(path.stat().st_mtime * 1000) for path in paths.values() if path.exists()]
    analysis_age = max(0, now - max(mtimes)) if mtimes else 0

    basic = (((raw.get("dimensions") or {}).get("0_basic") or {}).get("data") or {})
    ticker_norm, market = _ticker(ticker)
    reasons = ["cache_synthesis"]
    if synthesis.get("agent_reviewed"):
        reasons.append("agent_reviewed")
    if short_trading.get("bias"):
        reasons.append(f"short_bias:{short_trading.get('bias')}")
    if any(strategy_dist.values()):
        reasons.append(f"strategy_bull_bear:{strategy_dist['bullish']}/{strategy_dist['bearish']}")

    return {
        "ticker": ticker_norm,
        "market": market,
        "name": basic.get("name"),
        "candidate_score": round(score, 4),
        "action": None,
        "reasons": reasons,
        "source_tags": ["cache"],
        "staleness": {
            "analysis_age_ms": analysis_age,
            "cache_files": sorted(paths.keys()),
        },
        "analysis_age_ms": analysis_age,
        "metadata": {
            "verdict_label": synthesis.get("verdict_label"),
            "overall_score": overall,
            "panel_consensus": panel_consensus,
            "setup_score": setup_score,
            "strategy_dist": strategy_dist,
        },
    }


def _merge_candidate(target: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    if not target:
        target = {
            "ticker": incoming["ticker"],
            "market": incoming.get("market"),
            "name": incoming.get("name"),
            "candidate_score": _f(incoming.get("candidate_score")),
            "action": incoming.get("action"),
            "reasons": [],
            "source_tags": [],
            "staleness": {},
            "analysis_age_ms": _i(incoming.get("analysis_age_ms")),
            "quote_age_ms": _i(incoming.get("quote_age_ms")),
            "signal_id": incoming.get("signal_id"),
            "action_layer": incoming.get("action_layer"),
            "metadata": {},
        }

    if _f(incoming.get("candidate_score")) > _f(target.get("candidate_score")):
        target["candidate_score"] = _f(incoming.get("candidate_score"))
        target["action"] = incoming.get("action") or target.get("action")
    target["name"] = target.get("name") or incoming.get("name")
    target["market"] = target.get("market") or incoming.get("market")
    target["signal_id"] = target.get("signal_id") or incoming.get("signal_id")
    target["action_layer"] = target.get("action_layer") or incoming.get("action_layer")
    _unique_append(target["reasons"], *incoming.get("reasons", []))
    _unique_append(target["source_tags"], *incoming.get("source_tags", []))
    target["staleness"].update(incoming.get("staleness") or {})
    ages = [v for v in (_i(target.get("analysis_age_ms")), _i(incoming.get("analysis_age_ms"))) if v > 0]
    target["analysis_age_ms"] = min(ages) if ages else 0
    target["quote_age_ms"] = max(_i(target.get("quote_age_ms")), _i(incoming.get("quote_age_ms")))
    target["metadata"].update(incoming.get("metadata") or {})
    return target


def generate_candidate_pool(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    pool_name: str | None = None,
    watchlist_id: str | None = "wl_default",
    cache_roots: list[str | Path] | None = None,
    recent_signal_limit: int = 100,
    cache_scan_limit: int = 200,
    max_candidates: int = 50,
    as_of_date: str | None = None,
    archive_previous: bool = True,
) -> dict[str, Any]:
    now = now_ms()
    roots = _normalise_cache_roots(cfg, cache_roots)
    by_ticker: dict[str, dict[str, Any]] = {}

    for ticker, item in _signal_candidates(conn, limit=recent_signal_limit, now=now).items():
        by_ticker[ticker] = _merge_candidate(by_ticker.get(ticker, {}), item)

    manual_symbols = _watchlist_symbols(conn, watchlist_id=watchlist_id)
    for ticker in manual_symbols:
        ticker_norm, market = _ticker(ticker)
        by_ticker[ticker_norm] = _merge_candidate(
            by_ticker.get(ticker_norm, {}),
            {
                "ticker": ticker_norm,
                "market": market,
                "candidate_score": 50.0,
                "action": None,
                "reasons": ["manual_watchlist"],
                "source_tags": ["manual_watchlist"],
                "staleness": {"watchlist_id": watchlist_id or "all_active"},
                "metadata": {"watchlist_id": watchlist_id or "all_active"},
            },
        )

    cache_tickers = _discover_cached_tickers(roots, limit=cache_scan_limit)
    for ticker in sorted(set(cache_tickers + manual_symbols + list(by_ticker.keys()))):
        cached = _cache_candidate(ticker, roots=roots, now=now)
        if cached:
            by_ticker[cached["ticker"]] = _merge_candidate(by_ticker.get(cached["ticker"], {}), cached)

    prepared: list[dict[str, Any]] = []
    for item in by_ticker.values():
        if not _is_real_candidate_ticker(item.get("ticker")):
            continue
        score = round(_f(item.get("candidate_score")), 4)
        source_tags = list(item.get("source_tags") or [])
        prepared.append(
            {
                "ticker": item["ticker"],
                "market": item.get("market"),
                "name": item.get("name"),
                "candidate_score": score,
                "action_state": _candidate_action_state(
                    item.get("action"),
                    score,
                    source_tags,
                    action_layer=item.get("action_layer"),
                ),
                "reasons": item.get("reasons") or [],
                "source_tags": source_tags,
                "staleness": item.get("staleness") or {},
                "analysis_age_ms": _i(item.get("analysis_age_ms")),
                "quote_age_ms": _i(item.get("quote_age_ms")),
                "signal_id": item.get("signal_id"),
                "metadata": {
                    "generator_version": GENERATOR_VERSION,
                    **(item.get("metadata") or {}),
                },
            }
        )

    prepared.sort(key=lambda item: (-_f(item.get("candidate_score")), str(item.get("ticker") or "")))
    limited = prepared[: max(1, min(500, int(max_candidates)))]
    for idx, item in enumerate(limited, start=1):
        item["rank"] = idx

    return create_candidate_pool_batch(
        conn,
        items=limited,
        pool_name=pool_name or DEFAULT_POOL_NAME,
        source=GENERATOR_VERSION,
        as_of_date=as_of_date or date.today().isoformat(),
        universe=[item["ticker"] for item in limited],
        metadata={
            "generator_version": GENERATOR_VERSION,
            "watchlist_id": watchlist_id,
            "cache_roots": [str(root) for root in roots],
            "recent_signal_limit": int(recent_signal_limit),
            "cache_scan_limit": int(cache_scan_limit),
            "max_candidates": int(max_candidates),
            "source_counts": {
                "recent_signal": sum(1 for item in prepared if "recent_signal" in item.get("source_tags", [])),
                "manual_watchlist": sum(1 for item in prepared if "manual_watchlist" in item.get("source_tags", [])),
                "cache": sum(1 for item in prepared if "cache" in item.get("source_tags", [])),
            },
        },
        archive_previous=archive_previous,
        generated_at_ms=now,
    )
