from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.market_router import parse_ticker  # noqa: E402
from papertrade.api_common import PaperTradeAPIError  # noqa: E402
from papertrade.candidate_pool import DEFAULT_POOL_NAME, get_candidate_pool_top  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.event_dispatcher import dispatch_outbox_once  # noqa: E402
from papertrade.ledger import append_jsonl, connect, init_db  # noqa: E402
from papertrade.opening_matcher import match_queued_day_orders  # noqa: E402
from papertrade.quote_snapshots import QuoteProvider, refresh_quote_snapshots  # noqa: E402
from papertrade.read_models import get_watchlist_model  # noqa: E402
from papertrade.run_cycle import run_cycle  # noqa: E402
from papertrade.runtime_store import (  # noqa: E402
    DEFAULT_RUNTIME_LEASE_NAME,
    DEFAULT_RUNTIME_LEASE_TTL_MS,
    acquire_runtime_lease,
    finish_runtime_session,
    heartbeat_runtime_session,
    release_runtime_lease,
    renew_runtime_lease,
    start_runtime_session,
)
from papertrade.session_scheduler import (  # noqa: E402
    build_session_plan,
    expire_queued_day_orders,
    rebuild_candidate_pool_after_close,
    record_after_close_review,
)


def _parse_tickers(raw: str) -> list[str]:
    out: list[str] = []
    for x in (raw or "").split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out


def _normalise_ticker(raw: Any) -> str:
    ticker = str(raw or "").strip()
    if not ticker:
        return ""
    parsed = parse_ticker(ticker)
    return str(parsed.full or ticker).upper()


