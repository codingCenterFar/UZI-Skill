from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.market_router import parse_ticker  # noqa: E402
from papertrade.analysis_adapter import analyze_ticker  # noqa: E402
from papertrade.command_service import submit_intent_order  # noqa: E402
from papertrade.config import PaperTradeConfig, load_config  # noqa: E402
from papertrade.decision_policy import DecisionResult, evaluate_decision  # noqa: E402
from papertrade.drift_analytics import update_deviation_daily  # noqa: E402
from papertrade.ledger import (  # noqa: E402
    append_jsonl,
    connect,
    count_new_buys_today,
    finish_job_run,
    get_cash,
    get_position,
    init_db,
    mark_to_market,
    record_nav,
    record_signal,
    start_job_run,
)
from papertrade.market_snapshot import fetch_realtime_snapshot, inject_snapshot_into_bundle  # noqa: E402
from papertrade.metrics import (  # noqa: E402
    analysis_freshness_ms_from_bundle,
    build_loop_metrics,
    quote_freshness_ms_from_snapshot,
)
from papertrade.notifier import emit_alert, make_alert_payload  # noqa: E402
from papertrade.outbox import enqueue_outbox_event  # noqa: E402
from papertrade.quote_snapshots import record_quote_snapshot  # noqa: E402
from papertrade.runtime_store import (  # noqa: E402
    DEFAULT_RUNTIME_LEASE_NAME,
    DEFAULT_RUNTIME_LEASE_TTL_MS,
    acquire_runtime_lease,
    create_runtime_loop,
    finish_runtime_loop,
    release_runtime_lease,
)
from papertrade.watcher_persona import apply_watcher_overlay  # noqa: E402


def _today() -> str:
    return date.today().isoformat()


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _choose_trade_price(bundle: dict[str, Any], side: str) -> float:
    syn = bundle.get("synthesis") or {}
    st = syn.get("short_trading") or ((syn.get("friendly") or {}).get("short_trading") or {})
    levels = st.get("levels") or {}
    basic = ((bundle.get("raw") or {}).get("dimensions", {}).get("0_basic") or {}).get("data") or {}
    spot = _f(basic.get("price"), 0.0)

    if side == "BUY":
        px = _f(levels.get("retest_entry"), 0.0)
        return px if px > 0 else spot

    px = _f(levels.get("take_profit_1"), 0.0)
    return px if px > 0 else spot


def _apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    if price <= 0:
        return 0.0
    slip = float(slippage_bps) / 10000.0
    if side.upper() == "BUY":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def _lot_qty(cash: float, price: float, pct: float, lot_size: int) -> int:
    if cash <= 0 or price <= 0 or pct <= 0:
        return 0
    raw = cash * pct / price
    lots = math.floor(raw / max(1, lot_size))
    return int(lots * max(1, lot_size))


def _order_fee_tax(value: float, side: str, cfg: PaperTradeConfig) -> tuple[float, float]:
    fee = value * cfg.trade.commission_bps / 10000.0
    tax = value * cfg.trade.stamp_duty_bps / 10000.0 if side.upper() == "SELL" else 0.0
    return fee, tax


def _can_sell_today(pos_row: Any, as_of_date: str) -> bool:
    if not pos_row:
        return False
    t1 = pos_row["t1_sellable_date"]
    if not t1:
        return True
    return str(t1) <= as_of_date


