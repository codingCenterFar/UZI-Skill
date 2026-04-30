"""A-share constraint-aware strategy backtest framework (T304).

Usage:
  python strategy_backtest.py --depth deep --save
  python strategy_backtest.py --depth medium --limit 60 --save
  python strategy_backtest.py --depth deep --tickers 600519.SH,002273.SZ --save
"""
from __future__ import annotations

import argparse
import copy
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Iterable

from lib.cache import read_task_output
from lib.strategy_engine import build_strategy_outputs


def _f(v, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("%", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return default


def _dedup_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


def _resolve_cache_roots(cli_cache_root: str | None = None) -> list[Path]:
    if cli_cache_root:
        return [Path(cli_cache_root)]
    script_cache = Path(__file__).resolve().parent / ".cache"
    cwd_cache = Path(".cache")
    return _dedup_paths([cwd_cache, script_cache])


def _iter_cached_tickers(cache_roots: list[Path]) -> list[str]:
    out: set[str] = set()
    for root in cache_roots:
        if not root.exists():
            continue
        for p in root.iterdir():
            if not p.is_dir() or p.name.startswith("_"):
                continue
            if (p / "raw_data.json").exists():
                out.add(p.name)
    return sorted(out)


def _load_doc(ticker: str, name: str, cache_roots: list[Path]) -> dict:
    doc = read_task_output(ticker, name)
    if doc:
        return doc
    file_name = f"{name}.json"
    for root in cache_roots:
        p = root / ticker / file_name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def _candles(raw: dict) -> list[dict]:
    out = ((raw.get("dimensions", {}).get("2_kline") or {}).get("data") or {}).get("candles_60d") or []
    return out if isinstance(out, list) else []


def _close(c: dict, default: float = 0.0) -> float:
    return _f(c.get("close"), default=default) if isinstance(c, dict) else default


def _open(c: dict, default: float = 0.0) -> float:
    if not isinstance(c, dict):
        return default
    op = _f(c.get("open"), default=0.0)
    if op > 0:
        return op
    return _close(c, default=default)


def _high(c: dict, default: float = 0.0) -> float:
    if not isinstance(c, dict):
        return default
    hi = _f(c.get("high"), default=0.0)
    return hi if hi > 0 else _close(c, default=default)


def _safe_date(raw: object) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "·" in s:
        s = s.split("·", 1)[0].strip()
    if len(s) >= 10:
        s = s[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


def _candle_date(c: dict) -> datetime | None:
    if not isinstance(c, dict):
        return None
    for key in ("date", "day", "time", "datetime"):
        dt = _safe_date(c.get(key))
        if dt is not None:
            return dt
    return None


def _slice_rows_by_date(rows: list, current_dt: datetime | None) -> list:
    if not isinstance(rows, list):
        return []
    if current_dt is None:
        return rows
    out = []
    for r in rows:
        if isinstance(r, dict):
            dt = None
            for key in ("日期", "date", "day", "time", "datetime"):
                dt = _safe_date(r.get(key))
                if dt is not None:
                    break
            if dt is None:
                title_dt = _safe_date(r.get("title") or r.get("event"))
                if title_dt is not None:
                    dt = title_dt
            if dt is None or dt <= current_dt:
                out.append(r)
        else:
            dt = _safe_date(r)
            if dt is None or dt <= current_dt:
                out.append(r)
    return out


def _slice_raw_for_idx(raw: dict, idx: int) -> dict:
    """Create no-lookahead snapshot for day idx.

    Important:
    - K-line is truncated to [:idx+1].
    - Event/flow style fields are truncated by date to avoid lookahead.
    """
    snap = copy.deepcopy(raw)
    dims = snap.get("dimensions", {})
    kline = (dims.get("2_kline") or {}).get("data") or {}
    candles = kline.get("candles_60d") or []
    trunc = candles[: idx + 1]
    kline["candles_60d"] = trunc
    current_dt = _candle_date(trunc[-1]) if trunc else None

    # Avoid lookahead leakage from rolling/event lists in backtest snapshots.
    cap = (dims.get("12_capital_flow") or {}).get("data") or {}
    cap["main_fund_flow_20d"] = _slice_rows_by_date(cap.get("main_fund_flow_20d") or [], current_dt)
    cap["institutional_history"] = _slice_rows_by_date(cap.get("institutional_history") or [], current_dt)
    cap["unlock_schedule"] = _slice_rows_by_date(cap.get("unlock_schedule") or [], current_dt)
    ev = (dims.get("15_events") or {}).get("data") or {}
    ev["event_timeline"] = _slice_rows_by_date(ev.get("event_timeline") or [], current_dt)
    ev["recent_news"] = _slice_rows_by_date(ev.get("recent_news") or [], current_dt)
    ev["recent_notices"] = _slice_rows_by_date(ev.get("recent_notices") or [], current_dt)
    ev["catalyst"] = _slice_rows_by_date(ev.get("catalyst") or [], current_dt)
    lhb = (dims.get("16_lhb") or {}).get("data") or {}
    lhb["lhb_records"] = _slice_rows_by_date(lhb.get("lhb_records") or [], current_dt)
    lhb["matched_youzi_detail"] = _slice_rows_by_date(lhb.get("matched_youzi_detail") or [], current_dt)
    return snap


def _horizon_days(horizon: str) -> int:
    hz = (horizon or "").strip().lower()
    if hz == "intraday":
        return 1
    if hz == "swing":
        return 5
    return 20


def _is_limit_down(candles: list[dict], idx: int, limit_down_pct: float) -> bool:
    if idx <= 0 or idx >= len(candles):
        return False
    p0 = _close(candles[idx - 1], default=0.0)
    p1 = _close(candles[idx], default=0.0)
    if p0 <= 0 or p1 <= 0:
        return False
    chg = (p1 / p0 - 1) * 100
    return chg <= -abs(limit_down_pct)


def _simulate_long_trade(
    candles: list[dict],
    signal_idx: int,
    hold_days: int,
    cfg: dict,
) -> dict:
    entry_idx = signal_idx + 1  # T+1 entry
    if entry_idx >= len(candles):
        return {"status": "skip_no_forward"}

    prev_close = _close(candles[signal_idx], default=0.0)
    entry_open = _open(candles[entry_idx], default=0.0)
    if prev_close <= 0 or entry_open <= 0:
        return {"status": "skip_bad_price"}

    gap_up_pct = (entry_open / prev_close - 1) * 100
    if gap_up_pct >= cfg["limit_up_pct"]:
        return {"status": "unfilled_limit_up", "entry_idx": entry_idx, "gap_up_pct": round(gap_up_pct, 2)}

    exit_idx = min(entry_idx + hold_days - 1, len(candles) - 1)
    exit_extend_days = 0
    while exit_idx < len(candles) - 1 and _is_limit_down(candles, exit_idx, cfg["limit_down_pct"]):
        exit_idx += 1
        exit_extend_days += 1

    exit_close = _close(candles[exit_idx], default=0.0)
    if exit_close <= 0:
        return {"status": "skip_bad_price"}

    slip = cfg["slippage_bps"] / 10000.0
    entry_exec = entry_open * (1.0 + slip)
    exit_exec = exit_close * (1.0 - slip)

    gross_ret_pct = (exit_exec / entry_exec - 1.0) * 100.0
    cost_pct = (cfg["commission_bps"] * 2 + cfg["stamp_duty_bps"]) / 10000.0 * 100.0
    net_ret_pct = gross_ret_pct - cost_pct

    hold_slice = candles[entry_idx : exit_idx + 1]
    min_low = min((_f(c.get("low"), default=_close(c, default=entry_exec)) for c in hold_slice), default=entry_exec)
    max_dd_pct = (min_low / entry_exec - 1.0) * 100.0 if entry_exec > 0 else 0.0

    return {
        "status": "filled",
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "hold_days": max(1, exit_idx - entry_idx + 1),
        "exit_extend_days": exit_extend_days,
        "gap_up_pct": round(gap_up_pct, 2),
        "entry_open": round(entry_open, 6),
        "exit_close": round(exit_close, 6),
        "gross_ret_pct": round(gross_ret_pct, 3),
        "net_ret_pct": round(net_ret_pct, 3),
        "max_drawdown_pct": round(max_dd_pct, 3),
    }


def _evaluate_bearish_hit(candles: list[dict], signal_idx: int, hold_days: int) -> dict:
    entry_idx = signal_idx + 1
    if entry_idx >= len(candles):
        return {"status": "skip_no_forward"}
    exit_idx = min(entry_idx + hold_days - 1, len(candles) - 1)
    entry_open = _open(candles[entry_idx], default=0.0)
    exit_close = _close(candles[exit_idx], default=0.0)
    if entry_open <= 0 or exit_close <= 0:
        return {"status": "skip_bad_price"}
    fwd_ret_pct = (exit_close / entry_open - 1.0) * 100.0
    return {
        "status": "ok",
        "forward_ret_pct": round(fwd_ret_pct, 3),
        "hit": fwd_ret_pct <= 0.0,
        "hold_days": max(1, exit_idx - entry_idx + 1),
    }


def _trade_net_ret(entry_open: float, exit_close: float, scenario: dict) -> float:
    if entry_open <= 0 or exit_close <= 0:
        return 0.0
    slip = _f(scenario.get("slippage_bps"), 0.0) / 10000.0
    entry_exec = entry_open * (1.0 + slip)
    exit_exec = exit_close * (1.0 - slip)
    gross = (exit_exec / entry_exec - 1.0) * 100.0
    cost_pct = (
        _f(scenario.get("commission_bps"), 0.0) * 2.0
        + _f(scenario.get("stamp_duty_bps"), 0.0)
    ) / 10000.0 * 100.0
    return gross - cost_pct


def _cost_sensitivity(
    trades_by_strategy: dict[str, list[dict]],
    scenarios: dict[str, dict],
) -> dict:
    summary_by_scenario: dict[str, dict] = {}
    per_strategy: dict[str, dict] = {}
    for name, sc in scenarios.items():
        all_rets: list[float] = []
        wins = 0
        for sid, trs in trades_by_strategy.items():
            vals = [_trade_net_ret(_f(t.get("entry_open")), _f(t.get("exit_close")), sc) for t in trs]
            vals = [v for v in vals if abs(v) < 1e6]
            if not vals:
                continue
            all_rets.extend(vals)
            wins += sum(1 for v in vals if v > 0)
            p = per_strategy.setdefault(sid, {})
            p[name] = {
                "avg_net_ret_pct": round(mean(vals), 3),
                "win_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                "filled_trades": len(vals),
            }
        summary_by_scenario[name] = {
            "filled_trades": len(all_rets),
            "avg_net_ret_pct": round(mean(all_rets), 3) if all_rets else 0.0,
            "win_rate_pct": round(wins / max(len(all_rets), 1) * 100, 1),
        }

    delta_vs_base: list[dict] = []
    for sid, row in per_strategy.items():
        base = ((row.get("base") or {}).get("avg_net_ret_pct")) if isinstance(row.get("base"), dict) else None
        high = ((row.get("high") or {}).get("avg_net_ret_pct")) if isinstance(row.get("high"), dict) else None
        low = ((row.get("low") or {}).get("avg_net_ret_pct")) if isinstance(row.get("low"), dict) else None
        if base is None:
            continue
        delta_vs_base.append(
            {
                "strategy_id": sid,
                "base_avg_net_ret_pct": round(_f(base), 3),
                "high_cost_avg_net_ret_pct": round(_f(high), 3),
                "low_cost_avg_net_ret_pct": round(_f(low), 3),
                "delta_high_vs_base_pct": round(_f(high) - _f(base), 3),
                "delta_low_vs_base_pct": round(_f(low) - _f(base), 3),
                "filled_trades": int(((row.get("base") or {}).get("filled_trades")) or 0),
            }
        )

    delta_vs_base = sorted(delta_vs_base, key=lambda x: x["delta_high_vs_base_pct"])[:10]
    return {
        "scenarios": scenarios,
        "summary_by_scenario": summary_by_scenario,
        "strategy_delta_vs_base": delta_vs_base,
    }


def _walk_forward_summary(
    trades_by_strategy: dict[str, list[dict]],
    base_scenario: dict,
) -> dict:
    rows: list[dict] = []
    stable = unstable = insufficient = 0
    for sid, trades in sorted(trades_by_strategy.items()):
        if len(trades) < 12:
            insufficient += 1
            continue
        ordered = sorted(
            trades,
            key=lambda t: str(t.get("signal_date") or ""),
        )
        vals = [
            _trade_net_ret(_f(t.get("entry_open")), _f(t.get("exit_close")), base_scenario)
            for t in ordered
        ]
        n = len(vals)
        cut1 = max(6, int(n * 0.6))
        cut2 = max(cut1 + 3, int(n * 0.75))
        fold1_test = vals[cut1:]
        fold2_test = vals[cut2:]
        f1_avg = mean(fold1_test) if fold1_test else 0.0
        f2_avg = mean(fold2_test) if fold2_test else 0.0
        f1_wr = sum(1 for v in fold1_test if v > 0) / max(len(fold1_test), 1) * 100.0
        f2_wr = sum(1 for v in fold2_test if v > 0) / max(len(fold2_test), 1) * 100.0

        if f1_avg > 0 and f2_avg > 0 and f1_wr >= 52 and f2_wr >= 50:
            label = "stable"
            stable += 1
        elif f1_avg < 0 or f2_avg < 0:
            label = "unstable"
            unstable += 1
        else:
            label = "mixed"

        rows.append(
            {
                "strategy_id": sid,
                "samples": n,
                "fold1_test_avg_net_ret_pct": round(f1_avg, 3),
                "fold1_test_win_rate_pct": round(f1_wr, 1),
                "fold2_test_avg_net_ret_pct": round(f2_avg, 3),
                "fold2_test_win_rate_pct": round(f2_wr, 1),
                "stability": label,
            }
        )

    rows = sorted(rows, key=lambda x: (x["stability"] != "stable", -x["fold2_test_avg_net_ret_pct"]))[:20]
    return {
        "strategies_evaluated": len(rows),
        "stable_count": stable,
        "unstable_count": unstable,
        "insufficient_count": insufficient,
        "rows": rows,
    }


def _summarize_strategy(rows: dict[str, Counter], ret_map: dict[str, list[float]], dd_map: dict[str, list[float]]) -> dict:
    out: dict[str, dict] = {}
    for sid in sorted(rows):
        c = rows[sid]
        net_rets = ret_map.get(sid) or []
        dds = dd_map.get(sid) or []
        bullish_filled = int(c.get("bullish_filled", 0) or 0)
        bullish_wins = int(c.get("bullish_wins", 0) or 0)
        bearish_eval = int(c.get("bearish_evaluated", 0) or 0)
        bearish_hits = int(c.get("bearish_hits", 0) or 0)
        out[sid] = {
            "bullish_signals": int(c.get("bullish_signals", 0) or 0),
            "bullish_filled": bullish_filled,
            "unfilled_limit_up": int(c.get("unfilled_limit_up", 0) or 0),
            "skip_no_forward": int(c.get("skip_no_forward", 0) or 0),
            "bullish_win_rate_pct": round(bullish_wins / max(bullish_filled, 1) * 100, 1),
            "bullish_avg_net_ret_pct": round(mean(net_rets), 3) if net_rets else 0.0,
            "bullish_median_net_ret_pct": round(sorted(net_rets)[len(net_rets) // 2], 3) if net_rets else 0.0,
            "bullish_avg_max_dd_pct": round(mean(dds), 3) if dds else 0.0,
            "bearish_signals": int(c.get("bearish_signals", 0) or 0),
            "bearish_hit_rate_pct": round(bearish_hits / max(bearish_eval, 1) * 100, 1),
            "samples": int(c.get("samples", 0) or 0),
        }
    return out


def run_backtest(
    depth: str,
    cache_roots: list[Path],
    limit: int | None = None,
    tickers: set[str] | None = None,
    warmup_days: int = 25,
    strength_floor: float = 55.0,
) -> dict:
    universe = _iter_cached_tickers(cache_roots)
    if tickers:
        universe = [t for t in universe if t in tickers]
    if limit and limit > 0:
        universe = universe[:limit]

    cfg = {
        "warmup_days": warmup_days,
        "strength_floor": strength_floor,
        "limit_up_pct": 9.5,
        "limit_down_pct": 9.5,
        "commission_bps": 2.0,
        "stamp_duty_bps": 10.0,  # 卖出单边印花税
        "slippage_bps": 5.0,
        "t_plus_one": True,
    }
    scenarios = {
        "low": {"commission_bps": 1.0, "stamp_duty_bps": 5.0, "slippage_bps": 2.0},
        "base": {
            "commission_bps": cfg["commission_bps"],
            "stamp_duty_bps": cfg["stamp_duty_bps"],
            "slippage_bps": cfg["slippage_bps"],
        },
        "high": {"commission_bps": 3.0, "stamp_duty_bps": 10.0, "slippage_bps": 12.0},
    }

    rows: dict[str, Counter] = defaultdict(Counter)
    ret_map: dict[str, list[float]] = defaultdict(list)
    dd_map: dict[str, list[float]] = defaultdict(list)
    trades_by_strategy: dict[str, list[dict]] = defaultdict(list)
    regime_rows: dict[str, Counter] = defaultdict(Counter)
    sample_trades: list[dict] = []

    processed = 0
    skipped_non_a = 0
    skipped_no_candles = 0

    for ticker in universe:
        raw = _load_doc(ticker, "raw_data", cache_roots)
        if not raw:
            continue
        market = str(raw.get("market") or "A")
        if market != "A":
            skipped_non_a += 1
            continue

        candles = _candles(raw)
        if len(candles) < warmup_days + 5:
            skipped_no_candles += 1
            continue
        dims = _load_doc(ticker, "dimensions", cache_roots) or {}
        processed += 1

        max_hold = 20
        end_idx = len(candles) - max_hold - 1
        if end_idx <= warmup_days:
            continue
        for idx in range(warmup_days, end_idx + 1):
            snap = _slice_raw_for_idx(raw, idx)
            try:
                sf, ss, _ = build_strategy_outputs(ticker, snap, dims, depth)
            except Exception:
                continue

            regime = str((sf.get("regime") or {}).get("market_regime") or "unknown")
            for sig in ss.get("signals") or []:
                sid = str(sig.get("strategy_id") or "unknown")
                signal = str(sig.get("signal") or "neutral")
                strength = _f(sig.get("strength"), 0.0)
                if signal not in ("bullish", "bearish") or strength < strength_floor:
                    continue
                hold_days = _horizon_days(str(sig.get("horizon") or "position"))
                rows[sid]["samples"] += 1

                if signal == "bullish":
                    rows[sid]["bullish_signals"] += 1
                    tr = _simulate_long_trade(candles, idx, hold_days, cfg)
                    status = tr.get("status")
                    if status == "filled":
                        rows[sid]["bullish_filled"] += 1
                        rows[sid]["regime_" + regime + "_filled"] += 1
                        regime_rows[regime]["filled"] += 1
                        net_ret = _f(tr.get("net_ret_pct"), 0.0)
                        ret_map[sid].append(net_ret)
                        dd_map[sid].append(_f(tr.get("max_drawdown_pct"), 0.0))
                        regime_rows[regime]["net_ret_sum"] += net_ret
                        if net_ret > 0:
                            rows[sid]["bullish_wins"] += 1
                            regime_rows[regime]["wins"] += 1
                        sig_dt = _candle_date(candles[idx])
                        trades_by_strategy[sid].append(
                            {
                                "ticker": ticker,
                                "signal_date": sig_dt.strftime("%Y-%m-%d") if sig_dt else "",
                                "signal_idx": idx,
                                "entry_open": _f(tr.get("entry_open"), 0.0),
                                "exit_close": _f(tr.get("exit_close"), 0.0),
                                "hold_days": int(tr.get("hold_days") or 0),
                                "max_drawdown_pct": _f(tr.get("max_drawdown_pct"), 0.0),
                                "regime": regime,
                            }
                        )
                        if len(sample_trades) < 120:
                            sample_trades.append(
                                {
                                    "ticker": ticker,
                                    "strategy_id": sid,
                                    "signal": "bullish",
                                    "signal_idx": idx,
                                    "signal_date": sig_dt.strftime("%Y-%m-%d") if sig_dt else "",
                                    "regime": regime,
                                    "strength": round(strength, 1),
                                    "horizon": sig.get("horizon"),
                                    "net_ret_pct": tr.get("net_ret_pct"),
                                    "gross_ret_pct": tr.get("gross_ret_pct"),
                                    "max_drawdown_pct": tr.get("max_drawdown_pct"),
                                    "exit_extend_days": tr.get("exit_extend_days", 0),
                                }
                            )
                    elif status == "unfilled_limit_up":
                        rows[sid]["unfilled_limit_up"] += 1
                    elif status == "skip_no_forward":
                        rows[sid]["skip_no_forward"] += 1
                    else:
                        rows[sid]["skip_bad_price"] += 1
                else:
                    rows[sid]["bearish_signals"] += 1
                    br = _evaluate_bearish_hit(candles, idx, hold_days)
                    if br.get("status") != "ok":
                        rows[sid]["skip_no_forward"] += 1
                        continue
                    rows[sid]["bearish_evaluated"] += 1
                    if br.get("hit"):
                        rows[sid]["bearish_hits"] += 1

    by_strategy = _summarize_strategy(rows, ret_map, dd_map)

    by_regime = {}
    for rg, c in sorted(regime_rows.items()):
        filled = int(c.get("filled", 0) or 0)
        by_regime[rg] = {
            "filled_trades": filled,
            "win_rate_pct": round(c.get("wins", 0) / max(filled, 1) * 100, 1),
            "avg_net_ret_pct": round(c.get("net_ret_sum", 0.0) / max(filled, 1), 3),
        }

    effective: list[dict] = []
    fragile: list[dict] = []
    for sid, row in by_strategy.items():
        filled = int(row.get("bullish_filled", 0) or 0)
        if filled < 10:
            continue
        win_rate = _f(row.get("bullish_win_rate_pct"), 0.0)
        avg_ret = _f(row.get("bullish_avg_net_ret_pct"), 0.0)
        summary = {
            "strategy_id": sid,
            "filled_trades": filled,
            "win_rate_pct": round(win_rate, 1),
            "avg_net_ret_pct": round(avg_ret, 3),
        }
        if win_rate >= 55.0 and avg_ret > 0:
            effective.append(summary)
        elif win_rate <= 45.0 or avg_ret < 0:
            fragile.append(summary)

    effective = sorted(effective, key=lambda x: (-x["win_rate_pct"], -x["avg_net_ret_pct"]))[:8]
    fragile = sorted(fragile, key=lambda x: (x["avg_net_ret_pct"], x["win_rate_pct"]))[:8]
    cost_sensitivity = _cost_sensitivity(trades_by_strategy, scenarios)
    walk_forward = _walk_forward_summary(trades_by_strategy, scenarios["base"])

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "depth": depth,
        "config": cfg,
        "cost_scenarios": scenarios,
        "universe_size": len(universe),
        "processed_tickers": processed,
        "skipped_non_a": skipped_non_a,
        "skipped_no_candles": skipped_no_candles,
        "cache_roots": [str(p) for p in cache_roots],
        "by_strategy": by_strategy,
        "by_market_regime": by_regime,
        "walk_forward": walk_forward,
        "cost_sensitivity": cost_sensitivity,
        "strategy_effectiveness": {
            "effective": effective,
            "fragile": fragile,
            "note": (
                "A-share constrained long-side backtest. "
                "Applied T+1 entry, limit-up unfilled, limit-down delayed exit, "
                "commission/stamp duty/slippage, plus walk-forward and cost-sensitivity snapshots."
            ),
        },
        "sample_trades": sample_trades,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="A-share constraint-aware strategy backtest")
    ap.add_argument("--depth", default="deep", choices=["lite", "medium", "deep"])
    ap.add_argument("--limit", type=int, default=0, help="max cached tickers (0=all)")
    ap.add_argument("--cache-root", default="", help="cache root path")
    ap.add_argument("--tickers", default="", help="comma-separated tickers")
    ap.add_argument("--warmup-days", type=int, default=25)
    ap.add_argument("--strength-floor", type=float, default=55.0)
    ap.add_argument("--save", action="store_true", help="save JSON report to .cache/_global")
    ap.add_argument("--save-path", default="", help="optional save path")
    args = ap.parse_args()

    cache_roots = _resolve_cache_roots(args.cache_root or None)
    ticker_filter = (
        {t.strip() for t in args.tickers.split(",") if t.strip()}
        if args.tickers.strip()
        else None
    )
    report = run_backtest(
        args.depth,
        cache_roots=cache_roots,
        limit=args.limit if args.limit > 0 else None,
        tickers=ticker_filter,
        warmup_days=max(10, args.warmup_days),
        strength_floor=max(1.0, args.strength_floor),
    )

    print(
        f"[strategy_backtest] depth={report['depth']} "
        f"processed={report['processed_tickers']} "
        f"effective={len((report.get('strategy_effectiveness') or {}).get('effective') or [])} "
        f"fragile={len((report.get('strategy_effectiveness') or {}).get('fragile') or [])}"
    )

    if args.save or args.save_path:
        if args.save_path:
            path = Path(args.save_path)
        else:
            out = Path(".cache/_global")
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"strategy_backtest_{args.depth}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {path}")

    print(json.dumps({
        "processed_tickers": report.get("processed_tickers"),
        "universe_size": report.get("universe_size"),
        "effective_top": ((report.get("strategy_effectiveness") or {}).get("effective") or [])[:3],
        "fragile_top": ((report.get("strategy_effectiveness") or {}).get("fragile") or [])[:3],
        "walk_forward": {
            "stable_count": ((report.get("walk_forward") or {}).get("stable_count")),
            "unstable_count": ((report.get("walk_forward") or {}).get("unstable_count")),
        },
        "cost_sensitivity": ((report.get("cost_sensitivity") or {}).get("summary_by_scenario") or {}),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