def _dedupe_tickers(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = _normalise_ticker(value)
        if ticker and ticker not in seen:
            out.append(ticker)
            seen.add(ticker)
    return out


def _position_tickers(conn: Any) -> list[str]:
    rows = conn.execute(
        """
        SELECT ticker FROM positions WHERE qty > 0
        UNION
        SELECT ticker FROM paper_positions WHERE quantity > 0
        ORDER BY ticker
        """
    ).fetchall()
    return [str(row["ticker"]) for row in rows]


def resolve_realtime_universe(
    conn: Any,
    *,
    explicit_tickers: list[str] | None = None,
    candidate_pool_name: str | None = None,
    candidate_batch_id: str | None = None,
    candidate_limit: int = 50,
    watchlist_id: str | None = None,
    watchlist_limit: int = 100,
    include_positions: bool = False,
) -> dict[str, Any]:
    sources: dict[str, list[str]] = {
        "explicit": _dedupe_tickers(explicit_tickers or []),
        "candidate_pool": [],
        "watchlist": [],
        "positions": [],
    }
    warnings: list[dict[str, Any]] = []

    candidate_requested = bool(candidate_pool_name or candidate_batch_id)
    should_try_default_candidate = not sources["explicit"] and not candidate_requested
    if candidate_requested or should_try_default_candidate:
        try:
            pool = get_candidate_pool_top(
                conn,
                pool_name=candidate_pool_name or DEFAULT_POOL_NAME,
                candidate_batch_id=candidate_batch_id,
                limit=candidate_limit,
            )
            sources["candidate_pool"] = _dedupe_tickers([item.get("ticker") for item in pool.get("items", [])])
        except PaperTradeAPIError as e:
            if candidate_requested and not sources["explicit"]:
                raise
            warnings.append(
                {
                    "source": "candidate_pool",
                    "error_code": e.error_code,
                    "message": e.message,
                    "details": e.details or {},
                }
            )

    watchlist_requested = bool(watchlist_id)
    should_try_watchlist = watchlist_requested or (not sources["explicit"] and not sources["candidate_pool"])
    if should_try_watchlist:
        try:
            watchlist = get_watchlist_model(conn, watchlist_id=watchlist_id, limit=watchlist_limit)
            sources["watchlist"] = _dedupe_tickers([item.get("ticker") for item in watchlist.get("items", [])])
        except PaperTradeAPIError as e:
            if watchlist_requested and not sources["explicit"] and not sources["candidate_pool"]:
                raise
            warnings.append(
                {
                    "source": "watchlist",
                    "error_code": e.error_code,
                    "message": e.message,
                    "details": e.details or {},
                }
            )

    if include_positions:
        sources["positions"] = _dedupe_tickers(_position_tickers(conn))

    tickers = _dedupe_tickers(
        sources["explicit"]
        + sources["candidate_pool"]
        + sources["watchlist"]
        + sources["positions"]
    )
    if not tickers:
        raise PaperTradeAPIError(
            "INVALID_REQUEST",
            "realtime requires --tickers or an active candidate pool/watchlist",
            {
                "candidate_pool_name": candidate_pool_name or DEFAULT_POOL_NAME,
                "candidate_batch_id": candidate_batch_id,
                "watchlist_id": watchlist_id,
            },
        )

    return {
        "tickers": tickers,
        "ticker_count": len(tickers),
        "sources": sources,
        "candidate_pool_name": candidate_pool_name or (DEFAULT_POOL_NAME if sources["candidate_pool"] else None),
        "candidate_batch_id": candidate_batch_id,
        "watchlist_id": watchlist_id,
        "include_positions": bool(include_positions),
        "warnings": warnings,
    }


def realtime_refresh_mode(
    *,
    loop_no: int,
    refresh_every_loops: int,
    from_cache_only: bool = False,
    quote_only_between_full: bool = False,
) -> dict[str, Any]:
    interval = max(1, int(refresh_every_loops))
    is_full_refresh = (not from_cache_only) and (((int(loop_no) - 1) % interval) == 0)
    cycle_from_cache_only = bool(from_cache_only or not is_full_refresh)
    if quote_only_between_full and not is_full_refresh:
        mode = "quote_only"
    elif is_full_refresh:
        mode = "full_refresh"
    else:
        mode = "cached_cycle"
    return {
        "mode": mode,
        "is_full_refresh": bool(is_full_refresh),
        "cycle_from_cache_only": cycle_from_cache_only,
    }


def run_quote_only_refresh(
    conn: Any,
    *,
    tickers: list[str],
    session_id: str,
    loop_no: int,
    provider: QuoteProvider | None = None,
    quote_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    quote_batch_id = f"qbatch_{str(session_id)[:12]}_{int(loop_no):06d}"
    refreshed = refresh_quote_snapshots(
        conn,
        tickers=tickers,
        provider=provider,
        source="fetch_basic",
        quote_batch_id=quote_batch_id,
        provider_timeout_seconds=quote_timeout_seconds,
    )
    conn.commit()
    return refreshed


def _build_realtime_log_path(run_logs_jsonl: Path) -> Path:
    base = Path(run_logs_jsonl)
    return base.parent / "realtime_logs.jsonl"


def _dispatch_events_safe(db_path: Path) -> dict[str, Any]:
    try:
        return dispatch_outbox_once(db_path)
    except Exception as e:
        return {
            "scanned": 0,
            "published": 0,
            "retried": 0,
            "dead": 0,
            "errors": [str(e)],
        }


def _release_lease_safe(session_conn: Any, *, session_id: str, lease_name: str) -> bool:
    try:
        return release_runtime_lease(
            session_conn,
            lease_name=lease_name,
            owner_session_id=session_id,
        )
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="UZI paper-trade realtime simulator")
    ap.add_argument("--tickers", default="", help="逗号分隔，例如 600519.SH,002273.SZ；留空则从候选池/watchlist 解析")
    ap.add_argument("--depth", choices=["lite", "medium", "deep"], default="medium")
    ap.add_argument("--config", default=None, help="可选 JSON 配置文件")
    ap.add_argument("--mode", choices=["short", "swing"], default=None, help="覆盖策略模式")
    ap.add_argument("--poll-seconds", type=int, default=None, help="轮询间隔秒数")
    ap.add_argument("--max-loops", type=int, default=None, help="最大轮数，0 表示无限")
    ap.add_argument("--session-seconds", type=int, default=None, help="最长运行秒数，0 表示无限")
    ap.add_argument("--refresh-every-loops", type=int, default=None, help="每 N 轮做一次完整分析，其余走缓存")
    ap.add_argument("--lease-ttl-seconds", type=int, default=None, help="runtime 单活锁 TTL 秒数")
    ap.add_argument("--no-resume", action="store_true", help="首轮强制 stage1 重抓")
    ap.add_argument("--from-cache-only", action="store_true", help="所有轮次都只读缓存")
    ap.add_argument("--dry-run", action="store_true", help="只出建议，不落模拟订单/成交")
    ap.add_argument("--live-return-pct", type=float, default=None, help="可选：人工实盘当日收益率(%%)")
    ap.add_argument("--sleep-on-error-seconds", type=int, default=None, help="单轮失败后的等待秒数")
    ap.add_argument("--stop-on-error", action="store_true", help="单轮失败后立即退出")
    ap.add_argument("--candidate-pool-name", default=None, help="从候选池读取 ticker universe；未传 tickers 时默认尝试 default")
    ap.add_argument("--candidate-batch-id", default=None, help="指定候选池批次")
    ap.add_argument("--candidate-limit", type=int, default=50, help="候选池最多取多少只")
    ap.add_argument("--watchlist-id", default=None, help="从 watchlist 读取 ticker universe；未传 tickers 且无候选池时尝试默认 watchlist")
    ap.add_argument("--watchlist-limit", type=int, default=100, help="watchlist 最多取多少只")
    ap.add_argument("--include-positions", action="store_true", help="ticker universe 额外包含当前模拟持仓")
    ap.add_argument("--quote-only-between-full", action="store_true", help="非 full-refresh 轮次只刷新 quote_snapshots，不跑缓存决策循环")
    ap.add_argument("--quote-timeout-seconds", type=float, default=None, help="真实 quote provider 单票限时秒数，0 表示不启用限时保护")
    ap.add_argument("--strategy-refresh-depth", choices=["lite", "medium", "deep"], default=None, help="行情覆盖后重算策略层使用的深度")
    ap.add_argument("--after-close-rebuild-candidate-pool", action="store_true", help="收盘后用本地事实源重建候选池")
    ap.add_argument("--after-close-max-candidates", type=int, default=50, help="收盘后候选池重建最多保留多少只")

    quote_group = ap.add_mutually_exclusive_group()
    quote_group.add_argument("--quote-overlay", dest="quote_overlay", action="store_true", help="启用实时行情覆盖")
    quote_group.add_argument("--no-quote-overlay", dest="quote_overlay", action="store_false", help="禁用实时行情覆盖")
    ap.set_defaults(quote_overlay=None)

    strategy_refresh_group = ap.add_mutually_exclusive_group()
    strategy_refresh_group.add_argument("--strategy-refresh", dest="strategy_refresh", action="store_true", help="行情覆盖后重算策略信号与短线模块")
    strategy_refresh_group.add_argument("--no-strategy-refresh", dest="strategy_refresh", action="store_false", help="行情覆盖后不重算策略信号")
    ap.set_defaults(strategy_refresh=None)

    watcher_group = ap.add_mutually_exclusive_group()
    watcher_group.add_argument("--watcher-overlay", dest="watcher_overlay", action="store_true", help="启用模拟盯盘人物修正")
    watcher_group.add_argument("--no-watcher-overlay", dest="watcher_overlay", action="store_false", help="禁用模拟盯盘人物修正")
    ap.set_defaults(watcher_overlay=None)

    args = ap.parse_args()

    overrides: dict[str, Any] = {"depth": args.depth}
    if args.mode:
        overrides["policy"] = {"mode": args.mode}

    cfg = load_config(args.config, overrides=overrides)
    explicit_tickers = _parse_tickers(args.tickers)

    poll_seconds = int(args.poll_seconds if args.poll_seconds is not None else cfg.realtime.poll_seconds)
    max_loops = int(args.max_loops if args.max_loops is not None else cfg.realtime.max_loops)
    session_seconds = int(args.session_seconds if args.session_seconds is not None else cfg.realtime.session_seconds)
    refresh_every_loops = int(args.refresh_every_loops if args.refresh_every_loops is not None else cfg.realtime.refresh_every_loops)
    lease_ttl_seconds = int(
        args.lease_ttl_seconds
        if args.lease_ttl_seconds is not None
        else getattr(cfg.realtime, "lease_ttl_seconds", 0) or int(DEFAULT_RUNTIME_LEASE_TTL_MS / 1000)
    )
    lease_ttl_ms = max(1_000, lease_ttl_seconds * 1000)
    sleep_on_error_seconds = int(
        args.sleep_on_error_seconds if args.sleep_on_error_seconds is not None else cfg.realtime.sleep_on_error_seconds
    )
    quote_timeout_seconds = float(
        args.quote_timeout_seconds
        if args.quote_timeout_seconds is not None
        else getattr(cfg.realtime, "quote_timeout_seconds", 8.0)
    )
    cfg.realtime.quote_timeout_seconds = quote_timeout_seconds
    if args.strategy_refresh is not None:
        cfg.realtime.strategy_refresh = bool(args.strategy_refresh)
    if args.strategy_refresh_depth:
        cfg.realtime.strategy_refresh_depth = args.strategy_refresh_depth
    if refresh_every_loops <= 0:
        refresh_every_loops = 1

    session_conn = connect(cfg.db_path)
    init_db(session_conn, cfg.trade.initial_cash)
    try:
        universe = resolve_realtime_universe(
            session_conn,
            explicit_tickers=explicit_tickers,
            candidate_pool_name=args.candidate_pool_name,
            candidate_batch_id=args.candidate_batch_id,
            candidate_limit=args.candidate_limit,
            watchlist_id=args.watchlist_id,
            watchlist_limit=args.watchlist_limit,
            include_positions=args.include_positions,
        )
    except PaperTradeAPIError as e:
        session_conn.close()
        raise SystemExit(f"{e.error_code}: {e.message} {json.dumps(e.details or {}, ensure_ascii=False)}")
    tickers = list(universe["tickers"])
    runtime_config = cfg.as_dict()
    runtime_config.setdefault("realtime", {})["quote_timeout_seconds"] = quote_timeout_seconds
    session_id = start_runtime_session(
        session_conn,
        start_request_id=None,
        config_json={
            **runtime_config,
            "realtime_universe": universe,
            "quote_only_between_full": bool(args.quote_only_between_full),
        },
    )
    lease_result = acquire_runtime_lease(
        session_conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=session_id,
        ttl_ms=lease_ttl_ms,
    )
    if not lease_result.acquired:
        finish_runtime_session(
            session_conn,
            session_id=session_id,
            status="failed",
            stop_reason="runtime_lock_conflict",
            last_loop_id=None,
        )
        session_conn.commit()
        session_conn.close()
        raise SystemExit(
            "RUNTIME_ALREADY_RUNNING: "
            f"owner_session_id={lease_result.owner_session_id} "
            f"expires_at_ms={lease_result.lease_expires_at_ms}"
        )
    session_conn.commit()

    loop_idx = 0
    last_loop_id: str | None = None
    started = time.monotonic()
    realtime_log_path = _build_realtime_log_path(cfg.run_logs_jsonl)

    while True:
        loop_idx += 1
        loop_start = time.monotonic()
        as_of_date = date.today().isoformat()
        refresh_mode = realtime_refresh_mode(
            loop_no=loop_idx,
            refresh_every_loops=refresh_every_loops,
            from_cache_only=args.from_cache_only,
            quote_only_between_full=args.quote_only_between_full,
        )
        cycle_from_cache_only = bool(refresh_mode["cycle_from_cache_only"])
        session_plan = build_session_plan(
            trade_date=as_of_date,
            refresh_mode=refresh_mode,
        )

        try:
            lease_ok = renew_runtime_lease(
                session_conn,
                lease_name=DEFAULT_RUNTIME_LEASE_NAME,
                owner_session_id=session_id,
                ttl_ms=lease_ttl_ms,
            )
            if not lease_ok:
                raise RuntimeError("runtime lease lost before cycle start")
            session_conn.commit()
            if session_plan["match_opening"]:
                opening_match = match_queued_day_orders(session_conn)
            else:
                opening_match = {
                    "matched": 0,
                    "filled": 0,
                    "rejected": 0,
                    "skipped": 0,
                    "market_phase": session_plan["market_phase"],
                    "trade_date": session_plan["trade_date"],
                    "items": [],
                    "skipped_reason": f"scheduler_phase={session_plan['scheduler_phase']}",
                }
            session_conn.commit()
            if session_plan["loop_mode"] == "quote_only":
                result = {
                    "run_id": None,
                    "loop_id": None,
                    "rows": [],
                    "nav": {},
                    "quote_refresh": run_quote_only_refresh(
                        session_conn,
                        tickers=tickers,
                        session_id=session_id,
                        loop_no=loop_idx,
                        quote_timeout_seconds=quote_timeout_seconds,
                    ),
                }
            elif session_plan["cycle_enabled"]:
                result = run_cycle(
                    tickers=tickers,
                    cfg=cfg,
                    as_of_date=as_of_date,
                    no_resume=args.no_resume and loop_idx == 1,
                    from_cache_only=cycle_from_cache_only,
                    dry_run=args.dry_run,
                    live_return_pct=args.live_return_pct,
                    realtime_quote_overlay=args.quote_overlay,
                    watcher_overlay=args.watcher_overlay,
                    session_id=session_id,
                    loop_no=loop_idx,
                    lease_ttl_ms=lease_ttl_ms,
                )
            else:
                result = {"run_id": None, "loop_id": None, "rows": [], "nav": {}}
            day_order_expiry = None
            if session_plan["expire_day_orders"]:
                day_order_expiry = expire_queued_day_orders(
                    session_conn,
                    trade_date=as_of_date,
                    session_id=session_id,
                    loop_no=loop_idx,
                )
                session_conn.commit()
            after_close_review = None
            if session_plan["after_close_review"]:
                after_close_review = record_after_close_review(
                    session_conn,
                    trade_date=as_of_date,
                    session_id=session_id,
                    loop_no=loop_idx,
                    ticker_universe=universe,
                )
                session_conn.commit()
            candidate_pool_rebuild = None
            if session_plan["after_close_review"] and args.after_close_rebuild_candidate_pool:
                candidate_pool_rebuild = rebuild_candidate_pool_after_close(
                    session_conn,
                    cfg=cfg,
                    trade_date=as_of_date,
                    session_id=session_id,
                    loop_no=loop_idx,
                    pool_name=args.candidate_pool_name,
                    watchlist_id=args.watchlist_id,
                    max_candidates=args.after_close_max_candidates,
                )
                session_conn.commit()
            dispatch_stats = _dispatch_events_safe(cfg.db_path)
            last_loop_id = str(result.get("loop_id") or "") or last_loop_id
            heartbeat_runtime_session(session_conn, session_id=session_id, last_loop_id=last_loop_id)
            renew_runtime_lease(
                session_conn,
                lease_name=DEFAULT_RUNTIME_LEASE_NAME,
                owner_session_id=session_id,
                ttl_ms=lease_ttl_ms,
            )
            session_conn.commit()
            payload = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "loop": loop_idx,
                "status": "ok",
                "as_of_date": as_of_date,
                "from_cache_only": cycle_from_cache_only,
                "refresh_mode": refresh_mode["mode"],
                "session_plan": session_plan,
                "ticker_universe": universe,
                "session_id": session_id,
                "run_id": result.get("run_id"),
                "loop_id": result.get("loop_id"),
                "rows": result.get("rows", []),
                "nav": result.get("nav", {}),
                "quote_refresh": result.get("quote_refresh"),
                "opening_match": opening_match,
                "day_order_expiry": day_order_expiry,
                "after_close_review": after_close_review,
                "candidate_pool_rebuild": candidate_pool_rebuild,
                "event_dispatch": dispatch_stats,
                "elapsed_seconds": round(time.monotonic() - loop_start, 3),
            }
            append_jsonl(realtime_log_path, payload)
            print(json.dumps(payload, ensure_ascii=False))
        except KeyboardInterrupt:
            stop_payload = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "loop": loop_idx,
                "status": "stopped_by_keyboard",
                "session_id": session_id,
            }
            finish_runtime_session(
                session_conn,
                session_id=session_id,
                status="stopped",
                stop_reason="keyboard_interrupt",
                last_loop_id=None,
            )
            _release_lease_safe(
                session_conn,
                session_id=session_id,
                lease_name=DEFAULT_RUNTIME_LEASE_NAME,
            )
            session_conn.commit()
            append_jsonl(realtime_log_path, stop_payload)
            print(json.dumps(stop_payload, ensure_ascii=False))
            session_conn.close()
            return
        except Exception as e:
            dispatch_stats = _dispatch_events_safe(cfg.db_path)
            heartbeat_runtime_session(session_conn, session_id=session_id, last_loop_id=None)
            session_conn.commit()
            payload = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "loop": loop_idx,
                "status": "error",
                "as_of_date": as_of_date,
                "from_cache_only": cycle_from_cache_only,
                "refresh_mode": refresh_mode["mode"],
                "session_plan": session_plan,
                "ticker_universe": universe,
                "session_id": session_id,
                "error": str(e),
                "event_dispatch": dispatch_stats,
                "elapsed_seconds": round(time.monotonic() - loop_start, 3),
            }
            append_jsonl(realtime_log_path, payload)
            print(json.dumps(payload, ensure_ascii=False))
            fatal_lock_loss = "runtime lease" in str(e).lower()
            if args.stop_on_error or fatal_lock_loss:
                finish_runtime_session(
                    session_conn,
                    session_id=session_id,
                    status="failed",
                    stop_reason="runtime_lock_lost" if fatal_lock_loss else "stop_on_error",
                    last_loop_id=last_loop_id,
                )
                _release_lease_safe(
                    session_conn,
                    session_id=session_id,
                    lease_name=DEFAULT_RUNTIME_LEASE_NAME,
                )
                session_conn.commit()
                session_conn.close()
                raise
            if sleep_on_error_seconds > 0:
                time.sleep(sleep_on_error_seconds)

        elapsed = time.monotonic() - started
        if max_loops > 0 and loop_idx >= max_loops:
            break
        if session_seconds > 0 and elapsed >= session_seconds:
            break
        if poll_seconds > 0:
            time.sleep(poll_seconds)

    final_payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "status": "completed",
        "session_id": session_id,
        "loops": loop_idx,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    finish_runtime_session(
        session_conn,
        session_id=session_id,
        status="stopped",
        stop_reason="completed",
        last_loop_id=last_loop_id,
    )
    _release_lease_safe(
        session_conn,
        session_id=session_id,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
    )
    session_conn.commit()
    append_jsonl(realtime_log_path, final_payload)
    print(json.dumps(final_payload, ensure_ascii=False))
    session_conn.close()


if __name__ == "__main__":
    main()