def _decide_order(
    decision: DecisionResult,
    *,
    ticker: str,
    market: str,
    as_of_date: str,
    cfg: PaperTradeConfig,
    position_row: Any,
    cash: float,
    buy_count_today: int,
    dry_run: bool,
    bundle: dict[str, Any],
) -> dict[str, Any] | None:
    qty_pos = _f(position_row["quantity"], 0.0) if position_row else 0.0

    if qty_pos > 0:
        if decision.score_final <= cfg.thresholds.force_exit or decision.action == "AVOID":
            if not _can_sell_today(position_row, as_of_date):
                return {
                    "type": "HOLD",
                    "why": "T+1 未到，今日不可卖",
                }
            side = "SELL"
            qty = int(qty_pos)
            px_ref = _choose_trade_price(bundle, side)
            px = _apply_slippage(px_ref, side, cfg.trade.slippage_bps)
            if px <= 0 or qty <= 0:
                return {"type": "HOLD", "why": "无有效卖出价格"}
            value = px * qty
            fee, tax = _order_fee_tax(value, side, cfg)
            return {
                "type": "ORDER",
                "side": side,
                "qty": qty,
                "price": px,
                "fee": fee,
                "tax": tax,
                "reason": "score 低于强制退出阈值" if decision.score_final <= cfg.thresholds.force_exit else "决策回避，减风险",
            }
        return {"type": "HOLD", "why": "已有持仓且未触发退出"}

    if decision.action != "PAPER_BUY_A":
        return {"type": "HOLD", "why": f"action={decision.action}"}

    if buy_count_today >= cfg.trade.max_new_positions_per_cycle:
        return {"type": "HOLD", "why": "当日新开仓数量已达上限"}

    side = "BUY"
    px_ref = _choose_trade_price(bundle, side)
    px = _apply_slippage(px_ref, side, cfg.trade.slippage_bps)
    qty = _lot_qty(cash, px, cfg.trade.default_position_pct, cfg.trade.lot_size)
    if qty <= 0:
        return {"type": "HOLD", "why": "可用现金不足以开一手"}

    value = px * qty
    fee, tax = _order_fee_tax(value, side, cfg)
    required = value + fee + tax
    if required > cash:
        qty = _lot_qty(cash * 0.98, px, cfg.trade.default_position_pct, cfg.trade.lot_size)
        if qty <= 0:
            return {"type": "HOLD", "why": "开仓金额超过可用现金"}
        value = px * qty
        fee, tax = _order_fee_tax(value, side, cfg)
        required = value + fee + tax
        if required > cash:
            return {"type": "HOLD", "why": "现金仍不足"}

    if dry_run:
        return {
            "type": "SIM_ONLY",
            "side": side,
            "qty": qty,
            "price": px,
            "fee": fee,
            "tax": tax,
            "reason": "dry-run 模式，仅生成建议不落单",
        }

    return {
        "type": "ORDER",
        "side": side,
        "qty": qty,
        "price": px,
        "fee": fee,
        "tax": tax,
        "reason": "A 级信号开仓",
    }


