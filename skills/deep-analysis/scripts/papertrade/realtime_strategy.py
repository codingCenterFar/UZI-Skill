from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from compute_friendly import compute_short_trading_module  # noqa: E402
from lib.market_router import parse_ticker  # noqa: E402
from lib.strategy_engine import build_strategy_outputs  # noqa: E402


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _trade_date_from_snapshot(snapshot: dict[str, Any]) -> str:
    for key in ("trade_date", "date", "day"):
        raw = snapshot.get(key)
        if raw:
            s = str(raw).strip()
            if len(s) >= 10:
                try:
                    return date.fromisoformat(s[:10]).isoformat()
                except Exception:
                    pass

    ts = str(snapshot.get("ts") or snapshot.get("timestamp") or "").strip()
    if len(ts) >= 10:
        try:
            return date.fromisoformat(ts[:10]).isoformat()
        except Exception:
            pass
    return date.today().isoformat()


def _round_px(v: float) -> float:
    return round(float(v), 4)


def _raw_dimensions(bundle: dict[str, Any]) -> dict[str, Any]:
    raw = bundle.setdefault("raw", {})
    return raw.setdefault("dimensions", {})


def _sync_top_level_dimensions(bundle: dict[str, Any], *dim_keys: str) -> None:
    dims = bundle.get("dimensions")
    raw_dims = ((bundle.get("raw") or {}).get("dimensions") or {})
    if not isinstance(dims, dict) or not isinstance(raw_dims, dict):
        return
    for key in dim_keys:
        if key in raw_dims:
            dims[key] = raw_dims[key]


def _moving_average(values: list[float], n: int) -> list[float]:
    out: list[float] = []
    for i in range(len(values)):
        window = values[max(0, i - n + 1): i + 1]
        out.append(round(sum(window) / len(window), 4) if window else 0.0)
    return out


