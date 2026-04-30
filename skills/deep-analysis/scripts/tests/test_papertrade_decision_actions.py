from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.config import load_config  # noqa: E402
from papertrade.decision_policy import evaluate_decision  # noqa: E402


def _cfg():
    return load_config()


def _panel(consensus: float, *, bullish: int = 1, neutral: int = 1, bearish: int = 1) -> dict:
    return {
        "panel_consensus": consensus,
        "panel_mode": "investor_panel",
        "signal_distribution": {"bullish": bullish, "neutral": neutral, "bearish": bearish, "skip": 0},
        "investors": [],
    }


def _strategy(*signals: str) -> dict:
    return {
        "signals": [
            {
                "strategy_id": f"s{idx}",
                "signal": signal,
                "strength": 100.0,
                "confidence": 100.0,
                "regime_fit": 100.0,
            }
            for idx, signal in enumerate(signals, start=1)
        ]
    }


def _synthesis(
    *,
    overall: float,
    setup: float,
    bias: str = "中性",
    agent_reviewed: bool = False,
) -> dict:
    return {
        "overall_score": overall,
        "verdict_label": "pytest",
        "agent_reviewed": agent_reviewed,
        "short_trading": {
            "setup_score": setup,
            "bias": bias,
            "quick_stats": {"intraday_micro_available": True},
        },
    }


def test_decision_policy_keeps_buy_action_for_true_buy_signal():
    res = evaluate_decision(
        "600519.SH",
        market="A",
        panel=_panel(90.0, bullish=30, bearish=3),
        strategy_signals=_strategy("bullish", "bullish", "bullish"),
        synthesis=_synthesis(overall=90.0, setup=90.0, bias="偏多", agent_reviewed=True),
        review_issues=None,
        cfg=_cfg(),
    )

    assert res.action == "PAPER_BUY_A"
    assert res.summary["action_layer"]["family"] == "trade"


def test_decision_policy_uses_candidate_state_instead_of_legacy_observe():
    res = evaluate_decision(
        "600519.SH",
        market="A",
        panel=_panel(55.0, bullish=12, bearish=8),
        strategy_signals=_strategy("neutral"),
        synthesis=_synthesis(overall=55.0, setup=55.0, agent_reviewed=True),
        review_issues=None,
        cfg=_cfg(),
    )

    assert res.action == "CANDIDATE_A"
    assert res.summary["action_layer"]["reason"] == "score_above_observe"


def test_decision_policy_splits_wait_states_from_no_trade_avoid():
    breakout = evaluate_decision(
        "600519.SH",
        market="A",
        panel=_panel(50.0, bullish=10, bearish=8),
        strategy_signals=_strategy("neutral"),
        synthesis=_synthesis(overall=50.0, setup=60.0, bias="偏多", agent_reviewed=False),
        review_issues=None,
        cfg=_cfg(),
    )
    pullback = evaluate_decision(
        "600519.SH",
        market="A",
        panel=_panel(50.0, bullish=8, bearish=8),
        strategy_signals=_strategy("neutral"),
        synthesis=_synthesis(overall=60.0, setup=50.0, bias="中性", agent_reviewed=False),
        review_issues=None,
        cfg=_cfg(),
    )
    no_trade = evaluate_decision(
        "600519.SH",
        market="A",
        panel=_panel(40.0, bullish=8, bearish=8),
        strategy_signals=_strategy("neutral"),
        synthesis=_synthesis(overall=40.0, setup=30.0, bias="中性", agent_reviewed=False),
        review_issues=None,
        cfg=_cfg(),
    )

    assert breakout.action == "BREAKOUT_WAIT"
    assert pullback.action == "PULLBACK_WAIT"
    assert no_trade.action == "NO_TRADE_AVOID"


def test_decision_policy_reserves_avoid_for_hard_bearish():
    res = evaluate_decision(
        "600519.SH",
        market="A",
        panel=_panel(40.0, bullish=2, bearish=12),
        strategy_signals=_strategy("bearish", "bearish", "bearish", "neutral"),
        synthesis=_synthesis(overall=40.0, setup=30.0, bias="偏空", agent_reviewed=False),
        review_issues=None,
        cfg=_cfg(),
    )

    assert res.action == "AVOID"
    assert res.summary["action_layer"]["reason"] == "hard_bearish_below_force_exit"
