from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade.realtime_strategy import (  # noqa: E402
    apply_quote_as_realtime_candle,
    refresh_realtime_strategy_from_snapshot,
)


def _candles(days: int = 35, *, last_date: str = "2026-04-29") -> list[dict[str, Any]]:
    end = date.fromisoformat(last_date)
    start = end - timedelta(days=days - 1)
    out: list[dict[str, Any]] = []
    px = 10.0
    for i in range(days):
        day = start + timedelta(days=i)
        close = px + i * 0.08
        out.append(
            {
                "date": day.isoformat(),
                "open": round(close - 0.03, 3),
                "close": round(close, 3),
                "high": round(close + 0.12, 3),
                "low": round(close - 0.18, 3),
                "volume": 1_000_000 + i * 1_000,
            }
        )
    return out


def _bundle(*, last_date: str = "2026-04-29") -> dict[str, Any]:
    candles = _candles(last_date=last_date)
    raw = {
        "ticker": "601778.SH",
        "market": "A",
        "dimensions": {
            "0_basic": {
                "data": {
                    "name": "晶科科技",
                    "price": candles[-1]["close"],
                    "change_pct": 0.0,
                    "market": "A",
                }
            },
            "1_financials": {"data": {}},
            "2_kline": {
                "data": {
                    "stage": "Stage 2 上升",
                    "ma_align": "多头排列",
                    "macd": "金叉 水上",
                    "rsi": 58.0,
                    "kline_stats": {"volatility": "28%", "max_drawdown": "-18%", "ytd_return": "12%"},
                    "intraday_micro": {
                        "bars_count": 0,
                        "micro_available": False,
                        "open_auction_ret_pct": 0.0,
                        "open_15m_ret_pct": 0.0,
                        "tail_30m_ret_pct": 0.0,
                        "open_auction_volume_ratio": 0.0,
                        "close_auction_volume_ratio": 0.0,
                        "close_auction_jump_pct": 0.0,
                        "intraday_amplitude_pct": 0.0,
                    },
                    "candles_60d": candles,
                    "close_60d": [c["close"] for c in candles],
                    "ma20_60d": [c["close"] for c in candles],
                    "ma60_60d": [c["close"] for c in candles],
                    "indicators": {},
                }
            },
            "7_industry": {"data": {"growth": 0.0}},
            "10_valuation": {"data": {}},
            "12_capital_flow": {"data": {"main_fund_flow_20d": []}},
            "15_events": {"data": {"recent_news": [], "recent_notices": [], "event_timeline": []}},
            "16_lhb": {"data": {"lhb_count_30d": 0, "matched_youzi": []}},
            "17_sentiment": {"data": {"thermometer_value": 0.0, "positive_pct": "0%"}},
        },
    }
    return {
        "status": "ok",
        "ticker": "601778.SH",
        "market": "A",
        "raw": raw,
        "dimensions": dict(raw["dimensions"]),
        "panel": {"investors": [], "panel_consensus": 50.0, "panel_mode": "investor_panel"},
        "strategy_signals": {},
        "synthesis": {
            "overall_score": 55.0,
            "verdict_label": "观察",
            "agent_reviewed": True,
            "friendly": {},
        },
    }


def test_realtime_snapshot_updates_candle_and_recomputes_strategy_outputs():
    bundle = _bundle(last_date="2026-04-29")
    snapshot = {
        "ok": True,
        "ticker": "601778.SH",
        "market": "A",
        "name": "晶科科技",
        "price": 8.2,
        "open": 10.1,
        "high": 10.2,
        "low": 8.1,
        "prev_close": 12.72,
        "change_pct": -35.5,
        "volume": 2_000_000,
        "source": "pytest_quote",
        "ts": "2026-04-30T10:30:00",
    }

    refreshed = refresh_realtime_strategy_from_snapshot(bundle, snapshot, depth="deep")

    assert refreshed["ok"] is True
    assert refreshed["schema_valid"] is True
    assert refreshed["enabled_strategy_count"] == 12
    runtime = bundle["_papertrade_runtime"]
    assert runtime["realtime_candle_overlay_applied"] is True
    assert runtime["realtime_strategy_refresh_applied"] is True

    kline = bundle["raw"]["dimensions"]["2_kline"]["data"]
    assert kline["candles_60d"][-1]["date"] == "2026-04-30"
    assert kline["candles_60d"][-1]["close"] == 8.2
    assert kline["intraday_micro"]["open_to_close_ret_pct"] < 0
    assert bundle["strategy_signals"]["signals"]
    assert any(s["strategy_id"] == "intraday_timing" for s in bundle["strategy_signals"]["signals"])
    assert bundle["synthesis"]["short_trading"]["setup_score"] >= 0
    assert bundle["synthesis"]["short_trading"] == bundle["synthesis"]["friendly"]["short_trading"]


def test_realtime_snapshot_replaces_same_day_candle_without_growing_series():
    bundle = _bundle(last_date="2026-04-30")
    kline = bundle["raw"]["dimensions"]["2_kline"]["data"]
    before_len = len(kline["candles_60d"])

    overlay = apply_quote_as_realtime_candle(
        bundle,
        {
            "ok": True,
            "ticker": "601778.SH",
            "price": 11.5,
            "open": 11.0,
            "high": 11.7,
            "low": 10.9,
            "prev_close": 10.8,
            "ts": "2026-04-30T14:56:00",
            "source": "pytest_quote",
        },
    )

    assert overlay["ok"] is True
    assert overlay["replaced"] is True
    assert len(kline["candles_60d"]) == before_len
    assert kline["candles_60d"][-1]["close"] == 11.5
    assert bundle["dimensions"]["2_kline"]["data"]["candles_60d"][-1]["close"] == 11.5
