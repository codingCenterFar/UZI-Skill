from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from papertrade.config import PaperTradeConfig

ACTION_PAPER_BUY_A = "PAPER_BUY_A"
ACTION_PAPER_WATCH_B = "PAPER_WATCH_B"
ACTION_CANDIDATE_A = "CANDIDATE_A"
ACTION_PULLBACK_WAIT = "PULLBACK_WAIT"
ACTION_BREAKOUT_WAIT = "BREAKOUT_WAIT"
ACTION_NO_TRADE_AVOID = "NO_TRADE_AVOID"
ACTION_AVOID = "AVOID"


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _sign(v: float) -> int:
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


@dataclass
class DecisionResult:
    ticker: str
    action: str
    score_final: float
    score_strategy: float
    score_panel: float
    score_tactical: float
    score_core: float
    bonus_agent: float
    penalties: list[dict[str, Any]]
    gates: list[dict[str, Any]]
    summary: dict[str, Any]


def _group_mean(investors: list[dict], groups: set[str], fallback: float) -> float:
    vals: list[float] = []
    for inv in investors:
        if inv.get("group") not in groups:
            continue
        if str(inv.get("signal") or "").lower() == "skip":
            continue
        score = _f(inv.get("score"), 0.0)
        conf = _f(inv.get("confidence"), 0.0)
        vals.append(score * conf / 100.0)
    if not vals:
        return float(fallback)
    return sum(vals) / len(vals)


def _strategy_edge(signals_doc: dict) -> tuple[float, dict[str, int]]:
    signals = (signals_doc or {}).get("signals") or []
    scored = []
    dist = {"bullish": 0, "bearish": 0, "neutral": 0, "skip": 0}
    for s in signals:
        sig = str(s.get("signal") or "neutral").lower()
        dist[sig if sig in dist else "neutral"] = dist.get(sig if sig in dist else "neutral", 0) + 1
        if sig == "skip":
            continue
        pol = 1.0 if sig == "bullish" else -1.0 if sig == "bearish" else 0.0
        strength = _f(s.get("strength"), 50.0)
        conf = _f(s.get("confidence"), 50.0)
        regime = _f(s.get("regime_fit"), 50.0)
        scored.append(pol * strength * (conf / 100.0) * (regime / 100.0))

    avg = sum(scored) / len(scored) if scored else 0.0
    return 50.0 + _clip(avg, -25.0, 25.0), dist


def _extract_short_trading(syn: dict) -> dict:
    st = syn.get("short_trading") or {}
    if st:
        return st
    return ((syn.get("friendly") or {}).get("short_trading") or {})


def _detect_data_gaps(review_issues: dict | None) -> tuple[bool, list[str]]:
    if not isinstance(review_issues, dict):
        return False, []
    out = []
    for i in review_issues.get("issues", []) or []:
        if not isinstance(i, dict):
            continue
        if i.get("category") == "data" and i.get("severity") in ("critical", "warning"):
            out.append(str(i.get("dim") or i.get("issue") or "unknown"))
    return len(out) > 0, out[:8]


def _sig_count(dist: dict[str, Any], key: str) -> float:
    return _f((dist or {}).get(key), 0.0)


def _dominant_bearish(
    *,
    strat_dist: dict[str, int],
    panel_dist: dict[str, Any],
    bias: str,
) -> bool:
    strategy_bearish = _sig_count(strat_dist, "bearish") >= _sig_count(strat_dist, "bullish") + 3
    panel_bearish = _sig_count(panel_dist, "bearish") >= _sig_count(panel_dist, "bullish") + 5
    return bool(strategy_bearish or panel_bearish or bias == "偏空")


def _has_breakout_setup(
    *,
    short_trading: dict[str, Any],
    strat_dist: dict[str, int],
    bias: str,
) -> bool:
    setup_score = _f(short_trading.get("setup_score"), 50.0)
    bullish_ok = _sig_count(strat_dist, "bullish") >= _sig_count(strat_dist, "bearish")
    return bool(setup_score >= 60.0 and bullish_ok and bias != "偏空")


def _has_pullback_setup(
    *,
    score_core: float,
    panel_consensus: float,
    agent_reviewed: bool,
) -> bool:
    return bool(score_core >= 55.0 or panel_consensus >= 55.0 or agent_reviewed)


