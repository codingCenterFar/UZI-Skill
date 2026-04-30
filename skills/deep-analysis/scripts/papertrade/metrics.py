from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


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
    if not text:
        return None
    if text.isdigit():
        v = int(text)
        return v if v > 10_000_000_000 else v * 1000
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _freshness_ms(ts: Any, *, now: int | None = None) -> int | None:
    ts_ms = _parse_ts_ms(ts)
    if ts_ms is None:
        return None
    return max(0, int(now if now is not None else now_ms()) - ts_ms)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * float(pct)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def quote_freshness_ms_from_snapshot(snapshot: dict[str, Any] | None, *, now: int | None = None) -> int | None:
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return None
    return _freshness_ms(snapshot.get("ts") or snapshot.get("ts_ms"), now=now)


def analysis_freshness_ms_from_bundle(bundle: dict[str, Any] | None, *, from_cache_only: bool, now: int | None = None) -> int:
    if not isinstance(bundle, dict):
        return 0
    candidates: list[Any] = []
    for section_name in ("synthesis", "panel", "strategy_signals", "raw"):
        section = bundle.get(section_name)
        if isinstance(section, dict):
            candidates.extend(
                [
                    section.get("generated_at_ms"),
                    section.get("as_of_ts_ms"),
                    section.get("updated_at_ms"),
                    section.get("generated_at"),
                    section.get("created_at"),
                    section.get("ts"),
                ]
            )
            meta = section.get("_meta")
            if isinstance(meta, dict):
                candidates.extend(
                    [
                        meta.get("generated_at_ms"),
                        meta.get("as_of_ts_ms"),
                        meta.get("updated_at_ms"),
                        meta.get("generated_at"),
                        meta.get("created_at"),
                        meta.get("ts"),
                    ]
                )
    ages = [_freshness_ms(v, now=now) for v in candidates]
    ages_i = [int(v) for v in ages if v is not None]
    if ages_i:
        return min(ages_i)
    return 0 if not from_cache_only else 1


def build_loop_metrics(
    conn: sqlite3.Connection,
    *,
    summary_rows: list[dict[str, Any]],
    from_cache_only: bool,
    db_write_duration_ms: int,
    event_dispatch: dict[str, Any] | None = None,
    quote_freshness_values: list[int] | None = None,
    analysis_freshness_values: list[int] | None = None,
) -> dict[str, Any]:
    quote_values = [int(v) for v in (quote_freshness_values or []) if v is not None and int(v) >= 0]
    analysis_values = [int(v) for v in (analysis_freshness_values or []) if v is not None and int(v) >= 0]
    pending = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='pending'").fetchone()
    dead = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='dead'").fetchone()
    rule_total = conn.execute("SELECT COUNT(*) AS n FROM order_rule_checks").fetchone()
    rule_rejects = conn.execute("SELECT COUNT(*) AS n FROM order_rule_checks WHERE result='reject'").fetchone()
    manual = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='manual'").fetchone()
    auto = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='auto'").fetchone()
    rule_total_n = _i(rule_total["n"] if rule_total else 0)
    rule_reject_n = _i(rule_rejects["n"] if rule_rejects else 0)

    return {
        "quote_freshness_ms": max(quote_values) if quote_values else 0,
        "analysis_freshness_ms": max(analysis_values) if analysis_values else (1 if from_cache_only else 0),
        "quote_freshness_ms_p95": round(_percentile([float(v) for v in quote_values], 0.95), 3) if quote_values else 0,
        "analysis_freshness_ms_p95": round(_percentile([float(v) for v in analysis_values], 0.95), 3) if analysis_values else (1 if from_cache_only else 0),
        "db_write_duration_ms": int(db_write_duration_ms),
        "event_queue_lag": _i(pending["n"] if pending else 0),
        "event_dead_count": _i(dead["n"] if dead else 0),
        "event_dispatch": event_dispatch or {},
        "rule_reject_rate": round((rule_reject_n / rule_total_n), 6) if rule_total_n else 0.0,
        "manual_order_count": _i(manual["n"] if manual else 0),
        "auto_order_count": _i(auto["n"] if auto else 0),
        "ticker_count": len(summary_rows),
        "skip_count": sum(1 for r in summary_rows if str(r.get("action") or "").upper() == "SKIP"),
        "from_cache_only": bool(from_cache_only),
    }


def query_runtime_metrics(conn: sqlite3.Connection, *, limit: int = 100) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT *
        FROM runtime_loops
        ORDER BY started_at_ms DESC
        LIMIT ?
        """,
        (max(1, min(500, int(limit))),),
    ).fetchall()
    loops = [dict(row) for row in rows]
    metrics_docs = [_loads(row.get("metrics_json"), {}) for row in loops]
    durations = [_f(row.get("duration_ms")) for row in loops if row.get("duration_ms") is not None]
    db_durations = [_f(row.get("db_write_duration_ms")) for row in loops if row.get("db_write_duration_ms") is not None]

    latest = metrics_docs[0] if metrics_docs else {}
    quote_values = [_f(m.get("quote_freshness_ms")) for m in metrics_docs if isinstance(m, dict)]
    analysis_values = [_f(m.get("analysis_freshness_ms")) for m in metrics_docs if isinstance(m, dict)]
    pending = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='pending'").fetchone()
    dead = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='dead'").fetchone()
    manual = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='manual'").fetchone()
    auto = conn.execute("SELECT COUNT(*) AS n FROM order_intents WHERE source='auto'").fetchone()
    rule_total = conn.execute("SELECT COUNT(*) AS n FROM order_rule_checks").fetchone()
    rule_rejects = conn.execute("SELECT COUNT(*) AS n FROM order_rule_checks WHERE result='reject'").fetchone()
    rule_total_n = _i(rule_total["n"] if rule_total else 0)
    rule_reject_n = _i(rule_rejects["n"] if rule_rejects else 0)

    return {
        "quote_freshness_ms": round(max(quote_values), 3) if quote_values else 0,
        "analysis_freshness_ms": round(max(analysis_values), 3) if analysis_values else 0,
        "loop_duration_p50": round(_percentile(durations, 0.50), 3),
        "loop_duration_p95": round(_percentile(durations, 0.95), 3),
        "db_write_duration_p50": round(_percentile(db_durations, 0.50), 3),
        "db_write_duration_p95": round(_percentile(db_durations, 0.95), 3),
        "event_queue_lag": _i(pending["n"] if pending else 0),
        "event_dead_count": _i(dead["n"] if dead else 0),
        "rule_reject_rate": round((rule_reject_n / rule_total_n), 6) if rule_total_n else 0.0,
        "manual_vs_auto_order_count": {
            "manual": _i(manual["n"] if manual else 0),
            "auto": _i(auto["n"] if auto else 0),
        },
        "loop_count": len(loops),
        "latest_loop": {
            "loop_id": loops[0].get("loop_id"),
            "status": loops[0].get("status"),
            "metrics": latest if isinstance(latest, dict) else {},
        } if loops else None,
    }
