from __future__ import annotations

import json
from typing import Any


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _paper_daily_return_pct(conn, as_of_date: str) -> float:
    cur = conn.execute(
        "SELECT as_of_date, equity FROM paper_nav_daily WHERE as_of_date <= ? ORDER BY as_of_date DESC LIMIT 2",
        (as_of_date,),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return 0.0
    e0 = _f(rows[0]["equity"])
    e1 = _f(rows[1]["equity"])
    if e1 <= 0:
        return 0.0
    return (e0 / e1 - 1.0) * 100.0


def update_deviation_daily(conn, as_of_date: str, live_return_pct: float | None = None) -> dict[str, float]:
    paper_ret = _paper_daily_return_pct(conn, as_of_date)

    paper_trades = conn.execute(
        "SELECT ticker, side, price, qty FROM paper_fills WHERE as_of_date=?",
        (as_of_date,),
    ).fetchall()
    live_trades = conn.execute(
        "SELECT ticker, side, price, qty FROM live_mirror_trades WHERE as_of_date=?",
        (as_of_date,),
    ).fetchall()

    paper_map: dict[tuple[str, str], list[float]] = {}
    for r in paper_trades:
        k = (str(r["ticker"]), str(r["side"]).upper())
        paper_map.setdefault(k, []).append(_f(r["price"]))

    slippages = []
    matched = 0
    for r in live_trades:
        k = (str(r["ticker"]), str(r["side"]).upper())
        candidates = paper_map.get(k) or []
        if not candidates:
            continue
        paper_px = candidates[0]
        live_px = _f(r["price"])
        if paper_px <= 0 or live_px <= 0:
            continue
        if k[1] == "BUY":
            s = (live_px / paper_px - 1.0) * 100.0
        else:
            s = (paper_px / live_px - 1.0) * 100.0
        slippages.append(s)
        matched += 1

    avg_slip = sum(slippages) / len(slippages) if slippages else 0.0
    live_ret = _f(live_return_pct, 0.0)
    gap = live_ret - paper_ret

    details = {
        "paper_trade_count": len(paper_trades),
        "live_trade_count": len(live_trades),
        "matched_trade_count": matched,
        "slippage_samples_pct": [round(x, 4) for x in slippages[:50]],
        "note": "live_return_pct 未提供时默认 0，仅用于偏差框架占位",
    }

    conn.execute(
        """
        INSERT INTO deviation_daily (
            as_of_date, paper_return_pct, live_return_pct, return_gap_pct,
            avg_entry_slippage_pct, trade_count, updated_at, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(as_of_date) DO UPDATE SET
            paper_return_pct=excluded.paper_return_pct,
            live_return_pct=excluded.live_return_pct,
            return_gap_pct=excluded.return_gap_pct,
            avg_entry_slippage_pct=excluded.avg_entry_slippage_pct,
            trade_count=excluded.trade_count,
            updated_at=datetime('now'),
            details_json=excluded.details_json
        """,
        (
            as_of_date,
            paper_ret,
            live_ret,
            gap,
            avg_slip,
            matched,
            json.dumps(details, ensure_ascii=False),
        ),
    )

    return {
        "paper_return_pct": round(paper_ret, 4),
        "live_return_pct": round(live_ret, 4),
        "return_gap_pct": round(gap, 4),
        "avg_entry_slippage_pct": round(avg_slip, 4),
        "trade_count": float(matched),
    }