def classify_action_layer(
    *,
    score_final: float,
    cfg: PaperTradeConfig,
    short_trading: dict[str, Any],
    strat_dist: dict[str, int],
    panel_dist: dict[str, Any],
    bias: str,
    score_core: float,
    panel_consensus: float,
    agent_reviewed: bool,
) -> dict[str, Any]:
    th = cfg.thresholds
    score = float(score_final)
    dominant_bearish = _dominant_bearish(strat_dist=strat_dist, panel_dist=panel_dist, bias=bias)
    breakout_setup = _has_breakout_setup(short_trading=short_trading, strat_dist=strat_dist, bias=bias)
    pullback_setup = _has_pullback_setup(
        score_core=score_core,
        panel_consensus=panel_consensus,
        agent_reviewed=agent_reviewed,
    )

    if score >= th.buy_a:
        return {
            "action": ACTION_PAPER_BUY_A,
            "family": "trade",
            "reason": "score_above_buy_a",
        }
    if score >= th.watch_b:
        return {
            "action": ACTION_PAPER_WATCH_B,
            "family": "watch",
            "reason": "score_above_watch_b",
        }
    if score >= th.observe:
        return {
            "action": ACTION_CANDIDATE_A,
            "family": "candidate",
            "reason": "score_above_observe",
        }

    if score < th.force_exit and dominant_bearish:
        return {
            "action": ACTION_AVOID,
            "family": "avoid",
            "reason": "hard_bearish_below_force_exit",
        }
    if breakout_setup and score >= th.force_exit:
        return {
            "action": ACTION_BREAKOUT_WAIT,
            "family": "wait",
            "reason": "setup_positive_wait_breakout",
        }
    if pullback_setup and score >= th.force_exit:
        return {
            "action": ACTION_PULLBACK_WAIT,
            "family": "wait",
            "reason": "quality_or_reviewed_wait_pullback",
        }

    return {
        "action": ACTION_NO_TRADE_AVOID,
        "family": "avoid",
        "reason": "no_trade_not_enough_edge",
    }


