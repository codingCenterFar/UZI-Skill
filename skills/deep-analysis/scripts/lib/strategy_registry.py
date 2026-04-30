"""Strategy registry for the A-share 12-family strategy engine.

Phase 0 goal:
- define a stable contract for strategy IDs and depth gating
- allow stage1/stage2 to merge strategy outputs without breaking report flow
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    family: str
    title: str
    enabled_depths: frozenset[str]
    requires_a_share: bool = False
    phase_hint: str = "phase1"


STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("trend_momentum", "trend", "Trend / Momentum", frozenset({"lite", "medium", "deep"})),
    StrategySpec("reversal_mean_revert", "reversal", "Reversal / Mean Revert", frozenset({"lite", "medium", "deep"}), requires_a_share=True),
    StrategySpec("flow_turnover_behavior", "flow", "Volume / Turnover / Flow", frozenset({"lite", "medium", "deep"}), requires_a_share=True),
    StrategySpec("low_vol_risk_management", "risk", "Low Vol / Risk Management", frozenset({"medium", "deep"})),
    StrategySpec("value_repair", "value", "Value / Valuation Repair", frozenset({"medium", "deep"})),
    StrategySpec("quality_improvement", "quality", "Quality / Fundamental Improvement", frozenset({"medium", "deep"})),
    StrategySpec("event_drift", "event", "Event Driven", frozenset({"medium", "deep"}), requires_a_share=True),
    StrategySpec("limit_up_ecology", "limit_up", "Limit-Up Ecology", frozenset({"medium", "deep"}), requires_a_share=True),
    StrategySpec("intraday_timing", "intraday", "Intraday / Auction / Close", frozenset({"deep"}), requires_a_share=True),
    StrategySpec("calendar_seasonality", "calendar", "Calendar / Seasonality", frozenset({"deep"}), requires_a_share=True),
    StrategySpec("industry_style_rotation", "rotation", "Industry / Style Rotation", frozenset({"medium", "deep"}), requires_a_share=True),
    StrategySpec("pair_relative_strength", "pair", "Pair / Relative Strength", frozenset({"deep"}), requires_a_share=True),
)


def all_strategies() -> tuple[StrategySpec, ...]:
    return STRATEGY_SPECS


def enabled_strategies(depth: str) -> list[StrategySpec]:
    depth = (depth or "medium").lower()
    return [s for s in STRATEGY_SPECS if depth in s.enabled_depths]


def strategy_ids(depth: str) -> list[str]:
    return [s.strategy_id for s in enabled_strategies(depth)]

