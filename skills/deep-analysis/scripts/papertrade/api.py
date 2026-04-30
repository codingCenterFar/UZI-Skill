from __future__ import annotations

import sqlite3
from typing import Any

from papertrade.api_common import PaperTradeAPIError, error_envelope, ok_envelope
from papertrade.api_events import stream_sse_frames
from papertrade.candidate_generator import generate_candidate_pool
from papertrade.candidate_pool import archive_candidate_pool_batch, create_candidate_pool_batch
from papertrade.command_handlers import (
    match_opening_command,
    rebuild_readmodels_command,
    record_runtime_config_command,
    replay_loop_command,
    start_runtime_command,
    stop_runtime_command,
    submit_sim_order_command,
)
from papertrade.config import PaperTradeConfig
from papertrade.quote_snapshots import refresh_quote_snapshots
from papertrade.read_models import (
    get_candidate_pool_model,
    get_dashboard_candidates_model,
    get_dashboard_summary_model,
    get_events_model,
    get_health_model,
    get_metrics_model,
    get_orders_model,
    get_positions_model,
    get_quote_batch_model,
    get_quote_snapshots_model,
    get_runtime_status_model,
    get_ticker_detail_model,
    get_watchlist_model,
)

REPLAY_LOOP_PATHS = {"/api/v1/runtime/replay-loop", "/api/v2/runtime/replay-loop", "/runtime/replay-loop"}
REBUILD_READMODELS_PATHS = {
    "/api/v1/runtime/rebuild-readmodels",
    "/api/v2/runtime/rebuild-readmodels",
    "/runtime/rebuild-readmodels",
}
RUNTIME_CONFIG_PATHS = {"/api/v1/runtime/config", "/api/v2/runtime/config", "/runtime/config"}
QUOTE_LATEST_PATHS = {"/api/v1/quotes/latest", "/api/v1/quote-snapshots"}
QUOTE_BATCH_PATHS = {"/api/v1/quotes/batch", "/api/v1/quote-snapshots/batch"}
QUOTE_REFRESH_PATHS = {"/api/v1/quotes/refresh", "/api/v1/quote-snapshots/refresh"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except Exception:
        return default


def _as_ticker_list(value: Any) -> list[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        items = [str(value).strip()]
    out = [item for item in items if item]
    return out or None


def _read(fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return ok_envelope(fn(*args, **kwargs))
    except PaperTradeAPIError as e:
        return error_envelope(e)
    except Exception as e:
        return error_envelope(PaperTradeAPIError("INTERNAL_ERROR", str(e), status_code=500))


def _write(conn: sqlite3.Connection, fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        data = fn(*args, **kwargs)
        conn.commit()
        return ok_envelope(data)
    except PaperTradeAPIError as e:
        conn.commit()
        return error_envelope(e)
    except Exception as e:
        conn.rollback()
        return error_envelope(PaperTradeAPIError("INTERNAL_ERROR", str(e), status_code=500))


def get_dashboard_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    return _read(get_dashboard_summary_model, conn)


def get_dashboard_candidates(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None = None,
    candidate_batch_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    return _read(
        get_dashboard_candidates_model,
        conn,
        pool_name=pool_name,
        candidate_batch_id=candidate_batch_id,
        limit=limit,
    )


def get_watchlist(
    conn: sqlite3.Connection,
    *,
    watchlist_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return _read(get_watchlist_model, conn, watchlist_id=watchlist_id, limit=limit)


def get_candidate_pool(
    conn: sqlite3.Connection,
    *,
    pool_name: str | None = None,
    candidate_batch_id: str | None = None,
    limit: int = 50,
    status: str | None = "active",
    include_archived: bool = False,
) -> dict[str, Any]:
    return _read(
        get_candidate_pool_model,
        conn,
        pool_name=pool_name,
        candidate_batch_id=candidate_batch_id,
        limit=limit,
        status=status,
        include_archived=include_archived,
    )


def get_quote_snapshots(
    conn: sqlite3.Connection,
    *,
    tickers: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return _read(get_quote_snapshots_model, conn, tickers=tickers, limit=limit)


def get_quote_batch(
    conn: sqlite3.Connection,
    *,
    quote_batch_id: str,
    limit: int = 500,
) -> dict[str, Any]:
    return _read(get_quote_batch_model, conn, quote_batch_id=quote_batch_id, limit=limit)


def get_ticker(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    return _read(get_ticker_detail_model, conn, ticker=code)


def get_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    cursor: str | None = None,
    order: str = "desc",
    include_pending: bool = True,
) -> dict[str, Any]:
    return _read(
        get_events_model,
        conn,
        limit=limit,
        cursor=cursor,
        order=order,
        include_pending=include_pending,
    )


def get_positions(conn: sqlite3.Connection) -> dict[str, Any]:
    return _read(get_positions_model, conn)


def get_orders(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return _read(get_orders_model, conn, ticker=ticker, status=status, limit=limit)


def get_runtime_status(conn: sqlite3.Connection) -> dict[str, Any]:
    return _read(get_runtime_status_model, conn)


def get_metrics(conn: sqlite3.Connection, *, limit: int = 100) -> dict[str, Any]:
    return _read(get_metrics_model, conn, limit=limit)


def get_health(
    conn: sqlite3.Connection,
    *,
    consistency_threshold: float = 0.01,
    emit_alerts: bool = False,
) -> dict[str, Any]:
    return _read(
        get_health_model,
        conn,
        consistency_threshold=consistency_threshold,
        emit_alerts=emit_alerts,
    )


def post_sim_order(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]:
    return _write(
        conn,
        submit_sim_order_command,
        conn,
        cfg=cfg,
        payload=payload,
        idempotency_key=idempotency_key,
    )


def post_runtime_start(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return _write(
        conn,
        start_runtime_command,
        conn,
        cfg=cfg,
        payload=payload,
        idempotency_key=idempotency_key,
    )


def post_runtime_stop(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _write(conn, stop_runtime_command, conn, payload=payload)


def post_match_opening(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _write(conn, match_opening_command, conn, payload=payload)


def post_replay_loop(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _write(conn, replay_loop_command, conn, payload=payload)


def post_rebuild_readmodels(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _write(conn, rebuild_readmodels_command, conn, cfg=cfg, payload=payload)


def post_runtime_config(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _write(conn, record_runtime_config_command, conn, cfg=cfg, payload=payload)


def post_candidate_pool(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(payload or {})
    return _write(
        conn,
        create_candidate_pool_batch,
        conn,
        items=list(data.get("items") or []),
        pool_name=data.get("pool_name"),
        source=str(data.get("source") or "manual"),
        as_of_date=data.get("as_of_date"),
        universe=data.get("universe"),
        metadata=data.get("metadata"),
        candidate_batch_id=data.get("candidate_batch_id"),
        generated_at_ms=data.get("generated_at_ms"),
        archive_previous=bool(data.get("archive_previous", False)),
    )


def post_archive_candidate_pool(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(payload or {})
    return _write(
        conn,
        archive_candidate_pool_batch,
        conn,
        candidate_batch_id=str(data.get("candidate_batch_id") or ""),
        reason=data.get("reason"),
        archived_at_ms=data.get("archived_at_ms"),
    )


def post_generate_candidate_pool(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(payload or {})
    return _write(
        conn,
        generate_candidate_pool,
        conn,
        cfg=cfg,
        pool_name=data.get("pool_name"),
        watchlist_id=data.get("watchlist_id", "wl_default"),
        cache_roots=data.get("cache_roots"),
        recent_signal_limit=int(data.get("recent_signal_limit", 100)),
        cache_scan_limit=int(data.get("cache_scan_limit", 200)),
        max_candidates=int(data.get("max_candidates", 50)),
        as_of_date=data.get("as_of_date"),
        archive_previous=bool(data.get("archive_previous", True)),
    )


def post_quote_refresh(
    conn: sqlite3.Connection,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(payload or {})
    return _write(
        conn,
        refresh_quote_snapshots,
        conn,
        tickers=_as_ticker_list(data.get("tickers")),
        include_positions=_as_bool(data.get("include_positions"), False),
        candidate_batch_id=data.get("candidate_batch_id"),
        pool_name=data.get("pool_name"),
        candidate_limit=int(data.get("candidate_limit", 100)),
        source=str(data.get("source") or "fetch_basic"),
        quote_batch_id=data.get("quote_batch_id"),
        provider_timeout_seconds=_as_float(
            data.get("provider_timeout_seconds", data.get("timeout_seconds")),
            None,
        ),
    )


def handle_request(
    conn: sqlite3.Connection,
    *,
    cfg: PaperTradeConfig,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    method_u = str(method or "").upper()
    path_s = str(path or "").rstrip("/") or "/"
    q = query or {}
    h = headers or {}
    idem = h.get("Idempotency-Key") or h.get("idempotency-key")

    if method_u == "GET" and path_s == "/api/v1/dashboard/summary":
        return get_dashboard_summary(conn)
    if method_u == "GET" and path_s == "/api/v1/dashboard/candidates":
        return get_dashboard_candidates(
            conn,
            pool_name=q.get("pool_name"),
            candidate_batch_id=q.get("candidate_batch_id"),
            limit=int(q.get("limit", 20)),
        )
    if method_u == "GET" and path_s == "/api/v1/watchlist":
        return get_watchlist(conn, watchlist_id=q.get("watchlist_id"), limit=int(q.get("limit", 100)))
    if method_u == "GET" and path_s == "/api/v1/candidate-pool":
        return get_candidate_pool(
            conn,
            pool_name=q.get("pool_name"),
            candidate_batch_id=q.get("candidate_batch_id"),
            limit=int(q.get("limit", 50)),
            status=q.get("status", "active"),
            include_archived=bool(q.get("include_archived", False)),
        )
    if method_u == "GET" and path_s in QUOTE_LATEST_PATHS:
        return get_quote_snapshots(
            conn,
            tickers=_as_ticker_list(q.get("tickers") or q.get("ticker")),
            limit=int(q.get("limit", 100)),
        )
    if method_u == "GET" and path_s in QUOTE_BATCH_PATHS:
        return get_quote_batch(
            conn,
            quote_batch_id=str(q.get("quote_batch_id") or ""),
            limit=int(q.get("limit", 500)),
        )
    if method_u == "GET" and path_s.startswith("/api/v1/ticker/"):
        return get_ticker(conn, path_s.rsplit("/", 1)[-1])
    if method_u == "GET" and path_s == "/api/v1/events":
        return get_events(
            conn,
            limit=int(q.get("limit", 100)),
            cursor=q.get("cursor"),
            order=str(q.get("order", "desc")),
            include_pending=bool(q.get("include_pending", True)),
        )
    if method_u == "GET" and path_s == "/api/v1/positions":
        return get_positions(conn)
    if method_u == "GET" and path_s == "/api/v1/orders":
        return get_orders(
            conn,
            ticker=q.get("ticker"),
            status=q.get("status"),
            limit=int(q.get("limit", 50)),
        )
    if method_u == "GET" and path_s == "/api/v1/runtime/status":
        return get_runtime_status(conn)
    if method_u == "GET" and path_s == "/api/v1/metrics":
        return get_metrics(conn, limit=int(q.get("limit", 100)))
    if method_u == "GET" and path_s == "/api/v1/health":
        return get_health(
            conn,
            consistency_threshold=float(q.get("consistency_threshold", 0.01)),
            emit_alerts=bool(q.get("emit_alerts", False)),
        )
    if method_u == "POST" and path_s == "/api/v1/sim/order":
        return post_sim_order(conn, cfg=cfg, payload=body or {}, idempotency_key=idem)
    if method_u == "POST" and path_s == "/api/v1/runtime/start":
        return post_runtime_start(conn, cfg=cfg, payload=body or {}, idempotency_key=idem)
    if method_u == "POST" and path_s == "/api/v1/runtime/stop":
        return post_runtime_stop(conn, payload=body or {})
    if method_u == "POST" and path_s == "/api/v1/runtime/match-opening":
        return post_match_opening(conn, payload=body or {})
    if method_u == "POST" and path_s in REPLAY_LOOP_PATHS:
        return post_replay_loop(conn, payload=body or {})
    if method_u == "POST" and path_s in REBUILD_READMODELS_PATHS:
        return post_rebuild_readmodels(conn, cfg=cfg, payload=body or {})
    if method_u == "POST" and path_s in RUNTIME_CONFIG_PATHS:
        return post_runtime_config(conn, cfg=cfg, payload=body or {})
    if method_u == "POST" and path_s == "/api/v1/candidate-pool":
        return post_candidate_pool(conn, payload=body or {})
    if method_u == "POST" and path_s == "/api/v1/candidate-pool/archive":
        return post_archive_candidate_pool(conn, payload=body or {})
    if method_u == "POST" and path_s == "/api/v1/candidate-pool/generate":
        return post_generate_candidate_pool(conn, cfg=cfg, payload=body or {})
    if method_u == "POST" and path_s in QUOTE_REFRESH_PATHS:
        return post_quote_refresh(conn, payload=body or {})

    return error_envelope(
        PaperTradeAPIError(
            "INVALID_REQUEST",
            "unknown papertrade API endpoint",
            {"method": method_u, "path": path_s},
            status_code=404,
        )
    )


__all__ = [
    "get_dashboard_summary",
    "get_dashboard_candidates",
    "get_watchlist",
    "get_candidate_pool",
    "get_quote_snapshots",
    "get_quote_batch",
    "get_ticker",
    "get_events",
    "get_positions",
    "get_orders",
    "get_runtime_status",
    "get_metrics",
    "get_health",
    "post_sim_order",
    "post_runtime_start",
    "post_runtime_stop",
    "post_match_opening",
    "post_replay_loop",
    "post_rebuild_readmodels",
    "post_runtime_config",
    "post_candidate_pool",
    "post_archive_candidate_pool",
    "post_generate_candidate_pool",
    "post_quote_refresh",
    "handle_request",
    "stream_sse_frames",
]
