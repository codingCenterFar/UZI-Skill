from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from lib.market_router import parse_ticker

from papertrade.api_common import PaperTradeAPIError, new_id, now_ms


DEFAULT_POOL_NAME = "default"
DEFAULT_ACTION_STATE = "CANDIDATE_OBSERVE"
ACTIVE_STATUS = "active"
ARCHIVED_STATUS = "archived"


def _json_dumps(value: Any, default: Any) -> str:
    data = default if value is None else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        data = json.loads(raw)
    except Exception:
        return default
    return data


def _pool_name(value: Any) -> str:
    name = str(value or DEFAULT_POOL_NAME).strip()
    if not name:
        raise PaperTradeAPIError("INVALID_REQUEST", "pool_name is required", {"field": "pool_name"})
    return name


def _ticker(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise PaperTradeAPIError("INVALID_REQUEST", "candidate ticker is required", {"field": "ticker"})
    parsed = parse_ticker(raw)
    return str(parsed.full or raw).upper(), str(parsed.market or "").upper()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _normalise_item(raw_item: dict[str, Any], position: int) -> dict[str, Any]:
    if not isinstance(raw_item, dict):
        raise PaperTradeAPIError("INVALID_REQUEST", "candidate item must be an object", {"position": position})

    ticker, market_from_ticker = _ticker(raw_item.get("ticker") or raw_item.get("code"))
    score_raw = raw_item.get("candidate_score", raw_item.get("score"))
    score = _as_float(score_raw)
    rank = _as_int(raw_item.get("rank"), position)
    action_state = str(raw_item.get("action_state") or raw_item.get("action") or DEFAULT_ACTION_STATE).strip().upper()
    if not action_state:
        action_state = DEFAULT_ACTION_STATE

    return {
        "rank": rank,
        "ticker": ticker,
        "market": str(raw_item.get("market") or market_from_ticker or "").upper() or None,
        "name": raw_item.get("name"),
        "candidate_score": score,
        "action_state": action_state,
        "reasons": _as_list(raw_item.get("reasons")),
        "source_tags": _as_list(raw_item.get("source_tags", raw_item.get("sources"))),
        "staleness": _as_dict(raw_item.get("staleness")),
        "analysis_age_ms": max(0, _as_int(raw_item.get("analysis_age_ms"))),
        "quote_age_ms": max(0, _as_int(raw_item.get("quote_age_ms"))),
        "quote_ts_ms": raw_item.get("quote_ts_ms"),
        "analysis_snapshot_id": raw_item.get("analysis_snapshot_id"),
        "decision_snapshot_id": raw_item.get("decision_snapshot_id"),
        "signal_id": raw_item.get("signal_id"),
        "metadata": _as_dict(raw_item.get("metadata")),
    }


def _prepare_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        raise PaperTradeAPIError("INVALID_REQUEST", "candidate pool requires at least one item", {"field": "items"})

    prepared = [_normalise_item(item, idx) for idx, item in enumerate(items, start=1)]
    seen: set[str] = set()
    for item in prepared:
        if item["ticker"] in seen:
            raise PaperTradeAPIError("INVALID_REQUEST", "duplicate candidate ticker", {"ticker": item["ticker"]})
        seen.add(item["ticker"])

    prepared.sort(key=lambda x: (_as_int(x.get("rank"), 999_999), -_as_float(x.get("candidate_score"))))
    for idx, item in enumerate(prepared, start=1):
        item["rank"] = idx
    return prepared


def _row_to_batch(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "candidate_batch_id": data.get("candidate_batch_id"),
        "pool_name": data.get("pool_name"),
        "source": data.get("source"),
        "status": data.get("status"),
        "as_of_date": data.get("as_of_date"),
        "universe": _json_loads(data.get("universe_json"), []),
        "metadata": _json_loads(data.get("metadata_json"), {}),
        "generated_at_ms": data.get("generated_at_ms"),
        "archived_at_ms": data.get("archived_at_ms"),
        "archive_reason": data.get("archive_reason"),
        "created_at_ms": data.get("created_at_ms"),
        "updated_at_ms": data.get("updated_at_ms"),
    }


def _row_to_item(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "candidate_item_id": data.get("candidate_item_id"),
        "candidate_batch_id": data.get("candidate_batch_id"),
        "rank": _as_int(data.get("rank")),
        "ticker": str(data.get("ticker") or "").upper(),
        "market": data.get("market"),
        "name": data.get("name"),
        "candidate_score": round(_as_float(data.get("candidate_score")), 4),
        "action_state": data.get("action_state"),
        "reasons": _json_loads(data.get("reasons_json"), []),
        "source_tags": _json_loads(data.get("source_tags_json"), []),
        "staleness": _json_loads(data.get("staleness_json"), {}),
        "analysis_age_ms": _as_int(data.get("analysis_age_ms")),
        "quote_age_ms": _as_int(data.get("quote_age_ms")),
        "quote_ts_ms": data.get("quote_ts_ms"),
        "analysis_snapshot_id": data.get("analysis_snapshot_id"),
        "decision_snapshot_id": data.get("decision_snapshot_id"),
        "signal_id": data.get("signal_id"),
        "metadata": _json_loads(data.get("metadata_json"), {}),
        "created_at_ms": data.get("created_at_ms"),
        "updated_at_ms": data.get("updated_at_ms"),
    }


def create_candidate_pool_batch(
    conn: sqlite3.Connection,
    *,
    items: list[dict[str, Any]],
    pool_name: str | None = None,
    source: str = "manual",
    as_of_date: str | None = None,
    universe: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    candidate_batch_id: str | None = None,
    generated_at_ms: int | None = None,
    archive_previous: bool = False,
) -> dict[str, Any]:
    pool = _pool_name(pool_name)
    source_s = str(source or "manual").strip() or "manual"
    batch_id = str(candidate_batch_id or new_id("cpb")).strip()
    if not batch_id:
        raise PaperTradeAPIError("INVALID_REQUEST", "candidate_batch_id is required", {"field": "candidate_batch_id"})
    existing = conn.execute(
        "SELECT candidate_batch_id FROM candidate_pool WHERE candidate_batch_id=?",
        (batch_id,),
    ).fetchone()
    if existing:
        raise PaperTradeAPIError(
            "CANDIDATE_POOL_EXISTS",
            "candidate pool batch already exists",
            {"candidate_batch_id": batch_id},
            status_code=409,
        )

    now = now_ms()
    generated = int(generated_at_ms or now)
    as_of = str(as_of_date or date.fromtimestamp(generated / 1000).isoformat())
    prepared = _prepare_items(items)
    universe_norm = [ticker for ticker, _ in (_ticker(x) for x in _as_list(universe))]

    if archive_previous:
        conn.execute(
            """
            UPDATE candidate_pool
            SET status=?, archived_at_ms=?, archive_reason=?, updated_at_ms=?
            WHERE pool_name=? AND status=?
            """,
            (ARCHIVED_STATUS, now, f"superseded_by:{batch_id}", now, pool, ACTIVE_STATUS),
        )

    conn.execute(
        """
        INSERT INTO candidate_pool (
            candidate_batch_id, pool_name, source, status, as_of_date,
            universe_json, metadata_json, generated_at_ms,
            archived_at_ms, archive_reason, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        """,
        (
            batch_id,
            pool,
            source_s,
            ACTIVE_STATUS,
            as_of,
            _json_dumps(universe_norm, []),
            _json_dumps(metadata, {}),
            generated,
            now,
            now,
        ),
    )

    for item in prepared:
        conn.execute(
            """
            INSERT INTO candidate_pool_items (
                candidate_item_id, candidate_batch_id, rank, ticker, market, name,
                candidate_score, action_state, reasons_json, source_tags_json,
                staleness_json, analysis_age_ms, quote_age_ms, quote_ts_ms,
                analysis_snapshot_id, decision_snapshot_id, signal_id,
                metadata_json, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("cpi"),
                batch_id,
                item["rank"],
                item["ticker"],
                item["market"],
                item["name"],
                item["candidate_score"],
                item["action_state"],
                _json_dumps(item["reasons"], []),
                _json_dumps(item["source_tags"], []),
                _json_dumps(item["staleness"], {}),
                item["analysis_age_ms"],
                item["quote_age_ms"],
                item["quote_ts_ms"],
                item["analysis_snapshot_id"],
                item["decision_snapshot_id"],
                item["signal_id"],
                _json_dumps(item["metadata"], {}),
                now,
                now,
            ),
        )

    return get_candidate_pool_top(conn, candidate_batch_id=batch_id, limit=len(prepared), include_archived=True)


def archive_candidate_pool_batch(
    conn: sqlite3.Connection,
    *,
    candidate_batch_id: str,
    reason: str | None = None,
    archived_at_ms: int | None = None,
) -> dict[str, Any]:
    batch_id = str(candidate_batch_id or "").strip()
    if not batch_id:
        raise PaperTradeAPIError("INVALID_REQUEST", "candidate_batch_id is required", {"field": "candidate_batch_id"})

    row = conn.execute("SELECT * FROM candidate_pool WHERE candidate_batch_id=?", (batch_id,)).fetchone()
    if not row:
        raise PaperTradeAPIError("NOT_FOUND", "candidate pool batch not found", {"candidate_batch_id": batch_id}, status_code=404)

    now = int(archived_at_ms or now_ms())
    archive_reason = str(reason or "operator_archive")
    conn.execute(
        """
        UPDATE candidate_pool
        SET status=?, archived_at_ms=COALESCE(archived_at_ms, ?), archive_reason=COALESCE(archive_reason, ?), updated_at_ms=?
        WHERE candidate_batch_id=?
        """,
        (ARCHIVED_STATUS, now, archive_reason, now, batch_id),
    )
    return get_candidate_pool_top(conn, candidate_batch_id=batch_id, include_archived=True)


def list_candidate_pool_batches(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None = None,
    status: str | None = ACTIVE_STATUS,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit_n = max(1, min(200, int(limit)))
    clauses: list[str] = []
    params: list[Any] = []
    if pool_name:
        clauses.append("pool_name=?")
        params.append(_pool_name(pool_name))
    if status:
        clauses.append("status=?")
        params.append(str(status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM candidate_pool
        {where}
        ORDER BY generated_at_ms DESC, candidate_batch_id DESC
        LIMIT ?
        """,
        tuple(params + [limit_n]),
    ).fetchall()
    return [_row_to_batch(row) for row in rows]


def _latest_batch(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None,
    status: str | None,
) -> sqlite3.Row | None:
    pool = _pool_name(pool_name)
    if status:
        return conn.execute(
            """
            SELECT *
            FROM candidate_pool
            WHERE pool_name=? AND status=?
            ORDER BY generated_at_ms DESC, candidate_batch_id DESC
            LIMIT 1
            """,
            (pool, str(status)),
        ).fetchone()
    return conn.execute(
        """
        SELECT *
        FROM candidate_pool
        WHERE pool_name=?
        ORDER BY generated_at_ms DESC, candidate_batch_id DESC
        LIMIT 1
        """,
        (pool,),
    ).fetchone()


def get_candidate_pool_top(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None = None,
    candidate_batch_id: str | None = None,
    limit: int = 50,
    status: str | None = ACTIVE_STATUS,
    include_archived: bool = False,
) -> dict[str, Any]:
    limit_n = max(1, min(500, int(limit)))
    if candidate_batch_id:
        batch = conn.execute(
            "SELECT * FROM candidate_pool WHERE candidate_batch_id=?",
            (str(candidate_batch_id),),
        ).fetchone()
    else:
        batch = _latest_batch(conn, pool_name=pool_name, status=None if include_archived else status)

    if not batch:
        raise PaperTradeAPIError(
            "NOT_FOUND",
            "candidate pool batch not found",
            {"pool_name": _pool_name(pool_name), "candidate_batch_id": candidate_batch_id},
            status_code=404,
        )
    if not include_archived and str(batch["status"]) == ARCHIVED_STATUS and candidate_batch_id:
        raise PaperTradeAPIError(
            "NOT_FOUND",
            "candidate pool batch is archived",
            {"candidate_batch_id": candidate_batch_id},
            status_code=404,
        )

    rows = conn.execute(
        """
        SELECT *
        FROM candidate_pool_items
        WHERE candidate_batch_id=?
        ORDER BY rank ASC, candidate_score DESC, ticker ASC
        LIMIT ?
        """,
        (batch["candidate_batch_id"], limit_n),
    ).fetchall()
    batch_payload = _row_to_batch(batch)
    items = [_row_to_item(row) for row in rows]
    return {
        **batch_payload,
        "item_count": len(items),
        "items": items,
    }
