from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WeightConfig:
    strategy_edge: float = 0.35
    panel_score: float = 0.25
    tactical_score: float = 0.25
    core_score: float = 0.15


@dataclass
class ThresholdConfig:
    buy_a: float = 75.0
    watch_b: float = 65.0
    observe: float = 55.0
    force_exit: float = 45.0


@dataclass
class TradeConfig:
    initial_cash: float = 1_000_000.0
    default_position_pct: float = 0.10
    max_position_pct: float = 0.30
    max_new_positions_per_cycle: int = 3
    lot_size: int = 100
    commission_bps: float = 2.0
    stamp_duty_bps: float = 10.0
    slippage_bps: float = 5.0


@dataclass
class PolicyConfig:
    mode: str = "short"  # short / swing
    require_agent_review_for_buy_a: bool = False
    enforce_intraday_gate_for_buy_a: bool = True
    intraday_setup_floor: float = 72.0
    downgrade_basket_mode_from_buy_a: bool = True


@dataclass
class RealtimeConfig:
    poll_seconds: int = 60
    max_loops: int = 0
    session_seconds: int = 0
    refresh_every_loops: int = 5
    lease_ttl_seconds: int = 900
    sleep_on_error_seconds: int = 15
    quote_timeout_seconds: float = 8.0
    quote_overlay: bool = False
    watcher_overlay: bool = False


@dataclass
class WatcherConfig:
    enabled: bool = True
    max_abs_score_delta: float = 12.0
    stop_loss_floor_pct: float = -3.0
    take_profit_hint_pct: float = 5.0
    extreme_move_pct: float = 4.0


@dataclass
class PaperTradeConfig:
    depth: str = "medium"
    cache_root: Path = Path(".cache")
    db_path: Path = Path(".cache/paper_trade/paper.db")
    alerts_jsonl: Path = Path(".cache/paper_trade/alerts.jsonl")
    run_logs_jsonl: Path = Path(".cache/paper_trade/run_logs.jsonl")
    weights: WeightConfig = field(default_factory=WeightConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    trade: TradeConfig = field(default_factory=TradeConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    realtime: RealtimeConfig = field(default_factory=RealtimeConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cache_root"] = str(self.cache_root)
        payload["db_path"] = str(self.db_path)
        payload["alerts_jsonl"] = str(self.alerts_jsonl)
        payload["run_logs_jsonl"] = str(self.run_logs_jsonl)
        return payload


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _coerce_config(raw: dict[str, Any]) -> PaperTradeConfig:
    w = WeightConfig(**raw.get("weights", {}))
    t = ThresholdConfig(**raw.get("thresholds", {}))
    tr = TradeConfig(**raw.get("trade", {}))
    p = PolicyConfig(**raw.get("policy", {}))
    rt = RealtimeConfig(**raw.get("realtime", {}))
    wr = WatcherConfig(**raw.get("watcher", {}))
    return PaperTradeConfig(
        depth=raw.get("depth", "medium"),
        cache_root=Path(raw.get("cache_root", ".cache")),
        db_path=Path(raw.get("db_path", ".cache/paper_trade/paper.db")),
        alerts_jsonl=Path(raw.get("alerts_jsonl", ".cache/paper_trade/alerts.jsonl")),
        run_logs_jsonl=Path(raw.get("run_logs_jsonl", ".cache/paper_trade/run_logs.jsonl")),
        weights=w,
        thresholds=t,
        trade=tr,
        policy=p,
        realtime=rt,
        watcher=wr,
    )


def load_config(config_path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> PaperTradeConfig:
    cfg = PaperTradeConfig()
    payload = cfg.as_dict()

    if config_path:
        p = Path(config_path)
        if p.exists():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            payload = _merge_dict(payload, loaded)

    if overrides:
        payload = _merge_dict(payload, overrides)

    return _coerce_config(payload)


if __name__ == "__main__":
    print(json.dumps(load_config().as_dict(), ensure_ascii=False, indent=2))
