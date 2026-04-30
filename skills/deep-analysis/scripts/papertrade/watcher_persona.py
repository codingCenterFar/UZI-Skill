from __future__ import annotations

from dataclasses import replace
from typing import Any

from papertrade.config import PaperTradeConfig
from papertrade.decision_policy import (
    ACTION_AVOID,
    ACTION_BREAKOUT_WAIT,
    ACTION_CANDIDATE_A,
    ACTION_NO_TRADE_AVOID,
    ACTION_PAPER_BUY_A,
    ACTION_PAPER_WATCH_B,
    ACTION_PULLBACK_WAIT,
    DecisionResult,
)


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _action_by_score(score: float, cfg: PaperTradeConfig) -> str:
    th = cfg.thresholds
    if score >= th.buy_a:
        return ACTION_PAPER_BUY_A
    if score >= th.watch_b:
        return ACTION_PAPER_WATCH_B
    if score >= th.observe:
        return ACTION_CANDIDATE_A
    if score >= th.force_exit:
        return ACTION_PULLBACK_WAIT
    return ACTION_NO_TRADE_AVOID


def _extract_context(bundle: dict[str, Any]) -> dict[str, Any]:
    syn = bundle.get("synthesis") or {}
    short_trading = syn.get("short_trading") or ((syn.get("friendly") or {}).get("short_trading") or {})
    strategy_signals = bundle.get("strategy_signals") or {}
    sig_map = {str(s.get("strategy_id") or ""): s for s in (strategy_signals.get("signals") or [])}
    intraday_signal = str((sig_map.get("intraday_timing") or {}).get("signal") or "neutral").lower()
    basic = (((bundle.get("raw") or {}).get("dimensions") or {}).get("0_basic") or {}).get("data") or {}
    return {
        "setup_score": _f(short_trading.get("setup_score"), 50.0),
        "bias": str(short_trading.get("bias") or ""),
        "micro_ok": bool((short_trading.get("quick_stats") or {}).get("intraday_micro_available")),
        "intraday_signal": intraday_signal,
        "change_pct": _f(basic.get("change_pct"), 0.0),
    }


def _position_pnl_pct(position_row: Any, spot_price: float) -> float:
    if not position_row:
        return 0.0
    qty = _f(position_row["quantity"], 0.0)
    avg = _f(position_row["avg_cost"], 0.0)
    if qty <= 0 or avg <= 0 or spot_price <= 0:
        return 0.0
    return (spot_price / avg - 1.0) * 100.0


def apply_watcher_overlay(
    decision: DecisionResult,
    *,
    bundle: dict[str, Any],
    position_row: Any,
    cfg: PaperTradeConfig,
) -> DecisionResult:
    if not cfg.watcher.enabled:
        return decision

    ctx = _extract_context(bundle)
    has_pos = bool(position_row and _f(position_row["quantity"], 0.0) > 0)
    spot = _f((((bundle.get("raw") or {}).get("dimensions") or {}).get("0_basic") or {}).get("data", {}).get("price"), 0.0)
    pnl_pct = _position_pnl_pct(position_row, spot)

    votes: list[dict[str, Any]] = []
    delta_total = 0.0
    force_avoid = False

    intraday_sig = ctx["intraday_signal"]
    bias = ctx["bias"]
    trend_delta = 0.0
    trend_signal = "neutral"
    trend_why = "日内方向未形成明显一致预期"
    if intraday_sig == "bullish" and bias != "偏空":
        trend_delta = 3.0
        trend_signal = "bullish"
        trend_why = f"趋势交易员偏多：intraday={intraday_sig}, bias={bias or '中性'}"
    elif intraday_sig == "bearish" or bias == "偏空":
        trend_delta = -3.0
        trend_signal = "bearish"
        trend_why = f"趋势交易员转空：intraday={intraday_sig}, bias={bias or '中性'}"
    votes.append({"persona": "trend_trader", "signal": trend_signal, "delta": trend_delta, "reason": trend_why})
    delta_total += trend_delta

    risk_delta = 0.0
    risk_signal = "neutral"
    risk_why = "未触发止损/止盈风控"
    if has_pos and pnl_pct <= float(cfg.watcher.stop_loss_floor_pct):
        risk_delta = -8.0
        risk_signal = "bearish"
        risk_why = f"风控官触发止损：浮盈亏 {pnl_pct:.2f}% <= {cfg.watcher.stop_loss_floor_pct:.2f}%"
        force_avoid = True
    elif has_pos and pnl_pct >= float(cfg.watcher.take_profit_hint_pct) and intraday_sig == "bearish":
        risk_delta = -5.0
        risk_signal = "bearish"
        risk_why = f"风控官建议落袋：浮盈 {pnl_pct:.2f}% 且 intraday 转空"
    elif decision.action == ACTION_PAPER_BUY_A and not ctx["micro_ok"]:
        risk_delta = -3.0
        risk_signal = "bearish"
        risk_why = "风控官降速：日内微结构数据不可用"
    votes.append({"persona": "risk_guard", "signal": risk_signal, "delta": risk_delta, "reason": risk_why})
    delta_total += risk_delta

    mean_delta = 0.0
    mean_signal = "neutral"
    mean_why = "波动不极端，均值回归员保持中性"
    move = _f(ctx["change_pct"], 0.0)
    extreme = float(cfg.watcher.extreme_move_pct)
    if move <= -extreme and not has_pos and decision.action in (
        ACTION_CANDIDATE_A,
        ACTION_PULLBACK_WAIT,
        ACTION_BREAKOUT_WAIT,
        ACTION_PAPER_WATCH_B,
    ):
        mean_delta = 2.0
        mean_signal = "bullish"
        mean_why = f"均值回归员试探抄底：当日跌幅 {move:.2f}%"
    elif move >= extreme and has_pos:
        mean_delta = -2.0
        mean_signal = "bearish"
        mean_why = f"均值回归员建议减仓：当日涨幅 {move:.2f}%"
    votes.append({"persona": "mean_reverter", "signal": mean_signal, "delta": mean_delta, "reason": mean_why})
    delta_total += mean_delta

    delta_total = _clip(delta_total, -abs(float(cfg.watcher.max_abs_score_delta)), abs(float(cfg.watcher.max_abs_score_delta)))
    score_new = _clip(decision.score_final + delta_total, 0.0, 100.0)
    action_new = _action_by_score(score_new, cfg)

    if force_avoid and has_pos:
        action_new = ACTION_AVOID
        score_new = min(score_new, float(cfg.thresholds.force_exit))

    penalties = list(decision.penalties or [])
    if abs(delta_total) > 1e-8:
        penalties.append(
            {
                "key": "watcher_persona_overlay",
                "delta": round(delta_total, 3),
                "why": "模拟盯盘人物对决策做了实时修正",
            }
        )

    gates = list(decision.gates or [])
    if action_new != decision.action:
        gates.append(
            {
                "key": "watcher_action_shift",
                "triggered": True,
                "downgrade_to": action_new,
                "why": f"watcher_overlay 将动作从 {decision.action} 调整为 {action_new}",
            }
        )

    summary = dict(decision.summary or {})
    summary["watcher_overlay"] = {
        "enabled": True,
        "score_delta": round(delta_total, 3),
        "score_before": round(decision.score_final, 3),
        "score_after": round(score_new, 3),
        "action_before": decision.action,
        "action_after": action_new,
        "position_pnl_pct": round(pnl_pct, 3),
        "votes": votes,
    }

    return replace(
        decision,
        action=action_new,
        score_final=round(score_new, 3),
        penalties=penalties,
        gates=gates,
        summary=summary,
    )
