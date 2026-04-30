from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from papertrade.api_common import new_id, now_ms
from papertrade.metrics import query_runtime_metrics
from papertrade.outbox import enqueue_outbox_event


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


def _latest_nav(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT cash, position_market_value, equity, cumulative_return_pct, drawdown_pct,
               cash_drift_check, equity_recompute_diff, as_of_ts_ms AS ts_ms
        FROM portfolio_nav_snapshots
        ORDER BY as_of_ts_ms DESC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        """
        SELECT cash, position_market_value, equity, cumulative_return_pct, drawdown_pct,
               0 AS cash_drift_check, 0 AS equity_recompute_diff, NULL AS ts_ms
        FROM paper_nav_daily
        ORDER BY as_of_date DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def check_portfolio_consistency(
    conn: sqlite3.Connection,
    *,
    threshold: float = 0.01,
    emit_alert: bool = True,
) -> dict[str, Any]:
    cash_row = conn.execute("SELECT cash FROM account_state WHERE id=1").fetchone()
    cash_actual = _f(cash_row["cash"] if cash_row else 0.0)

    mv_row = conn.execute("SELECT COALESCE(SUM(market_value), 0) AS mv FROM positions WHERE qty > 0").fetchone()
    mv_actual = _f(mv_row["mv"] if mv_row else 0.0)
    if mv_actual <= 0:
        legacy_mv = conn.execute("SELECT COALESCE(SUM(market_value), 0) AS mv FROM paper_positions WHERE quantity > 0").fetchone()
        mv_actual = _f(legacy_mv["mv"] if legacy_mv else 0.0)

    nav = _latest_nav(conn)
    nav_cash = _f(nav.get("cash") if nav else cash_actual)
    nav_mv = _f(nav.get("position_market_value") if nav else mv_actual)
    nav_equity = _f(nav.get("equity") if nav else cash_actual + mv_actual)

    cash_drift_check = round(cash_actual - nav_cash, 6)
    equity_recompute_diff = round((cash_actual + mv_actual) - nav_equity, 6)
    mv_diff = round(mv_actual - nav_mv, 6)
    ok = abs(cash_drift_check) <= float(threshold) and abs(equity_recompute_diff) <= float(threshold)

    result = {
        "ok": ok,
        "cash_actual": round(cash_actual, 6),
        "cash_nav": round(nav_cash, 6),
        "position_market_value_actual": round(mv_actual, 6),
        "position_market_value_nav": round(nav_mv, 6),
        "equity_actual": round(cash_actual + mv_actual, 6),
        "equity_nav": round(nav_equity, 6),
        "cash_drift_check": cash_drift_check,
        "position_market_value_diff": mv_diff,
        "equity_recompute_diff": equity_recompute_diff,
        "threshold": float(threshold),
    }

    if emit_alert and not ok:
        _raise_health_alert(
            conn,
            code="EQUITY_RECOMPUTE_DIFF",
            title="Papertrade equity consistency drift",
            details=result,
            severity="warning",
        )
    return result


def _raise_health_alert(
    conn: sqlite3.Connection,
    *,
    code: str,
    title: str,
    details: dict[str, Any],
    severity: str = "warning",
) -> str:
    corr = new_id("crr")
    as_of_date = date.today().isoformat()
    conn.execute(
        """
        INSERT INTO alerts (
            run_id, as_of_date, ticker, level, channel, title, body, payload_json, created_at
        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            "health_check",
            as_of_date,
            severity,
            "system",
            title,
            json.dumps(details, ensure_ascii=False),
            json.dumps({"code": code, "details": details}, ensure_ascii=False),
        ),
    )
    return enqueue_outbox_event(
        conn,
        event_type="alert.raised",
        entity_type="health",
        entity_id=code,
        source="health_check",
        severity=severity,
        correlation_id=corr,
        payload={
            "code": code,
            "title": title,
            "details": details,
        },
    )


def check_runtime_health(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
    consistency_threshold: float = 0.01,
    emit_alerts: bool = False,
) -> dict[str, Any]:
    ts = int(now if now is not None else now_ms())
    runtime = conn.execute(
        """
        SELECT *
        FROM runtime_sessions
        ORDER BY updated_at_ms DESC
        LIMIT 1
        """
    ).fetchone()
    lease = conn.execute("SELECT * FROM runtime_leases WHERE lease_name='papertrade_runtime'").fetchone()
    pending = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='pending'").fetchone()
    dead = conn.execute("SELECT COUNT(*) AS n FROM event_outbox WHERE status='dead'").fetchone()

    runtime_data = dict(runtime) if runtime else None
    lease_data = dict(lease) if lease else None
    runtime_status = str((runtime_data or {}).get("status") or "")
    runtime_active = runtime_status in {"starting", "running", "stopping"}
    lease_expired = bool(lease_data and _i(lease_data.get("lease_expires_at_ms")) <= ts)
    dead_count = _i(dead["n"] if dead else 0)
    pending_count = _i(pending["n"] if pending else 0)
    consistency = check_portfolio_consistency(
        conn,
        threshold=consistency_threshold,
        emit_alert=emit_alerts,
    )

    issues: list[dict[str, Any]] = []
    if runtime_active and lease_expired:
        issues.append({"code": "RUNTIME_LEASE_EXPIRED", "severity": "warning", "retryable": True})
    if dead_count > 0:
        issues.append({"code": "EVENT_OUTBOX_DEAD", "severity": "warning", "retryable": True, "dead_count": dead_count})
    if not consistency["ok"]:
        issues.append(
            {
                "code": "EQUITY_RECOMPUTE_DIFF",
                "severity": "warning",
                "retryable": False,
                "equity_recompute_diff": consistency["equity_recompute_diff"],
            }
        )

    status = "ok" if not issues else "degraded"
    if runtime_data and runtime_data.get("status") == "failed":
        status = "failed"

    return {
        "status": status,
        "ts_ms": ts,
        "runtime": runtime_data,
        "lease": {
            **lease_data,
            "expired": lease_expired,
        } if lease_data else None,
        "outbox": {
            "pending": pending_count,
            "dead": dead_count,
        },
        "consistency": consistency,
        "metrics": query_runtime_metrics(conn),
        "issues": issues,
    }
