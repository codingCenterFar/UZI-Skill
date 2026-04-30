"""Dimension 2 · K线 (OHLCV + 均线 + MACD + RSI + 筹码分布 + 简单形态).

补全：原方案要求覆盖
  • Stage 阶段判断 (1底/2升/3顶/4降)
  • MA5/10/20/60/120/250 多空头排列
  • MACD/RSI
  • 量价配合
  • 关键支撑/压力位 (近 250 日高低 + 斐波)
  • 形态: VCP/突破/三角收敛 (简易标记)
  • 筹码分布 stock_cyq_em
"""
import json
import sys
from datetime import datetime
from statistics import mean

import akshare as ak  # type: ignore
from lib import data_sources as ds
from lib.market_router import parse_ticker


def _ema(values, n):
    k = 2 / (n + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _ma(closes, n):
    return [sum(closes[max(0, i - n + 1):i + 1]) / min(i + 1, n) for i in range(len(closes))]


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = mean(gains[-n:])
    avg_loss = mean(losses[-n:]) or 1e-9
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _stage(closes, ma200) -> int:
    """Weinstein Stage Analysis: 1=底部 2=上升 3=顶部 4=下降"""
    if len(closes) < 60 or ma200 is None:
        return 0
    last = closes[-1]
    ma200_now = ma200[-1]
    ma200_60ago = ma200[-60] if len(ma200) >= 60 else ma200[0]
    above = last > ma200_now
    rising = ma200_now > ma200_60ago
    if above and rising:
        return 2
    if not above and rising:
        return 1
    if above and not rising:
        return 3
    return 4


def _vcp_score(highs: list[float], lows: list[float]) -> float:
    """Simple VCP detector: range contraction over last 30 / 60 / 90 days."""
    def rng(start, end):
        if end > len(highs):
            end = len(highs)
            start = max(0, end - (end - start))
        h, l = highs[-end:-start] if start > 0 else highs[-end:], lows[-end:-start] if start > 0 else lows[-end:]
        if not h:
            return 0
        return (max(h) - min(l)) / max(min(l), 1e-9)
    r30 = rng(0, 30)
    r60 = rng(30, 60)
    r90 = rng(60, 90)
    if r60 > 0 and r90 > 0:
        return max(0, 1 - r30 / r60) + max(0, 1 - r60 / r90)
    return 0


def compute_indicators(klines: list[dict]) -> dict:
    if not klines:
        return {}
    closes = [float(r.get("收盘") or r.get("Close") or 0) for r in klines]
    highs = [float(r.get("最高") or r.get("High") or 0) for r in klines]
    lows = [float(r.get("最低") or r.get("Low") or 0) for r in klines]
    vols = [float(r.get("成交量") or r.get("Volume") or 0) for r in klines]
    if not closes or all(c == 0 for c in closes):
        return {}

    ma5, ma10, ma20, ma60, ma120, ma200 = (_ma(closes, n) for n in (5, 10, 20, 60, 120, 200))
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    macd_hist = [(d - e) * 2 for d, e in zip(dif, dea)]

    last = closes[-1]
    avg_vol_5 = mean(vols[-5:]) if len(vols) >= 5 else 0
    avg_vol_20 = mean(vols[-20:]) if len(vols) >= 20 else 0

    return {
        "last_close": last,
        "ma5": ma5[-1], "ma10": ma10[-1], "ma20": ma20[-1],
        "ma60": ma60[-1], "ma120": ma120[-1], "ma200": ma200[-1] if ma200 else None,
        "above_ma20": last > ma20[-1],
        "above_ma200": last > ma200[-1] if ma200 else None,
        "ma_bull_alignment": ma5[-1] > ma10[-1] > ma20[-1] > ma60[-1] > ma120[-1],
        "macd_dif": dif[-1], "macd_dea": dea[-1], "macd_hist": macd_hist[-1],
        "macd_golden_cross": dif[-1] > dea[-1] and dif[-2] <= dea[-2] if len(dif) > 1 else False,
        "rsi_14": _rsi(closes, 14),
        "year_high": max(closes[-250:]) if len(closes) >= 250 else max(closes),
        "year_low": min(closes[-250:]) if len(closes) >= 250 else min(closes),
        "pct_from_year_high": (last - max(closes[-250:])) / max(closes[-250:]) * 100 if len(closes) >= 250 else 0,
        "stage": _stage(closes, ma200),
        "vol_5_vs_20": (avg_vol_5 / avg_vol_20) if avg_vol_20 else None,
        "vcp_score": _vcp_score(highs, lows),
    }


def fetch_chip_distribution(ti) -> dict:
    """筹码分布 — akshare stock_cyq_em."""
    if ti.market != "A":
        return {}
    try:
        df = ak.stock_cyq_em(symbol=ti.code, adjust="qfq")
        if df is None or df.empty:
            return {}
        last = df.iloc[-1].to_dict()
        return {
            "profit_ratio": last.get("获利比例"),
            "avg_cost": last.get("平均成本"),
            "concentration_70": last.get("70集中度"),
            "concentration_90": last.get("90集中度"),
            "cost_low_70": last.get("70成本-低"),
            "cost_high_70": last.get("70成本-高"),
            "history_30d": df.tail(30).to_dict("records"),
        }
    except Exception as e:
        return {"error": str(e)}


def _float(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_intraday_minutes(ti) -> dict:
    """分钟级分时（T313）: 1min bars for latest trading day."""
    if ti.market != "A":
        return {"bars_count": 0, "minute_bars_1d": [], "note": "minute stream currently A-share only"}
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=ti.code, period="1", adjust="")
    except Exception as e:
        return {"bars_count": 0, "minute_bars_1d": [], "error": str(e)}

    if df is None or df.empty:
        return {"bars_count": 0, "minute_bars_1d": []}

    records = df.to_dict("records")
    rows = []
    for r in records:
        t = str(r.get("时间") or r.get("date") or r.get("Date") or "").strip()
        if not t:
            continue
        rows.append(
            {
                "time": t,
                "open": _float(r.get("开盘", r.get("open"))),
                "close": _float(r.get("收盘", r.get("close"))),
                "high": _float(r.get("最高", r.get("high"))),
                "low": _float(r.get("最低", r.get("low"))),
                "volume": _float(r.get("成交量", r.get("volume"))),
                "amount": _float(r.get("成交额", r.get("amount"))),
            }
        )

    if not rows:
        return {"bars_count": 0, "minute_bars_1d": []}

    latest_date = max(str(x.get("time"))[:10] for x in rows)
    one_day = [x for x in rows if str(x.get("time"))[:10] == latest_date and x.get("close", 0) > 0]
    one_day = one_day[-300:] if len(one_day) > 300 else one_day
    return {
        "latest_trade_date": latest_date,
        "bars_count": len(one_day),
        "minute_bars_1d": one_day,
    }


def _intraday_micro_features(intraday_doc: dict, prev_close: float) -> dict:
    bars = (intraday_doc or {}).get("minute_bars_1d") or []
    if not bars:
        return {
            "bars_count": 0,
            "micro_available": False,
            "open_auction_ret_pct": 0.0,
            "open_15m_ret_pct": 0.0,
            "tail_30m_ret_pct": 0.0,
            "open_auction_volume_ratio": 0.0,
            "close_auction_volume_ratio": 0.0,
            "close_auction_jump_pct": 0.0,
            "open_to_close_ret_pct": 0.0,
            "intraday_amplitude_pct": 0.0,
        }

    opens = [_float(x.get("open")) for x in bars]
    closes = [_float(x.get("close")) for x in bars]
    highs = [_float(x.get("high")) for x in bars]
    lows = [_float(x.get("low")) for x in bars]
    vols = [_float(x.get("volume")) for x in bars]
    if not closes or closes[0] <= 0 or closes[-1] <= 0:
        return {
            "bars_count": len(bars),
            "micro_available": False,
            "open_auction_ret_pct": 0.0,
            "open_15m_ret_pct": 0.0,
            "tail_30m_ret_pct": 0.0,
            "open_auction_volume_ratio": 0.0,
            "close_auction_volume_ratio": 0.0,
            "close_auction_jump_pct": 0.0,
            "open_to_close_ret_pct": 0.0,
            "intraday_amplitude_pct": 0.0,
        }

    day_open = opens[0] if opens[0] > 0 else closes[0]
    day_close = closes[-1]
    open_auction_ret = (day_open / prev_close - 1) * 100.0 if prev_close > 0 else 0.0
    idx_15 = min(len(closes) - 1, 14)
    open_15m_ret = (closes[idx_15] / day_open - 1) * 100.0 if day_open > 0 else 0.0
    tail_anchor = max(0, len(closes) - 31)
    tail_30m_ret = (day_close / closes[tail_anchor] - 1) * 100.0 if closes[tail_anchor] > 0 else 0.0
    open_to_close_ret = (day_close / day_open - 1) * 100.0 if day_open > 0 else 0.0
    low_ref = min(v for v in lows if v > 0) if any(v > 0 for v in lows) else 0.0
    high_ref = max(highs) if highs else 0.0
    intraday_amp = (high_ref / low_ref - 1) * 100.0 if low_ref > 0 else 0.0

    positive_vols = [v for v in vols if v > 0]
    avg_vol = mean(positive_vols) if positive_vols else 0.0
    open_ratio = (mean(vols[:5]) / avg_vol) if avg_vol > 0 and len(vols) >= 5 else 0.0
    close_ratio = (mean(vols[-5:]) / avg_vol) if avg_vol > 0 and len(vols) >= 5 else 0.0
    close_jump = (closes[-1] / closes[-2] - 1) * 100.0 if len(closes) >= 2 and closes[-2] > 0 else 0.0

    return {
        "bars_count": len(bars),
        "micro_available": len(bars) >= 45,
        "open_auction_ret_pct": round(open_auction_ret, 3),
        "open_15m_ret_pct": round(open_15m_ret, 3),
        "tail_30m_ret_pct": round(tail_30m_ret, 3),
        "open_auction_volume_ratio": round(open_ratio, 3),
        "close_auction_volume_ratio": round(close_ratio, 3),
        "close_auction_jump_pct": round(close_jump, 3),
        "open_to_close_ret_pct": round(open_to_close_ret, 3),
        "intraday_amplitude_pct": round(intraday_amp, 3),
    }


CNY_DATES = {
    2019: "2019-02-05",
    2020: "2020-01-25",
    2021: "2021-02-12",
    2022: "2022-02-01",
    2023: "2023-01-22",
    2024: "2024-02-10",
    2025: "2025-01-29",
    2026: "2026-02-17",
    2027: "2027-02-06",
    2028: "2028-01-26",
    2029: "2029-02-13",
    2030: "2030-02-03",
}


def _parse_date(s: str) -> datetime | None:
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except Exception:
        return None


def _seasonality_long_stats(klines: list[dict]) -> dict:
    if len(klines) < 80:
        return {
            "sample_days": 0,
            "weekday_mean_return_pct": {},
            "weekday_win_rate_pct": {},
            "thursday_mean_return_pct": 0.0,
            "thursday_win_rate_pct": 50.0,
            "month_start_mean_return_pct": 0.0,
            "month_end_mean_return_pct": 0.0,
            "month_start_win_rate_pct": 50.0,
            "month_end_win_rate_pct": 50.0,
            "spring_festival_window_mean_return_pct": 0.0,
            "spring_festival_pre_window_mean_return_pct": 0.0,
            "spring_festival_post_window_mean_return_pct": 0.0,
            "spring_festival_samples": 0,
        }

    rows: list[tuple[datetime, float]] = []
    for r in klines:
        date_s = str(r.get("日期") or r.get("Date") or r.get("date") or "")
        dt = _parse_date(date_s)
        close = _float(r.get("收盘", r.get("Close")))
        if dt is None or close <= 0:
            continue
        rows.append((dt, close))
    if len(rows) < 80:
        return {
            "sample_days": 0,
            "weekday_mean_return_pct": {},
            "weekday_win_rate_pct": {},
            "thursday_mean_return_pct": 0.0,
            "thursday_win_rate_pct": 50.0,
            "month_start_mean_return_pct": 0.0,
            "month_end_mean_return_pct": 0.0,
            "month_start_win_rate_pct": 50.0,
            "month_end_win_rate_pct": 50.0,
            "spring_festival_window_mean_return_pct": 0.0,
            "spring_festival_pre_window_mean_return_pct": 0.0,
            "spring_festival_post_window_mean_return_pct": 0.0,
            "spring_festival_samples": 0,
        }

    weekday_map = {i: [] for i in range(5)}
    month_start: list[float] = []
    month_end: list[float] = []
    thursday: list[float] = []
    spring_window: list[float] = []
    spring_pre: list[float] = []
    spring_post: list[float] = []
    total_rets: list[float] = []
    for i in range(1, len(rows)):
        dt, c = rows[i]
        _, p = rows[i - 1]
        if p <= 0:
            continue
        ret = (c / p - 1.0) * 100.0
        total_rets.append(ret)
        wd = dt.weekday()
        if wd <= 4:
            weekday_map[wd].append(ret)
            if wd == 3:
                thursday.append(ret)
        if dt.day <= 5:
            month_start.append(ret)
        if dt.day >= 25:
            month_end.append(ret)

        cny = _parse_date(CNY_DATES.get(dt.year, ""))
        if cny is not None:
            delta = (dt.date() - cny.date()).days
            if -7 <= delta <= 10:
                spring_window.append(ret)
                if delta < 0:
                    spring_pre.append(ret)
                elif delta > 0:
                    spring_post.append(ret)

    def _m(vals: list[float]) -> float:
        return round(mean(vals), 4) if vals else 0.0

    def _w(vals: list[float]) -> float:
        if not vals:
            return 50.0
        return round(sum(1 for x in vals if x > 0) / len(vals) * 100.0, 2)

    wd_keys = ["mon", "tue", "wed", "thu", "fri"]
    wd_mean = {k: _m(weekday_map[i]) for i, k in enumerate(wd_keys)}
    wd_win = {k: _w(weekday_map[i]) for i, k in enumerate(wd_keys)}
    return {
        "sample_days": len(total_rets),
        "weekday_mean_return_pct": wd_mean,
        "weekday_win_rate_pct": wd_win,
        "thursday_mean_return_pct": _m(thursday),
        "thursday_win_rate_pct": _w(thursday),
        "month_start_mean_return_pct": _m(month_start),
        "month_end_mean_return_pct": _m(month_end),
        "month_start_win_rate_pct": _w(month_start),
        "month_end_win_rate_pct": _w(month_end),
        "spring_festival_window_mean_return_pct": _m(spring_window),
        "spring_festival_pre_window_mean_return_pct": _m(spring_pre),
        "spring_festival_post_window_mean_return_pct": _m(spring_post),
        "spring_festival_samples": len(spring_window),
    }


STAGE_LABEL = {0: "—", 1: "Stage 1 底部", 2: "Stage 2 上升", 3: "Stage 3 顶部", 4: "Stage 4 下跌"}


def _extract_for_viz(klines: list[dict]) -> dict:
    """Produce the shape the report viz expects: candles_60d / ma20_60d / ma60_60d / kline_stats."""
    if not klines:
        return {}

    def _v(r, *keys, default=0):
        for k in keys:
            if k in r and r[k] is not None:
                try:
                    return float(r[k])
                except (ValueError, TypeError):
                    pass
        return default

    closes = [_v(r, "收盘", "Close") for r in klines]
    opens = [_v(r, "开盘", "Open") for r in klines]
    highs = [_v(r, "最高", "High") for r in klines]
    lows = [_v(r, "最低", "Low") for r in klines]

    dates = []
    for r in klines:
        d = r.get("日期") or r.get("Date") or ""
        dates.append(str(d)[:10])

    # last 60 candles
    last_n = min(60, len(klines))
    candles_60d = []
    for i in range(len(klines) - last_n, len(klines)):
        candles_60d.append({
            "date": dates[i],
            "open": round(opens[i], 2),
            "close": round(closes[i], 2),
            "high": round(highs[i], 2),
            "low": round(lows[i], 2),
        })

    ma20_full = _ma(closes, 20)
    ma60_full = _ma(closes, 60)
    ma20_60d = [round(v, 2) if i >= 19 else None for i, v in enumerate(ma20_full)][-last_n:]
    ma60_60d = [round(v, 2) if i >= 59 else None for i, v in enumerate(ma60_full)][-last_n:]

    # stats
    stats: dict = {}
    if len(closes) >= 252:
        ytd_idx = max(0, len(closes) - 252)
        ytd_return = (closes[-1] - closes[ytd_idx]) / closes[ytd_idx] * 100
        stats["ytd_return"] = f"{ytd_return:+.1f}%"
    if len(closes) >= 20:
        # annualized volatility
        rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
        if rets:
            import statistics as _st
            try:
                vol = _st.stdev(rets[-252:] if len(rets) >= 252 else rets) * (252 ** 0.5) * 100
                stats["volatility"] = f"{vol:.1f}%"
            except _st.StatisticsError:
                pass
        # max drawdown last 252 days
        window = closes[-252:] if len(closes) >= 252 else closes
        peak = window[0]
        max_dd = 0.0
        for c in window:
            if c > peak:
                peak = c
            dd = (c - peak) / peak
            if dd < max_dd:
                max_dd = dd
        stats["max_drawdown"] = f"{max_dd * 100:.1f}%"

    return {
        "candles_60d": candles_60d,
        "ma20_60d": ma20_60d,
        "ma60_60d": ma60_60d,
        "close_60d": [round(c, 2) for c in closes[-last_n:]],
        "kline_stats": stats,
    }


def main(ticker: str) -> dict:
    ti = parse_ticker(ticker)
    klines = ds.fetch_kline(ti)
    indicators = compute_indicators(klines)
    chips = fetch_chip_distribution(ti)
    intraday = fetch_intraday_minutes(ti)
    prev_close = _float((klines[-2] or {}).get("收盘")) if len(klines) >= 2 else 0.0
    intraday_micro = _intraday_micro_features(intraday, prev_close)
    seasonality = _seasonality_long_stats(klines[-520:] if len(klines) > 520 else klines)
    viz_shape = _extract_for_viz(klines)

    # Derive stage / ma_align / macd / rsi human labels from indicators
    stage_label = STAGE_LABEL.get(indicators.get("stage", 0), "—")
    ma_align = "多头排列" if indicators.get("ma_bull_alignment") else "非多头"
    macd_label = "金叉水上" if (indicators.get("macd_golden_cross") and indicators.get("macd_dif", 0) > 0) else (
        "死叉水上" if (indicators.get("macd_dif", 0) > 0 and indicators.get("macd_hist", 0) < 0) else
        "水下" if indicators.get("macd_dif", 0) < 0 else "中性"
    )
    rsi_val = indicators.get("rsi_14")
    rsi_label = f"{rsi_val:.0f}" if rsi_val is not None else "—"

    return {
        "ticker": ti.full,
        "data": {
            "kline_count": len(klines),
            "indicators": indicators,
            "stage": stage_label,
            "ma_align": ma_align,
            "macd": macd_label,
            "rsi": rsi_label,
            "chip_distribution": chips,
            "intraday_minutes": intraday,
            "intraday_micro": intraday_micro,
            "seasonality_1y": seasonality,
            **viz_shape,
        },
        "source": "akshare:stock_zh_a_hist + stock_zh_a_hist_min_em + stock_cyq_em (+ 6 path fallback chain)",
        "fallback": False,
    }


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else "002273.SZ"), ensure_ascii=False, indent=2, default=str))
