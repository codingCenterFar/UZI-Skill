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
    StrategySpec("trend_momentum", "trend", "趋势/动量", frozenset({"lite", "medium", "deep"})),
    StrategySpec("reversal_mean_revert", "reversal", "反转/均值回复", frozenset({"lite", "medium", "deep"}), requires_a_share=True),
    StrategySpec("flow_turnover_behavior", "flow", "成交量/换手/资金行为", frozenset({"lite", "medium", "deep"}), requires_a_share=True),
    StrategySpec("low_vol_risk_management", "risk", "低波/风险管理", frozenset({"medium", "deep"})),
    StrategySpec("value_repair", "value", "价值/估值修复", frozenset({"medium", "deep"})),
    StrategySpec("quality_improvement", "quality", "质量/基本面改善", frozenset({"medium", "deep"})),
    StrategySpec("event_drift", "event", "事件驱动", frozenset({"medium", "deep"}), requires_a_share=True),
    StrategySpec("limit_up_ecology", "limit_up", "涨停/连板生态", frozenset({"medium", "deep"}), requires_a_share=True),
    StrategySpec("intraday_timing", "intraday", "日内/竞价/尾盘", frozenset({"deep"}), requires_a_share=True),
    StrategySpec("calendar_seasonality", "calendar", "日历/季节效应", frozenset({"deep"}), requires_a_share=True),
    StrategySpec("industry_style_rotation", "rotation", "行业/风格/主题轮动", frozenset({"medium", "deep"}), requires_a_share=True),
    StrategySpec("pair_relative_strength", "pair", "配对/相对强弱", frozenset({"deep"}), requires_a_share=True),
)


def all_strategies() -> tuple[StrategySpec, ...]:
    return STRATEGY_SPECS


def enabled_strategies(depth: str) -> list[StrategySpec]:
    depth = (depth or "medium").lower()
    return [s for s in STRATEGY_SPECS if depth in s.enabled_depths]


def strategy_ids(depth: str) -> list[str]:
    return [s.strategy_id for s in enabled_strategies(depth)]
