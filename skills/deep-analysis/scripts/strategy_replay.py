"""Replay strategy signals on cached tickers for threshold calibration.

Usage:
  python strategy_replay.py --depth medium
  python strategy_replay.py --depth medium --limit 80 --save
  python strategy_replay.py --depth medium --cache-root .cache --save
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Iterable

from lib.cache import read_task_output
from lib.strategy_engine import build_strategy_outputs


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
        p = Path(cli_cache_root)
        return [p]

    script_cache = Path(__file__).resolve().parent / ".cache"
    cwd_cache = Path(".cache")
    return _dedup_paths([cwd_cache, script_cache])


def _iter_cached_tickers(cache_roots: list[Path]) -> list[str]:
    out: set[str] = set()
    for cache_root in cache_roots:
        if not cache_root.exists():
            continue
        for p in sorted(cache_root.iterdir()):
            if not p.is_dir():
                continue
            name = p.name
            if name.startswith("_"):
                continue
            if (p / "raw_data.json").exists():
                out.add(name)
    return sorted(out)


def _load_doc(ticker: str, name: str, cache_roots: list[Path]) -> dict:
    # Prefer lib.cache default location first for backward compatibility.
    doc = read_task_output(ticker, name)
    if doc:
        return doc
    file_name = f"{name}.json"
    for root in cache_roots:
        path = root / ticker / file_name
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def _load_backtest_report(depth: str, cache_roots: list[Path]) -> tuple[dict, str]:
    candidates: list[Path] = []
    for root in cache_roots:
        candidates.append(root / "_global" / f"strategy_backtest_{depth}.json")
    candidates.append(Path(".cache/_global") / f"strategy_backtest_{depth}.json")
    candidates.append((Path(__file__).resolve().parent / ".cache/_global") / f"strategy_backtest_{depth}.json")

    for p in _dedup_paths(candidates):
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8")), str(p)
        except Exception:
            continue
    return {}, ""


def _calibration_hint(by_strategy: dict[str, dict], processed: int, min_samples: int) -> dict:
    recommendations: list[dict] = []
    status_counter: Counter = Counter()

    for sid, stats in by_strategy.items():
        non_skip = (
            int(stats.get("bullish", 0) or 0)
            + int(stats.get("bearish", 0) or 0)
            + int(stats.get("neutral", 0) or 0)
        )
        if non_skip <= 0:
            status = "insufficient"
            action = "No non-skip samples; keep baseline and collect more A-share samples."
        else:
            bull_rate = float(stats.get("bullish_rate_pct", 0.0) or 0.0)
            bear_rate = float(stats.get("bearish_rate_pct", 0.0) or 0.0)
            neutral_rate = float(stats.get("neutral_rate_pct", 0.0) or 0.0)
            if non_skip < max(8, min_samples // 2):
                status = "insufficient"
                action = "Sample size is too small for threshold tuning."
            elif neutral_rate >= 85.0:
                status = "too_neutral"
                action = "Consider relaxing key thresholds slightly (about 10-15%)."
            elif bull_rate >= 70.0 or bear_rate >= 70.0:
                status = "one_sided"
                action = "Signal is skewed; tighten trigger thresholds or add regime filters."
            else:
                status = "balanced"
                action = "Distribution is balanced enough; keep current thresholds."

        recommendations.append(
            {
                "strategy_id": sid,
                "status": status,
                "samples_non_skip": non_skip,
                "action": action,
            }
        )
        status_counter[status] += 1

    global_note = (
        f"processed={processed} is below min_samples={min_samples}; "
        "calibration confidence is limited."
        if processed < min_samples
        else "sample size reached target; calibration confidence is acceptable."
    )
    return {
        "min_samples_target": min_samples,
        "processed": processed,
        "global_note": global_note,
        "status_breakdown": dict(status_counter),
        "recommendations": recommendations,
    }


def replay(
    depth: str,
    cache_roots: list[Path],
    limit: int | None = None,
    tickers: set[str] | None = None,
    min_samples: int = 20,
    trade_backtest: dict | None = None,
) -> dict:
    universe = _iter_cached_tickers(cache_roots)
    if tickers:
        universe = [t for t in universe if t in tickers]
    if limit and limit > 0:
        universe = universe[:limit]

    strategy_signal_counter: dict[str, Counter] = defaultdict(Counter)
    strategy_strength_values: dict[str, list[float]] = defaultdict(list)
    strategy_alignment: dict[str, Counter] = defaultdict(Counter)
    regime_stats: dict[str, Counter] = defaultdict(Counter)
    sample_rows: list[dict] = []
    skipped_missing_raw = 0

    bull_any = bear_any = neutral_only = skipped = 0
    processed = 0
    for t in universe:
        raw = _load_doc(t, "raw_data", cache_roots)
        if not raw:
            skipped += 1
            skipped_missing_raw += 1
            continue
        dims = _load_doc(t, "dimensions", cache_roots) or {}
        try:
            sf, ss, _ = build_strategy_outputs(t, raw, dims, depth)
        except Exception:
            skipped += 1
            continue
        processed += 1

        sigs = ss.get("signals") or []
        features = sf.get("features") or {}
        regime = sf.get("regime") or {}
        market_regime = str(regime.get("market_regime") or "unknown")
        for s in sigs:
            sid = s.get("strategy_id", "unknown")
            signal = s.get("signal", "unknown")
            strategy_signal_counter[sid][signal] += 1
            strategy_strength_values[sid].append(float(s.get("strength", 0) or 0))

            if signal in ("bullish", "bearish"):
                strategy_alignment[sid]["active"] += 1
                strategy_alignment[sid][signal] += 1
                mom20 = float(features.get("raw_momentum_20d_pct", 0) or 0)
                ytd = float(features.get("ytd_return", 0) or 0)
                if (signal == "bullish" and mom20 >= 0) or (signal == "bearish" and mom20 <= 0):
                    strategy_alignment[sid]["hit_20d_proxy"] += 1
                if (signal == "bullish" and ytd >= 0) or (signal == "bearish" and ytd <= 0):
                    strategy_alignment[sid]["hit_ytd_proxy"] += 1

        summary = ss.get("summary") or {}
        bull_n = int(summary.get("bullish_count", 0) or 0)
        bear_n = int(summary.get("bearish_count", 0) or 0)
        neutral_n = int(summary.get("neutral_count", 0) or 0)
        regime_stats[market_regime]["ticker_count"] += 1
        regime_stats[market_regime]["bullish_signals"] += bull_n
        regime_stats[market_regime]["bearish_signals"] += bear_n
        regime_stats[market_regime]["neutral_signals"] += neutral_n
        if bull_n > 0:
            bull_any += 1
            regime_stats[market_regime]["bull_any_count"] += 1
        if bear_n > 0:
            bear_any += 1
            regime_stats[market_regime]["bear_any_count"] += 1
        if bull_n == 0 and bear_n == 0:
            neutral_only += 1
            regime_stats[market_regime]["neutral_only_count"] += 1

        sample_rows.append(
            {
                "ticker": t,
                "market": sf.get("market"),
                "fundamental_score": sf.get("features", {}).get("fundamental_score"),
                "market_regime": market_regime,
                "bullish_count": bull_n,
                "bearish_count": bear_n,
                "neutral_count": neutral_n,
                "top_bullish": (summary.get("top_bullish") or [])[:2],
                "top_bearish": (summary.get("top_bearish") or [])[:2],
            }
        )

    by_strategy = {}
    for sid, cnt in sorted(strategy_signal_counter.items()):
        total = sum(cnt.values()) or 1
        by_strategy[sid] = {
            "bullish": cnt.get("bullish", 0),
            "bearish": cnt.get("bearish", 0),
            "neutral": cnt.get("neutral", 0),
            "skip": cnt.get("skip", 0),
            "bullish_rate_pct": round(cnt.get("bullish", 0) / total * 100, 1),
            "bearish_rate_pct": round(cnt.get("bearish", 0) / total * 100, 1),
            "neutral_rate_pct": round(cnt.get("neutral", 0) / total * 100, 1),
        }

    calibration = _calibration_hint(by_strategy, processed, min_samples=min_samples)
    regime_stability = {}
    for rg, c in sorted(regime_stats.items()):
        n = int(c.get("ticker_count", 0) or 0)
        if n <= 0:
            continue
        regime_stability[rg] = {
            "ticker_count": n,
            "bull_any_rate_pct": round(c.get("bull_any_count", 0) / n * 100, 1),
            "bear_any_rate_pct": round(c.get("bear_any_count", 0) / n * 100, 1),
            "neutral_only_rate_pct": round(c.get("neutral_only_count", 0) / n * 100, 1),
            "avg_bullish_signals": round(c.get("bullish_signals", 0) / n, 2),
            "avg_bearish_signals": round(c.get("bearish_signals", 0) / n, 2),
            "avg_neutral_signals": round(c.get("neutral_signals", 0) / n, 2),
        }

    backtest_proxy = {}
    for sid, align in sorted(strategy_alignment.items()):
        active = int(align.get("active", 0) or 0)
        if active <= 0:
            continue
        hit_20d = int(align.get("hit_20d_proxy", 0) or 0)
        hit_ytd = int(align.get("hit_ytd_proxy", 0) or 0)
        bullish_n = int(align.get("bullish", 0) or 0)
        bearish_n = int(align.get("bearish", 0) or 0)
        strengths = strategy_strength_values.get(sid) or []
        mean_strength = mean(strengths) if strengths else 0.0
        std_strength = pstdev(strengths) if len(strengths) > 1 else 0.0
        directional_balance = 100.0 - abs(bullish_n - bearish_n) / max(active, 1) * 100.0
        hit_20d_rate = hit_20d / active * 100.0
        stability_score = max(
            0.0,
            min(
                100.0,
                directional_balance * 0.35 + hit_20d_rate * 0.45 + max(0.0, 100.0 - std_strength) * 0.20,
            ),
        )
        backtest_proxy[sid] = {
            "active_signals": active,
            "bullish_signals": bullish_n,
            "bearish_signals": bearish_n,
            "hit_rate_20d_proxy_pct": round(hit_20d_rate, 1),
            "hit_rate_ytd_proxy_pct": round(hit_ytd / active * 100.0, 1),
            "mean_strength": round(mean_strength, 2),
            "std_strength": round(std_strength, 2),
            "stability_score": round(stability_score, 1),
        }

    tb = trade_backtest if isinstance(trade_backtest, dict) else {}
    eff = (tb.get("strategy_effectiveness") or {}) if tb else {}
    tradable_summary = {
        "status": "available" if tb else "unavailable",
        "note": (
            (eff.get("note") or "A-share constrained backtest is available.")
            if tb
            else "No strategy_backtest report found; run `python strategy_backtest.py --depth <depth> --save`.",
        ),
        "effective_top": (eff.get("effective") or [])[:5] if tb else [],
        "fragile_top": (eff.get("fragile") or [])[:5] if tb else [],
        "processed_tickers": tb.get("processed_tickers", 0) if tb else 0,
        "by_market_regime": tb.get("by_market_regime") if tb else {},
        "walk_forward": tb.get("walk_forward") if tb else {},
        "cost_sensitivity": (tb.get("cost_sensitivity") or {}).get("summary_by_scenario") if tb else {},
    }

    stability_note = (
        "Replay now includes tradable A-share constrained backtest summary + proxy alignment."
        if tb
        else "Proxy-only evaluation: compares signal direction with 20d momentum / YTD sign; not a tradable backtest."
    )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "depth": depth,
        "processed_tickers": processed,
        "skipped_tickers": skipped,
        "skipped_missing_raw": skipped_missing_raw,
        "universe_size": len(universe),
        "cache_roots": [str(p) for p in cache_roots],
        "coverage": {
            "bull_any_count": bull_any,
            "bear_any_count": bear_any,
            "neutral_only_count": neutral_only,
            "bull_any_rate_pct": round(bull_any / max(processed, 1) * 100, 1),
            "bear_any_rate_pct": round(bear_any / max(processed, 1) * 100, 1),
            "neutral_only_rate_pct": round(neutral_only / max(processed, 1) * 100, 1),
        },
        "by_strategy": by_strategy,
        "calibration": calibration,
        "stability": {
            "note": stability_note,
            "by_market_regime": regime_stability,
            "backtest_proxy_by_strategy": backtest_proxy,
            "tradable_backtest": tradable_summary,
        },
        "samples": sample_rows[:60],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay strategy signals on local cache universe")
    ap.add_argument("--depth", default="medium", choices=["lite", "medium", "deep"])
    ap.add_argument("--limit", type=int, default=0, help="max cached tickers to replay (0 = all)")
    ap.add_argument("--cache-root", default="", help="cache root path (default: auto detect)")
    ap.add_argument("--tickers", default="", help="comma-separated tickers to replay")
    ap.add_argument("--min-samples", type=int, default=20, help="minimum sample target for calibration confidence")
    ap.add_argument("--run-backtest", action="store_true", help="run strategy_backtest before replay and merge its summary")
    ap.add_argument("--backtest-warmup-days", type=int, default=25)
    ap.add_argument("--backtest-strength-floor", type=float, default=55.0)
    ap.add_argument("--save", action="store_true", help="save JSON report to .cache/_global")
    ap.add_argument("--save-path", default="", help="optional JSON output path")
    args = ap.parse_args()

    cache_roots = _resolve_cache_roots(args.cache_root or None)
    ticker_filter = (
        {t.strip() for t in args.tickers.split(",") if t.strip()}
        if args.tickers.strip()
        else None
    )
    bt_report: dict | None = None
    bt_path = ""
    if args.run_backtest:
        try:
            from strategy_backtest import run_backtest as _run_backtest
            bt_report = _run_backtest(
                args.depth,
                cache_roots=cache_roots,
                limit=args.limit if args.limit > 0 else None,
                tickers=ticker_filter,
                warmup_days=max(10, args.backtest_warmup_days),
                strength_floor=max(1.0, args.backtest_strength_floor),
            )
            bt_path = "(in-memory)"
        except Exception as e:
            print(f"⚠ failed to run strategy_backtest inline: {type(e).__name__}: {str(e)[:120]}")

    if bt_report is None:
        loaded, bt_path = _load_backtest_report(args.depth, cache_roots)
        bt_report = loaded or None

    report = replay(
        args.depth,
        cache_roots=cache_roots,
        limit=args.limit if args.limit > 0 else None,
        tickers=ticker_filter,
        min_samples=max(1, args.min_samples),
        trade_backtest=bt_report,
    )
    print(
        f"replay depth={report['depth']} "
        f"processed={report['processed_tickers']} "
        f"skipped={report['skipped_tickers']} "
        f"universe={report['universe_size']}"
    )
    print("cache_roots:")
    for root in report.get("cache_roots", []):
        print(f"  - {root}")
    cov = report["coverage"]
    print(
        "coverage "
        f"bull_any={cov['bull_any_count']} ({cov['bull_any_rate_pct']}%) "
        f"bear_any={cov['bear_any_count']} ({cov['bear_any_rate_pct']}%) "
        f"neutral_only={cov['neutral_only_count']} ({cov['neutral_only_rate_pct']}%)"
    )
    print("by_strategy:")
    for sid, v in report["by_strategy"].items():
        print(
            f"  {sid:28s} bull={v['bullish']:4d} bear={v['bearish']:4d} "
            f"neutral={v['neutral']:4d} skip={v['skip']:4d} "
            f"bull%={v['bullish_rate_pct']:5.1f} bear%={v['bearish_rate_pct']:5.1f}"
        )
    cal = report.get("calibration") or {}
    print("calibration:")
    print(f"  note={cal.get('global_note', '')}")
    for status, n in (cal.get("status_breakdown") or {}).items():
        print(f"  {status}: {n}")
    stability = report.get("stability") or {}
    if stability:
        print("stability:")
        for rg, row in (stability.get("by_market_regime") or {}).items():
            print(
                f"  regime={rg:14s} n={row.get('ticker_count', 0):3d} "
                f"bull_any%={row.get('bull_any_rate_pct', 0):5.1f} "
                f"bear_any%={row.get('bear_any_rate_pct', 0):5.1f}"
            )
        tb = stability.get("tradable_backtest") or {}
        if tb:
            print(f"tradable_backtest: {tb.get('status')} (source={bt_path or 'n/a'})")
            for row in (tb.get("effective_top") or [])[:3]:
                print(
                    f"  effective {row.get('strategy_id','?'):28s} "
                    f"wr={row.get('win_rate_pct', 0):5.1f}% "
                    f"avg={row.get('avg_net_ret_pct', 0):6.3f}% "
                    f"n={row.get('filled_trades', 0)}"
                )
            for row in (tb.get("fragile_top") or [])[:2]:
                print(
                    f"  fragile   {row.get('strategy_id','?'):28s} "
                    f"wr={row.get('win_rate_pct', 0):5.1f}% "
                    f"avg={row.get('avg_net_ret_pct', 0):6.3f}% "
                    f"n={row.get('filled_trades', 0)}"
                )
            wf = tb.get("walk_forward") or {}
            if wf:
                print(
                    "  walk_forward "
                    f"stable={wf.get('stable_count', 0)} "
                    f"unstable={wf.get('unstable_count', 0)} "
                    f"insufficient={wf.get('insufficient_count', 0)}"
                )
            cs = tb.get("cost_sensitivity") or {}
            if isinstance(cs, dict) and cs:
                low = cs.get("low") or {}
                base = cs.get("base") or {}
                high = cs.get("high") or {}
                print(
                    "  cost_sensitivity "
                    f"low={low.get('avg_net_ret_pct', 0)} "
                    f"base={base.get('avg_net_ret_pct', 0)} "
                    f"high={high.get('avg_net_ret_pct', 0)}"
                )

    if args.save:
        if args.save_path:
            path = Path(args.save_path)
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            out_root = next((p for p in cache_roots if p.exists()), cache_roots[0] if cache_roots else Path(".cache"))
            out = out_root / "_global"
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"strategy_replay_{args.depth}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved {path}")


if __name__ == "__main__":
    main()