def run_cycle(
    tickers: list[str],
    *,
    cfg: PaperTradeConfig,
    as_of_date: str | None = None,
    no_resume: bool = False,
    from_cache_only: bool = False,
    dry_run: bool = False,
    live_return_pct: float | None = None,
    realtime_quote_overlay: bool | None = None,
    watcher_overlay: bool | None = None,
    session_id: str | None = None,
    loop_no: int = 1,
    lease_ttl_ms: int | None = None,
) -> dict[str, Any]:
    if not tickers:
        raise ValueError("tickers 不能为空")

    as_of_date = as_of_date or _today()
    if realtime_quote_overlay is None:
        realtime_quote_overlay = bool(cfg.realtime.quote_overlay)
    if watcher_overlay is None:
        watcher_overlay = bool(cfg.realtime.watcher_overlay)
    watcher_overlay = bool(watcher_overlay and cfg.watcher.enabled)

    conn = connect(cfg.db_path)
    init_db(conn, cfg.trade.initial_cash)

    run_id = start_job_run(conn, tickers, cfg.depth)
    loop_id = run_id
    runtime_session_id = str(session_id or run_id)
    event_batch_id = f"eb_{run_id[:16]}"
    summary_rows: list[dict[str, Any]] = []
    latest_prices: dict[str, float] = {}
    quote_freshness_values: list[int] = []
    analysis_freshness_values: list[int] = []
    lease_ttl = int(lease_ttl_ms if lease_ttl_ms is not None else DEFAULT_RUNTIME_LEASE_TTL_MS)
    release_lease_on_exit = session_id is None

    lease_result = acquire_runtime_lease(
        conn,
        lease_name=DEFAULT_RUNTIME_LEASE_NAME,
        owner_session_id=runtime_session_id,
        ttl_ms=lease_ttl,
    )
    if not lease_result.acquired:
        err = (
            "runtime lease not acquired: "
            f"owner_session_id={lease_result.owner_session_id} "
            f"expires_at_ms={lease_result.lease_expires_at_ms}"
        )
        finish_job_run(conn, run_id, "failed", error=err)
        conn.commit()
        conn.close()
        raise RuntimeError(err)

    create_runtime_loop(
        conn,
        loop_id=loop_id,
        session_id=runtime_session_id,
        loop_no=int(loop_no),
        status="started",
        decision_version="papertrade-v2",
        event_batch_id=event_batch_id,
    )
    enqueue_outbox_event(
        conn,
        event_type="loop.started",
        entity_type="loop",
        entity_id=loop_id,
        source="runtime_loop",
        severity="info",
        event_batch_id=event_batch_id,
        correlation_id=f"crr_{loop_id}",
        payload={
            "loop_id": loop_id,
            "session_id": runtime_session_id,
            "loop_no": int(loop_no),
            "as_of_date": as_of_date,
            "ticker_count": len(tickers),
        },
    )
    conn.commit()

    try:
        for raw_ticker in tickers:
            ti = parse_ticker(raw_ticker)
            ticker = ti.full
            market = ti.market
            buy_count = count_new_buys_today(conn, as_of_date)
            cash = get_cash(conn)

            bundle = analyze_ticker(
                ticker,
                depth=cfg.depth,
                no_resume=no_resume,
                from_cache_only=from_cache_only,
            )
            if bundle.get("status") != "ok":
                msg = f"分析失败/跳过: {bundle.get('status')}"
                payload = {
                    "ts": _today(),
                    "as_of_date": as_of_date,
                    "ticker": ticker,
                    "action": "SKIP",
                    "reason": msg,
                    "stage1": bundle.get("stage1", {}),
                }
                emit_alert(
                    conn,
                    cfg,
                    run_id=run_id,
                    as_of_date=as_of_date,
                    ticker=ticker,
                    level="warning",
                    title=f"[SKIP] {ticker}",
                    body=msg,
                    payload=payload,
                )
                summary_rows.append({"ticker": ticker, "action": "SKIP", "note": msg})
                enqueue_outbox_event(
                    conn,
                    event_type="loop.ticker.skipped",
                    entity_type="loop",
                    entity_id=loop_id,
                    source="runtime_loop",
                    severity="warning",
                    event_batch_id=event_batch_id,
                    correlation_id=f"crr_{loop_id}",
                    payload={
                        "loop_id": loop_id,
                        "ticker": ticker,
                        "reason": msg,
                    },
                )
                conn.commit()
                continue

            # normal path
            analysis_freshness_values.append(
                analysis_freshness_ms_from_bundle(
                    bundle,
                    from_cache_only=bool(from_cache_only),
                )
            )
            market = bundle.get("market", market)
            pos = get_position(conn, ticker)
            quote_snapshot: dict[str, Any] | None = None

            if realtime_quote_overlay:
                snapshot = fetch_realtime_snapshot(
                    ticker,
                    timeout_seconds=getattr(cfg.realtime, "quote_timeout_seconds", None),
                )
                quote_snapshot = snapshot
                try:
                    record_quote_snapshot(
                        conn,
                        ticker=ticker,
                        snapshot=snapshot,
                        quote_batch_id=f"qbatch_{loop_id[:12]}_{parse_ticker(ticker).code}",
                        source="runtime_quote_overlay",
                        request_context={
                            "run_id": run_id,
                            "loop_id": loop_id,
                            "session_id": runtime_session_id,
                            "overlay": "realtime_quote",
                        },
                    )
                except Exception as e:
                    quote_snapshot = {**snapshot, "persist_error": str(e)}
                q_age = quote_freshness_ms_from_snapshot(snapshot)
                if q_age is not None:
                    quote_freshness_values.append(int(q_age))
                inject_snapshot_into_bundle(bundle, snapshot)

            decision = evaluate_decision(
                ticker,
                market=market,
                panel=bundle.get("panel") or {},
                strategy_signals=bundle.get("strategy_signals") or {},
                synthesis=bundle.get("synthesis") or {},
                review_issues=bundle.get("review_issues"),
                cfg=cfg,
            )
            if watcher_overlay:
                decision = apply_watcher_overlay(
                    decision,
                    bundle=bundle,
                    position_row=pos,
                    cfg=cfg,
                )

            syn = bundle.get("synthesis") or {}
            panel_mode = str((bundle.get("panel") or {}).get("panel_mode") or "investor_panel")
            signal_id = record_signal(
                conn,
                run_id=run_id,
                as_of_date=as_of_date,
                ticker=ticker,
                market=market,
                panel_mode=panel_mode,
                action=decision.action,
                score_final=decision.score_final,
                score_strategy=decision.score_strategy,
                score_panel=decision.score_panel,
                score_tactical=decision.score_tactical,
                score_core=decision.score_core,
                bonus_agent=decision.bonus_agent,
                penalties=decision.penalties,
                gates=decision.gates,
                summary=decision.summary,
            )
            enqueue_outbox_event(
                conn,
                event_type="signal.generated",
                entity_type="signal",
                entity_id=str(signal_id),
                source="runtime_loop",
                severity="info",
                event_batch_id=event_batch_id,
                correlation_id=f"crr_{loop_id}",
                causation_id=loop_id,
                payload={
                    "signal_id": signal_id,
                    "loop_id": loop_id,
                    "ticker": ticker,
                    "action": decision.action,
                    "score_final": round(float(decision.score_final), 4),
                },
            )

            px_spot = _f((((bundle.get("raw") or {}).get("dimensions", {}).get("0_basic") or {}).get("data") or {}).get("price"), 0.0)
            if px_spot > 0:
                latest_prices[ticker] = px_spot

            order_plan = _decide_order(
                decision,
                ticker=ticker,
                market=market,
                as_of_date=as_of_date,
                cfg=cfg,
                position_row=pos,
                cash=cash,
                buy_count_today=buy_count,
                dry_run=dry_run,
                bundle=bundle,
            )

            order_info: dict[str, Any] | None = None
            intent_row: dict[str, Any] | None = None
            if order_plan and order_plan.get("type") in {"ORDER", "SIM_ONLY"}:
                intent_side = str(order_plan.get("side") or "").upper()
                intent_qty = int(_f(order_plan.get("qty"), 0.0))
                if intent_side in {"BUY", "SELL"} and intent_qty > 0:
                    idem_key = (
                        f"auto:{run_id}:{ticker}:{decision.action}:{intent_side}:{intent_qty}:"
                        f"{str(order_plan.get('type') or '')}:{as_of_date}"
                    )
                    basic_data = (((bundle.get("raw") or {}).get("dimensions") or {}).get("0_basic") or {}).get("data") or {}
                    instrument_name = str(basic_data.get("name") or "")
                    cmd_result = submit_intent_order(
                        conn,
                        run_id=run_id,
                        as_of_date=as_of_date,
                        ticker=ticker,
                        market=market,
                        source="auto",
                        idempotency_key=idem_key,
                        side=intent_side,
                        qty=intent_qty,
                        price=_f(order_plan.get("price"), 0.0),
                        action=decision.action,
                        order_reason=str(order_plan.get("reason") or ""),
                        signal_id=signal_id,
                        loop_id=run_id,
                        operator_id="runtime_loop",
                        operator_channel="loop",
                        sim_only=order_plan.get("type") == "SIM_ONLY",
                        instrument_name=instrument_name,
                        decision_basis="cached_overlay" if from_cache_only else "full",
                        fee=_f(order_plan.get("fee"), 0.0),
                        tax=_f(order_plan.get("tax"), 0.0),
                        lot_size=cfg.trade.lot_size,
                        commission_bps=cfg.trade.commission_bps,
                        stamp_duty_bps=cfg.trade.stamp_duty_bps,
                        slippage_bps=cfg.trade.slippage_bps,
                        policy_snapshot={
                            "score_final": decision.score_final,
                            "gates": decision.gates,
                            "penalties": decision.penalties,
                        },
                        source_snapshot={
                            "verdict": syn.get("verdict_label"),
                            "panel_mode": panel_mode,
                        },
                    )
                    intent_row = cmd_result.get("intent")
                    order_info = cmd_result.get("order_info")
                    if not cmd_result.get("accepted", False) and order_plan.get("type") == "ORDER":
                        order_info = order_info or {
                            "intent_id": intent_row.get("intent_id") if intent_row else None,
                            "intent_status": intent_row.get("status") if intent_row else None,
                            "rejected": True,
                            "reject_code": "RULE_REJECTED",
                            "reject_reason": "command rejected by pre-trade checks",
                        }
                        # auto path falls back to hold semantic for rejected orders.
                        order_info.setdefault("hold", True)
                        order_info.setdefault("reason", order_info.get("reject_reason") or "command rejected")
            else:
                order_info = {"hold": True, "reason": (order_plan or {}).get("why", "")}
            if order_info is None:
                order_info = {"hold": True, "reason": (order_plan or {}).get("why", "")}

            alert_payload = make_alert_payload(
                ticker=ticker,
                action=decision.action,
                score_final=decision.score_final,
                summary=decision.summary,
                order=order_info,
                as_of_date=as_of_date,
            )
            emit_alert(
                conn,
                cfg,
                run_id=run_id,
                as_of_date=as_of_date,
                ticker=ticker,
                level="info",
                title=f"[{decision.action}] {ticker} · {decision.score_final:.1f}",
                body=f"verdict={decision.summary.get('verdict_label')} · panel={decision.summary.get('panel_mode')}",
                payload=alert_payload,
            )

            summary_rows.append(
                {
                    "ticker": ticker,
                    "action": decision.action,
                    "score_final": decision.score_final,
                    "signal_id": signal_id,
                    "intent_id": intent_row.get("intent_id") if intent_row else None,
                    "intent_status": intent_row.get("status") if intent_row else None,
                    "order": order_info,
                    "verdict": decision.summary.get("verdict_label"),
                    "panel_mode": decision.summary.get("panel_mode"),
                    "short_bias": decision.summary.get("short_bias"),
                    "watcher_overlay": decision.summary.get("watcher_overlay"),
                    "quote_snapshot": quote_snapshot,
                    "quote_overlay_applied": bool(
                        (((bundle.get("_papertrade_runtime") or {}).get("quote_overlay_applied")))
                    ),
                    "gates": decision.gates,
                    "penalties": decision.penalties,
                }
            )
            conn.commit()

        db_write_t0 = time.perf_counter()
        mark_to_market(conn, as_of_date, latest_prices)
        nav = record_nav(
            conn,
            run_id,
            as_of_date,
            cfg.trade.initial_cash,
            session_id=runtime_session_id,
            loop_id=loop_id,
        )
        drift = update_deviation_daily(conn, as_of_date, live_return_pct=live_return_pct)
        db_write_duration_ms = int((time.perf_counter() - db_write_t0) * 1000)

        failed_count = sum(1 for r in summary_rows if str(r.get("action") or "").upper() == "SKIP")
        success_count = max(0, len(summary_rows) - failed_count)
        loop_metrics = build_loop_metrics(
            conn,
            summary_rows=summary_rows,
            from_cache_only=bool(from_cache_only),
            db_write_duration_ms=db_write_duration_ms,
            quote_freshness_values=quote_freshness_values,
            analysis_freshness_values=analysis_freshness_values,
        )

        enqueue_outbox_event(
            conn,
            event_type="nav.updated",
            entity_type="portfolio_nav",
            entity_id=str(as_of_date),
            source="runtime_loop",
            severity="info",
            event_batch_id=event_batch_id,
            correlation_id=f"crr_{loop_id}",
            causation_id=loop_id,
            payload={
                "loop_id": loop_id,
                "as_of_date": as_of_date,
                "nav": nav,
                "drift": drift,
            },
        )
        enqueue_outbox_event(
            conn,
            event_type="loop.finished",
            entity_type="loop",
            entity_id=loop_id,
            source="runtime_loop",
            severity="info",
            event_batch_id=event_batch_id,
            correlation_id=f"crr_{loop_id}",
            payload={
                "loop_id": loop_id,
                "status": "completed",
                "ticker_count": len(tickers),
                "success_count": success_count,
                "failed_count": failed_count,
                "db_write_duration_ms": db_write_duration_ms,
                "metrics": loop_metrics,
            },
        )
        finish_runtime_loop(
            conn,
            loop_id=loop_id,
            status="completed",
            ticker_count=len(tickers),
            success_count=success_count,
            failed_count=failed_count,
            db_write_duration_ms=db_write_duration_ms,
            event_batch_id=event_batch_id,
            decision_basis_summary={"from_cache_only": bool(from_cache_only)},
            staleness_summary={
                "quote_freshness_ms": loop_metrics.get("quote_freshness_ms"),
                "analysis_freshness_ms": loop_metrics.get("analysis_freshness_ms"),
            },
            metrics=loop_metrics,
        )
        conn.commit()

        result = {
            "run_id": run_id,
            "loop_id": loop_id,
            "event_batch_id": event_batch_id,
            "session_id": runtime_session_id,
            "as_of_date": as_of_date,
            "depth": cfg.depth,
            "tickers": tickers,
            "rows": summary_rows,
            "nav": nav,
            "drift": drift,
            "from_cache_only": from_cache_only,
            "dry_run": dry_run,
            "realtime_quote_overlay": realtime_quote_overlay,
            "watcher_overlay": watcher_overlay,
        }
        append_jsonl(cfg.run_logs_jsonl, result)
        finish_job_run(conn, run_id, "ok", note="cycle completed")
        conn.commit()
        return result

    except Exception as e:
        conn.rollback()
        enqueue_outbox_event(
            conn,
            event_type="loop.failed",
            entity_type="loop",
            entity_id=loop_id,
            source="runtime_loop",
            severity="error",
            event_batch_id=event_batch_id,
            correlation_id=f"crr_{loop_id}",
            payload={
                "loop_id": loop_id,
                "status": "failed",
                "error": str(e),
            },
        )
        finish_runtime_loop(
            conn,
            loop_id=loop_id,
            status="failed",
            ticker_count=len(tickers),
            success_count=0,
            failed_count=len(tickers),
            error_json={"error": str(e)},
            event_batch_id=event_batch_id,
            decision_basis_summary={"from_cache_only": bool(from_cache_only)},
            staleness_summary={},
        )
        finish_job_run(conn, run_id, "failed", error=str(e))
        conn.commit()
        raise
    finally:
        if release_lease_on_exit:
            try:
                released = release_runtime_lease(
                    conn,
                    lease_name=DEFAULT_RUNTIME_LEASE_NAME,
                    owner_session_id=runtime_session_id,
                )
                if released:
                    conn.commit()
            except Exception:
                conn.rollback()
        conn.close()