def _rsi14(closes: list[float]) -> float:
    if len(closes) < 15:
        return 0.0
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(closes[-15:-1], closes[-14:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / 14.0
    avg_loss = sum(losses) / 14.0
    if avg_loss <= 1e-9:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _refresh_kline_rollups(kline: dict[str, Any]) -> None:
    candles = kline.get("candles_60d") if isinstance(kline.get("candles_60d"), list) else []
    closes = [_f(c.get("close")) for c in candles if isinstance(c, dict) and _f(c.get("close")) > 0]
    highs = [_f(c.get("high")) for c in candles if isinstance(c, dict) and _f(c.get("high")) > 0]
    lows = [_f(c.get("low")) for c in candles if isinstance(c, dict) and _f(c.get("low")) > 0]
    vols = [_f(c.get("volume")) for c in candles if isinstance(c, dict)]
    if closes:
        kline["close_60d"] = closes[-60:]
        kline["ma20_60d"] = _moving_average(closes[-60:], 20)
        kline["ma60_60d"] = _moving_average(closes[-60:], 60)
        rsi = _rsi14(closes)
        if rsi > 0:
            kline["rsi"] = rsi
    indicators = kline.setdefault("indicators", {})
    if highs and closes:
        high_base = max(highs[-60:])
        indicators["pct_from_year_high"] = round((closes[-1] - high_base) / high_base * 100, 3) if high_base > 0 else 0.0
    if len(vols) >= 20:
        avg5 = sum(vols[-5:]) / 5.0
        avg20 = sum(vols[-20:]) / 20.0
        indicators["vol_5_vs_20"] = round(avg5 / avg20, 3) if avg20 > 0 else 0.0
    if highs and lows and closes:
        high_60 = max(highs[-60:])
        low_60 = min(lows[-60:])
        if low_60 > 0:
            kline.setdefault("kline_stats", {})["range_position_60d_pct"] = round((closes[-1] - low_60) / (high_60 - low_60) * 100, 2) if high_60 > low_60 else 50.0


def apply_quote_as_realtime_candle(bundle: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Update the in-memory bundle so cached cycles can reason from the latest quote.

    This intentionally does not write cache files. Full stage1/stage2 refreshes
    remain the durable source; realtime loops get a per-loop candle overlay.
    """
    if not isinstance(bundle, dict) or not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return {"ok": False, "reason": "invalid_bundle_or_snapshot"}

    ticker = str(snapshot.get("ticker") or bundle.get("ticker") or "").strip()
    try:
        ticker = parse_ticker(ticker).full
    except Exception:
        pass

    price = _f(snapshot.get("price"), 0.0)
    if price <= 0:
        return {"ok": False, "reason": "missing_price", "ticker": ticker}

    raw_dims = _raw_dimensions(bundle)
    basic = raw_dims.setdefault("0_basic", {}).setdefault("data", {})
    basic["price"] = price
    if snapshot.get("change_pct") not in (None, ""):
        basic["change_pct"] = snapshot.get("change_pct")
    if snapshot.get("name"):
        basic["name"] = snapshot.get("name")

    kline = raw_dims.setdefault("2_kline", {}).setdefault("data", {})
    candles = kline.get("candles_60d")
    if not isinstance(candles, list):
        candles = []

    trade_date = _trade_date_from_snapshot(snapshot)
    prev_close = _f(snapshot.get("prev_close"), 0.0)
    if prev_close <= 0 and candles:
        prev_close = _f(candles[-1].get("close"), 0.0) if isinstance(candles[-1], dict) else 0.0

    open_px = _f(snapshot.get("open"), 0.0) or prev_close or price
    high_px = _f(snapshot.get("high"), 0.0) or max(open_px, price, prev_close)
    low_candidates = [v for v in (open_px, price, prev_close, _f(snapshot.get("low"), 0.0)) if v > 0]
    low_px = _f(snapshot.get("low"), 0.0) or min(low_candidates or [price])
    high_px = max(high_px, open_px, price)
    low_px = min(low_px, open_px, price)

    new_candle: dict[str, Any] = {
        "date": trade_date,
        "open": _round_px(open_px),
        "close": _round_px(price),
        "high": _round_px(high_px),
        "low": _round_px(low_px),
    }
    volume = _f(snapshot.get("volume"), 0.0)
    amount = _f(snapshot.get("amount"), 0.0)
    if volume > 0:
        new_candle["volume"] = volume
    if amount > 0:
        new_candle["amount"] = amount

    replaced = False
    if candles and isinstance(candles[-1], dict):
        last_date = str(candles[-1].get("date") or candles[-1].get("day") or "")[:10]
        if last_date == trade_date:
            merged = dict(candles[-1])
            merged.update(new_candle)
            candles[-1] = merged
            replaced = True
    if not replaced:
        candles.append(new_candle)
    kline["candles_60d"] = candles[-60:]

    intraday = kline.setdefault("intraday_micro", {})
    intraday["bars_count"] = max(int(_f(intraday.get("bars_count"), 0.0)), 1)
    intraday["micro_available"] = bool(intraday.get("micro_available"))
    if prev_close > 0:
        intraday["open_auction_ret_pct"] = round((open_px / prev_close - 1.0) * 100.0, 3)
    if open_px > 0:
        intraday["open_to_close_ret_pct"] = round((price / open_px - 1.0) * 100.0, 3)
    if low_px > 0:
        intraday["intraday_amplitude_pct"] = round((high_px / low_px - 1.0) * 100.0, 3)
    intraday.setdefault("open_15m_ret_pct", 0.0)
    intraday.setdefault("tail_30m_ret_pct", 0.0)
    intraday.setdefault("open_auction_volume_ratio", 0.0)
    intraday.setdefault("close_auction_volume_ratio", 0.0)
    intraday.setdefault("close_auction_jump_pct", 0.0)

    _refresh_kline_rollups(kline)
    _sync_top_level_dimensions(bundle, "0_basic", "2_kline")

    runtime = bundle.setdefault("_papertrade_runtime", {})
    runtime["realtime_candle_overlay_applied"] = True
    runtime["realtime_candle_date"] = trade_date
    runtime["realtime_candle_replaced"] = replaced
    runtime["realtime_candle_price"] = price

    return {
        "ok": True,
        "ticker": ticker,
        "trade_date": trade_date,
        "price": price,
        "replaced": replaced,
        "source": snapshot.get("source"),
    }


def refresh_realtime_strategy_from_snapshot(
    bundle: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    depth: str = "deep",
) -> dict[str, Any]:
    overlay = apply_quote_as_realtime_candle(bundle, snapshot)
    if not overlay.get("ok"):
        return {"ok": False, "stage": "overlay", "overlay": overlay}

    ticker = str(overlay.get("ticker") or snapshot.get("ticker") or bundle.get("ticker") or "")
    raw = bundle.get("raw") if isinstance(bundle.get("raw"), dict) else {}
    dims = bundle.get("dimensions") if isinstance(bundle.get("dimensions"), dict) else raw.get("dimensions", {})
    try:
        features_doc, signals_doc, meta_doc = build_strategy_outputs(ticker, raw, dims or {}, depth or "deep")
        bundle["strategy_features"] = features_doc
        bundle["strategy_signals"] = signals_doc
        bundle["strategy_meta"] = meta_doc

        synthesis = bundle.setdefault("synthesis", {})
        short_trading = compute_short_trading_module(
            raw,
            dims or {},
            synthesis,
            strategy_signals=signals_doc,
            strategy_summary=signals_doc.get("summary") if isinstance(signals_doc, dict) else {},
        )
        synthesis["short_trading"] = short_trading
        friendly = synthesis.setdefault("friendly", {})
        if isinstance(friendly, dict):
            friendly["short_trading"] = short_trading

        runtime = bundle.setdefault("_papertrade_runtime", {})
        runtime["realtime_strategy_refresh_applied"] = True
        runtime["realtime_strategy_depth"] = depth or "deep"
        runtime["realtime_strategy_generated_at"] = signals_doc.get("generated_at")
        runtime["realtime_short_setup_score"] = short_trading.get("setup_score")
        runtime["realtime_short_bias"] = short_trading.get("bias")

        return {
            "ok": True,
            "overlay": overlay,
            "depth": depth or "deep",
            "schema_valid": bool(meta_doc.get("schema_valid")),
            "enabled_strategy_count": meta_doc.get("enabled_strategy_count"),
            "strategy_generated_at": signals_doc.get("generated_at"),
            "short_setup_score": short_trading.get("setup_score"),
            "short_bias": short_trading.get("bias"),
        }
    except Exception as e:
        runtime = bundle.setdefault("_papertrade_runtime", {})
        runtime["realtime_strategy_refresh_applied"] = False
        runtime["realtime_strategy_error"] = str(e)
        return {
            "ok": False,
            "stage": "strategy_recompute",
            "overlay": overlay,
            "error": str(e),
        }