def evaluate_decision(
    ticker: str,
    *,
    market: str,
    panel: dict,
    strategy_signals: dict,
    synthesis: dict,
    review_issues: dict | None,
    cfg: PaperTradeConfig,
) -> DecisionResult:
    investors = panel.get("investors") or []
    panel_consensus = _f(panel.get("panel_consensus"), 50.0)
    panel_mode = str(panel.get("panel_mode") or "investor_panel")

    s_strategy, strat_dist = _strategy_edge(strategy_signals or {})

    dfg = _group_mean(investors, {"D", "F", "G"}, fallback=panel_consensus)
    cgrp = _group_mean(investors, {"C"}, fallback=panel_consensus)
    abe = _group_mean(investors, {"A", "B", "E"}, fallback=panel_consensus)

    s_panel_short = 0.60 * dfg + 0.20 * cgrp + 0.20 * abe
    s_panel_swing = 0.50 * abe + 0.35 * dfg + 0.15 * cgrp
    s_panel = s_panel_short if cfg.policy.mode == "short" else s_panel_swing

    short_trading = _extract_short_trading(synthesis or {})
    s_tactical = _f(short_trading.get("setup_score"), 50.0)
    s_core = _f(synthesis.get("overall_score"), 50.0)

    agent_reviewed = bool(synthesis.get("agent_reviewed"))
    bonus_agent = 4.0 if agent_reviewed else -3.0

    penalties: list[dict[str, Any]] = []

    strategy_dir = _sign(s_strategy - 50.0)
    panel_dir = _sign(s_panel - 50.0)
    mismatch = strategy_dir != 0 and panel_dir != 0 and strategy_dir != panel_dir
    if mismatch:
        penalties.append({"key": "strategy_panel_mismatch", "delta": -8.0, "why": "策略层与评委层方向冲突"})

    if strat_dist.get("bearish", 0) >= strat_dist.get("bullish", 0) + 3:
        penalties.append({"key": "strategy_bearish_dominant", "delta": -6.0, "why": "策略看空数量明显更高"})

    sig_dist = panel.get("signal_distribution") or {}
    if _f(sig_dist.get("bearish"), 0) >= _f(sig_dist.get("bullish"), 0) + 5:
        penalties.append({"key": "panel_bearish_dominant", "delta": -6.0, "why": "评委看空人数显著更多"})

    bias = str(short_trading.get("bias") or "")
    if bias == "偏空":
        penalties.append({"key": "short_bias_bearish", "delta": -5.0, "why": "短线节奏偏空"})

    penalty_total = sum(_f(p.get("delta")) for p in penalties)

    w = cfg.weights
    s_final = (
        w.strategy_edge * s_strategy
        + w.panel_score * s_panel
        + w.tactical_score * s_tactical
        + w.core_score * s_core
        + bonus_agent
        + penalty_total
    )
    s_final = _clip(s_final, 0.0, 100.0)

    sig_dist = panel.get("signal_distribution") or {}
    action_layer = classify_action_layer(
        score_final=s_final,
        cfg=cfg,
        short_trading=short_trading,
        strat_dist=strat_dist,
        panel_dist=sig_dist,
        bias=bias,
        score_core=s_core,
        panel_consensus=panel_consensus,
        agent_reviewed=agent_reviewed,
    )
    action = str(action_layer["action"])

    gates: list[dict[str, Any]] = []

    has_gaps, gap_dims = _detect_data_gaps(review_issues)
    if has_gaps and action == "PAPER_BUY_A":
        gates.append({
            "key": "data_gap_gate",
            "triggered": True,
            "downgrade_to": ACTION_CANDIDATE_A,
            "why": f"存在数据缺口: {', '.join(gap_dims)}",
        })
        action = ACTION_CANDIDATE_A
        action_layer = {
            "action": action,
            "family": "candidate",
            "reason": "data_gap_downgrade_from_buy",
        }

    if cfg.policy.require_agent_review_for_buy_a and action == ACTION_PAPER_BUY_A and not agent_reviewed:
        gates.append({
            "key": "agent_review_gate",
            "triggered": True,
            "downgrade_to": ACTION_PAPER_WATCH_B,
            "why": "未检测到 agent_reviewed=true",
        })
        action = ACTION_PAPER_WATCH_B
        action_layer = {
            "action": action,
            "family": "watch",
            "reason": "agent_review_downgrade_from_buy",
        }

    if cfg.policy.enforce_intraday_gate_for_buy_a and action == ACTION_PAPER_BUY_A:
        sig_map = {str(s.get("strategy_id") or ""): s for s in (strategy_signals.get("signals") or [])}
        intraday_sig = str((sig_map.get("intraday_timing") or {}).get("signal") or "neutral")
        quick_stats = short_trading.get("quick_stats") or {}
        micro_ok = bool(quick_stats.get("intraday_micro_available"))
        setup_ok = _f(short_trading.get("setup_score"), 0.0) >= float(cfg.policy.intraday_setup_floor)
        if intraday_sig == "bearish" or (not micro_ok) or (not setup_ok):
            gates.append({
                "key": "intraday_quality_gate",
                "triggered": True,
                "downgrade_to": ACTION_PAPER_WATCH_B,
                "why": f"intraday_sig={intraday_sig}, micro_ok={micro_ok}, setup_ok={setup_ok}",
            })
            action = ACTION_PAPER_WATCH_B
            action_layer = {
                "action": action,
                "family": "watch",
                "reason": "intraday_gate_downgrade_from_buy",
            }

    if cfg.policy.downgrade_basket_mode_from_buy_a and action == ACTION_PAPER_BUY_A and panel_mode == "basket_strategy":
        gates.append({
            "key": "basket_mode_gate",
            "triggered": True,
            "downgrade_to": ACTION_PAPER_WATCH_B,
            "why": "ETF/LOF 策略篮子模式不走 51 评委个股方法论",
        })
        action = ACTION_PAPER_WATCH_B
        action_layer = {
            "action": action,
            "family": "watch",
            "reason": "basket_mode_downgrade_from_buy",
        }

    summary = {
        "verdict_label": synthesis.get("verdict_label"),
        "panel_mode": panel_mode,
        "panel_consensus": round(panel_consensus, 2),
        "strategy_dist": strat_dist,
        "panel_signal_dist": sig_dist,
        "short_bias": bias,
        "agent_reviewed": agent_reviewed,
        "panel_insights": synthesis.get("panel_insights", ""),
        "action_layer": action_layer,
    }

    return DecisionResult(
        ticker=ticker,
        action=action,
        score_final=round(s_final, 3),
        score_strategy=round(s_strategy, 3),
        score_panel=round(s_panel, 3),
        score_tactical=round(s_tactical, 3),
        score_core=round(s_core, 3),
        bonus_agent=round(bonus_agent, 3),
        penalties=penalties,
        gates=gates,
        summary=summary,
    )