def _parse_tickers(raw: str) -> list[str]:
    out = []
    for x in (raw or "").split(","):
        x = x.strip()
        if x:
            out.append(x)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="UZI paper-trade cycle runner")
    ap.add_argument("--tickers", required=True, help="逗号分隔，例如 600519.SH,002273.SZ")
    ap.add_argument("--depth", choices=["lite", "medium", "deep"], default="medium")
    ap.add_argument("--as-of-date", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--config", default=None, help="可选 JSON 配置文件")
    ap.add_argument("--mode", choices=["short", "swing"], default=None, help="覆盖策略模式")
    ap.add_argument("--no-resume", action="store_true", help="强制 stage1 重抓")
    ap.add_argument("--from-cache-only", action="store_true", help="仅使用 .cache 现有输出，不重新跑 stage1/stage2")
    ap.add_argument("--dry-run", action="store_true", help="只出建议，不写模拟订单/成交")
    ap.add_argument("--live-return-pct", type=float, default=None, help="可选：人工实盘当日收益率(%%)")
    ap.add_argument("--quote-timeout-seconds", type=float, default=None, help="实时行情覆盖单票限时秒数，0 表示不启用限时保护")
    ap.add_argument(
        "--realtime-quote-overlay",
        action="store_const",
        const=True,
        default=None,
        help="启用实时行情快照覆盖（会用最新价覆盖 bundle.price）",
    )
    ap.add_argument(
        "--watcher-overlay",
        action="store_const",
        const=True,
        default=None,
        help="启用模拟盯盘人物（趋势/风控/均值回归）对动作做实时修正",
    )
    args = ap.parse_args()

    overrides: dict[str, Any] = {"depth": args.depth}
    if args.mode:
        overrides["policy"] = {"mode": args.mode}
    if args.quote_timeout_seconds is not None:
        overrides["realtime"] = {"quote_timeout_seconds": args.quote_timeout_seconds}

    cfg = load_config(args.config, overrides=overrides)
    tickers = _parse_tickers(args.tickers)
    if not tickers:
        raise SystemExit("--tickers 不能为空")

    res = run_cycle(
        tickers,
        cfg=cfg,
        as_of_date=args.as_of_date,
        no_resume=args.no_resume,
        from_cache_only=args.from_cache_only,
        dry_run=args.dry_run,
        live_return_pct=args.live_return_pct,
        realtime_quote_overlay=args.realtime_quote_overlay,
        watcher_overlay=args.watcher_overlay,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
