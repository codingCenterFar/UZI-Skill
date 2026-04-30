"""Strategy engine skeleton (Phase 1).

This module provides:
- stable strategy output schema
- depth-aware strategy registry execution
- lightweight baseline signals with safe fallback behavior
"""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from statistics import mean, pstdev
from typing import Callable

from .stock_features import extract_features
from .strategy_registry import StrategySpec, all_strategies, enabled_strategies


def _f(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        s = str(v).replace(",", "").replace("%", "").replace("+", "").replace("亿", "").strip()
        if not s or s in ("—", "-", "None", "nan", "N/A"):
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


def _get_dim_data(raw: dict, key: str) -> dict:
    return ((raw or {}).get("dimensions", {}).get(key) or {}).get("data") or {}


def _candles_60d(raw: dict) -> list[dict]:
    c = _get_dim_data(raw, "2_kline").get("candles_60d") or []
    return c if isinstance(c, list) else []


def _close_series(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        v = _f(c.get("close"), default=None)  # type: ignore[arg-type]
        if v is None:
            continue
        out.append(float(v))
    return out


def _open_series(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        v = _f(c.get("open"), default=None)  # type: ignore[arg-type]
        if v is None:
            continue
        out.append(float(v))
    return out


def _high_series(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        v = _f(c.get("high"), default=None)  # type: ignore[arg-type]
        if v is None:
            continue
        out.append(float(v))
    return out


def _low_series(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        v = _f(c.get("low"), default=None)  # type: ignore[arg-type]
        if v is None:
            continue
        out.append(float(v))
    return out


def _rolling_mean(vals: list[float], n: int) -> float:
    if len(vals) < max(1, n):
        return 0.0
    return mean(vals[-n:])


def _rolling_std(vals: list[float], n: int) -> float:
    if len(vals) < max(2, n):
        return 0.0
    return pstdev(vals[-n:])


def _donchian_stats(candles: list[dict], n: int = 20) -> dict:
    highs = _high_series(candles)
    lows = _low_series(candles)
    closes = _close_series(candles)
    if len(highs) <= n or len(lows) <= n or len(closes) <= n:
        return {
            "upper": 0.0,
            "lower": 0.0,
            "width_pct": 0.0,
            "breakout_up": False,
            "breakout_down": False,
            "n_day_new_high": False,
        }
    upper = max(highs[-n - 1:-1])
    lower = min(lows[-n - 1:-1])
    close = closes[-1]
    width_pct = (upper - lower) / close * 100 if close > 0 else 0.0
    breakout_up = close >= upper * 1.001 if upper > 0 else False
    breakout_down = close <= lower * 0.999 if lower > 0 else False
    n_day_new_high = close >= max(closes[-n:]) * 0.998
    return {
        "upper": round(upper, 3),
        "lower": round(lower, 3),
        "width_pct": round(width_pct, 3),
        "breakout_up": breakout_up,
        "breakout_down": breakout_down,
        "n_day_new_high": n_day_new_high,
    }


def _adx14(candles: list[dict]) -> dict:
    highs = _high_series(candles)
    lows = _low_series(candles)
    closes = _close_series(candles)
    n = 14
    if len(highs) < n + 2 or len(lows) < n + 2 or len(closes) < n + 2:
        return {"adx14": 0.0, "plus_di14": 0.0, "minus_di14": 0.0}

    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(highs)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus = up_move if up_move > down_move and up_move > 0 else 0.0
        minus = down_move if down_move > up_move and down_move > 0 else 0.0
        tr_i = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr.append(tr_i)
        plus_dm.append(plus)
        minus_dm.append(minus)

    if len(tr) < n + 1:
        return {"adx14": 0.0, "plus_di14": 0.0, "minus_di14": 0.0}

    tr_n = sum(tr[:n])
    plus_n = sum(plus_dm[:n])
    minus_n = sum(minus_dm[:n])
    dx_vals: list[float] = []
    plus_di_last = 0.0
    minus_di_last = 0.0
    for i in range(n, len(tr)):
        tr_n = tr_n - tr_n / n + tr[i]
        plus_n = plus_n - plus_n / n + plus_dm[i]
        minus_n = minus_n - minus_n / n + minus_dm[i]
        if tr_n <= 0:
            continue
        plus_di = 100.0 * plus_n / tr_n
        minus_di = 100.0 * minus_n / tr_n
        denom = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0
        dx_vals.append(dx)
        plus_di_last = plus_di
        minus_di_last = minus_di

    adx = _rolling_mean(dx_vals, n) if dx_vals else 0.0
    return {
        "adx14": round(adx, 3),
        "plus_di14": round(plus_di_last, 3),
        "minus_di14": round(minus_di_last, 3),
    }


def _bollinger_stats(closes: list[float], n: int = 20, k: float = 2.0) -> dict:
    if len(closes) < n:
        return {
            "mid": 0.0,
            "upper": 0.0,
            "lower": 0.0,
            "zscore": 0.0,
            "near_upper": False,
            "near_lower": False,
        }
    mid = _rolling_mean(closes, n)
    std = _rolling_std(closes, n)
    upper = mid + k * std
    lower = mid - k * std
    close = closes[-1]
    z = (close - mid) / std if std > 1e-9 else 0.0
    return {
        "mid": round(mid, 3),
        "upper": round(upper, 3),
        "lower": round(lower, 3),
        "zscore": round(z, 3),
        "near_upper": close >= upper * 0.995 if upper > 0 else False,
        "near_lower": close <= lower * 1.005 if lower > 0 else False,
    }


def _kdj_stats(candles: list[dict], n: int = 9) -> dict:
    highs = _high_series(candles)
    lows = _low_series(candles)
    closes = _close_series(candles)
    if len(closes) < n:
        return {"k": 50.0, "d": 50.0, "j": 50.0}
    k_val = 50.0
    d_val = 50.0
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        rsv = 50.0 if hh <= ll else (closes[i] - ll) / (hh - ll) * 100.0
        k_val = 2.0 / 3.0 * k_val + 1.0 / 3.0 * rsv
        d_val = 2.0 / 3.0 * d_val + 1.0 / 3.0 * k_val
    j_val = 3.0 * k_val - 2.0 * d_val
    return {"k": round(k_val, 3), "d": round(d_val, 3), "j": round(j_val, 3)}


def _cci20(candles: list[dict], n: int = 20) -> float:
    if len(candles) < n:
        return 0.0
    tp: list[float] = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        h = _f(c.get("high"))
        l = _f(c.get("low"))
        cl = _f(c.get("close"))
        if h <= 0 or l <= 0 or cl <= 0:
            continue
        tp.append((h + l + cl) / 3.0)
    if len(tp) < n:
        return 0.0
    ma = _rolling_mean(tp, n)
    md = mean([abs(v - ma) for v in tp[-n:]]) if n > 0 else 0.0
    if md <= 1e-9:
        return 0.0
    return round((tp[-1] - ma) / (0.015 * md), 3)


def _gap_fill_stats(candles: list[dict], lookback: int = 20) -> dict:
    if len(candles) < lookback + 2:
        return {
            "up_gap_count": 0,
            "down_gap_count": 0,
            "up_gap_fill_rate_pct": 0.0,
            "down_gap_fill_rate_pct": 0.0,
        }
    start = max(1, len(candles) - lookback)
    up_total = up_fill = 0
    down_total = down_fill = 0
    for i in range(start, len(candles)):
        prev = candles[i - 1]
        cur = candles[i]
        if not isinstance(prev, dict) or not isinstance(cur, dict):
            continue
        prev_high = _f(prev.get("high"))
        prev_low = _f(prev.get("low"))
        cur_open = _f(cur.get("open"))
        if prev_high <= 0 or prev_low <= 0 or cur_open <= 0:
            continue
        nxt = candles[i:min(len(candles), i + 6)]
        if cur_open >= prev_high * 1.005:
            up_total += 1
            if any(_f(x.get("low")) <= prev_high for x in nxt if isinstance(x, dict)):
                up_fill += 1
        if cur_open <= prev_low * 0.995:
            down_total += 1
            if any(_f(x.get("high")) >= prev_low for x in nxt if isinstance(x, dict)):
                down_fill += 1
    return {
        "up_gap_count": up_total,
        "down_gap_count": down_total,
        "up_gap_fill_rate_pct": round(up_fill / max(up_total, 1) * 100, 2),
        "down_gap_fill_rate_pct": round(down_fill / max(down_total, 1) * 100, 2),
    }


def _window_return(closes: list[float], n: int) -> float:
    if len(closes) <= n:
        return 0.0
    prev = closes[-1 - n]
    if prev == 0:
        return 0.0
    return (closes[-1] / prev - 1) * 100


def _daily_close_returns(closes: list[float]) -> list[float]:
    rets: list[float] = []
    for i in range(1, len(closes)):
        p0 = closes[i - 1]
        p1 = closes[i]
        if p0 == 0:
            continue
        rets.append((p1 / p0 - 1) * 100)
    return rets


def _etf_pair_hint(
    size_style: str,
    style_bias: str,
    industry: str,
    mom20: float,
    ytd: float,
) -> dict:
    pair_leg = "沪深300ETF"
    hedge_leg = "中证1000ETF"
    pair_theme = "core_beta_spread"

    if style_bias == "growth_tech":
        pair_leg = "科创50/创业板ETF"
        hedge_leg = "红利低波ETF"
        pair_theme = "growth_vs_dividend"
    elif style_bias == "value_dividend":
        pair_leg = "红利低波/央企红利ETF"
        hedge_leg = "科创50/创业板ETF"
        pair_theme = "dividend_vs_growth"
    elif size_style == "small_cap":
        pair_leg = "中证1000ETF"
        hedge_leg = "沪深300ETF"
        pair_theme = "small_vs_large"
    elif size_style == "large_cap":
        pair_leg = "沪深300ETF"
        hedge_leg = "中证1000ETF"
        pair_theme = "large_vs_small"

    industry_overlay = "none"
    if any(k in industry for k in ("半导体", "光学", "电子", "算力", "软件", "人工智能")):
        industry_overlay = "科技主题ETF"
    elif any(k in industry for k in ("银行", "煤炭", "电力", "公用", "运营商", "石油", "红利")):
        industry_overlay = "红利/高股息ETF"
    elif any(k in industry for k in ("医药", "创新药", "生物")):
        industry_overlay = "医药ETF"

    pair_bias = mom20 * 0.7 + ytd * 0.08
    if style_bias in ("growth_tech", "value_dividend"):
        pair_bias += 1.2
    if size_style == "small_cap":
        pair_bias += 0.5
    elif size_style == "large_cap":
        pair_bias += 0.3

    return {
        "etf_pair_leg": pair_leg,
        "etf_hedge_leg": hedge_leg,
        "etf_pair_theme": pair_theme,
        "industry_overlay": industry_overlay,
        "etf_pair_bias_score": round(max(-15.0, min(15.0, pair_bias)), 3),
    }


def _atr14(candles: list[dict]) -> dict:
    if len(candles) < 15:
        return {"atr14": 0.0, "atr14_pct": 0.0}
    tr: list[float] = []
    prev_close: float | None = None
    for c in candles:
        if not isinstance(c, dict):
            continue
        high = _f(c.get("high"))
        low = _f(c.get("low"))
        close = _f(c.get("close"))
        if high <= 0 or low <= 0 or close <= 0:
            continue
        if prev_close is None:
            tr.append(max(0.0, high - low))
        else:
            tr.append(
                max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close),
                )
            )
        prev_close = close
    if len(tr) < 14:
        return {"atr14": 0.0, "atr14_pct": 0.0}
    atr14 = mean(tr[-14:])
    close_last = _f(candles[-1].get("close")) if candles and isinstance(candles[-1], dict) else 0.0
    atr14_pct = atr14 / close_last * 100 if close_last > 0 else 0.0
    return {"atr14": round(atr14, 4), "atr14_pct": round(atr14_pct, 4)}


def _ivol_stats(rets: list[float]) -> dict:
    r20 = rets[-20:] if len(rets) >= 5 else rets
    r60 = rets[-60:] if len(rets) >= 5 else rets
    iv20 = pstdev(r20) if len(r20) > 1 else 0.0
    iv60 = pstdev(r60) if len(r60) > 1 else 0.0
    annual20 = iv20 * (252.0 ** 0.5)
    annual60 = iv60 * (252.0 ** 0.5)
    return {
        "ivol_20d_daily_pct": round(iv20, 4),
        "ivol_60d_daily_pct": round(iv60, 4),
        "ivol_20d_annual_pct": round(annual20, 3),
        "ivol_60d_annual_pct": round(annual60, 3),
    }


def _vol_targeting_proxy(ivol_20d_daily_pct: float, target_annual_vol_pct: float = 24.0) -> dict:
    # Convert annualized target vol to daily std (in %-point space).
    target_daily = target_annual_vol_pct / (252.0 ** 0.5)
    if ivol_20d_daily_pct <= 1e-9:
        leverage = 1.0
    else:
        leverage = target_daily / ivol_20d_daily_pct
    leverage = max(0.35, min(1.8, leverage))
    return {
        "target_annual_vol_pct": round(target_annual_vol_pct, 2),
        "target_daily_vol_pct": round(target_daily, 4),
        "target_leverage": round(leverage, 3),
    }


def _risk_parity_proxy(vol_1y_pct: float, ivol_annual_pct: float, atr_pct: float, max_drawdown_pct: float) -> dict:
    vol1 = max(vol_1y_pct, 1.0)
    vol2 = max(ivol_annual_pct, 1.0)
    inv1 = 1.0 / vol1
    inv2 = 1.0 / vol2
    rp_weight = inv1 / max(inv1 + inv2, 1e-9) * 100.0
    budget = (
        100.0
        - 1.0 * vol_1y_pct
        - 0.6 * abs(min(max_drawdown_pct, 0.0))
        - 1.8 * atr_pct
        - 0.4 * max(ivol_annual_pct - 22.0, 0.0)
    )
    budget = max(0.0, min(100.0, budget))
    return {
        "risk_parity_weight_proxy_pct": round(max(0.0, min(100.0, rp_weight)), 3),
        "risk_budget_score": round(budget, 3),
    }


def _beta_neutral_proxy(
    candles: list[dict],
    features: dict,
    size_style: str,
    style_bias: str,
) -> dict:
    closes = _close_series(candles)
    rets = _daily_close_returns(closes)
    sample_rets = rets[-40:] if len(rets) >= 10 else rets
    daily_std = pstdev(sample_rets) if len(sample_rets) > 1 else 0.0
    vol_1y = _f(features.get("volatility_1y"))
    if daily_std <= 1e-9 and vol_1y > 0:
        daily_std = vol_1y / (252.0 ** 0.5)

    atr = _atr14(candles)
    atr14_pct = _f(atr.get("atr14_pct"))

    market_daily_proxy = 1.2
    if size_style == "small_cap":
        market_daily_proxy += 0.35
    elif size_style == "large_cap":
        market_daily_proxy -= 0.12
    if style_bias == "growth_tech":
        market_daily_proxy += 0.10
    elif style_bias == "value_dividend":
        market_daily_proxy -= 0.08
    if atr14_pct >= 4.5:
        market_daily_proxy += 0.08

    beta_vol = daily_std / max(market_daily_proxy, 0.7)
    beta_vol = max(0.35, min(2.6, beta_vol))
    target_beta = 0.18 if style_bias == "growth_tech" else (0.12 if style_bias == "value_dividend" else 0.15)
    hedge_ratio = max(0.0, min(1.8, beta_vol - target_beta))
    post_hedge_beta = max(0.0, beta_vol - hedge_ratio)

    risk_bucket = "balanced_beta"
    if beta_vol >= 1.6:
        risk_bucket = "high_beta"
    elif beta_vol <= 0.75:
        risk_bucket = "low_beta"

    return {
        "market_daily_vol_proxy_pct": round(market_daily_proxy, 3),
        "realized_daily_vol_40d_pct": round(daily_std, 3),
        "beta_vol_proxy": round(beta_vol, 3),
        "beta_target_proxy": round(target_beta, 3),
        "beta_neutral_hedge_ratio": round(hedge_ratio, 3),
        "post_hedge_beta_proxy": round(post_hedge_beta, 3),
        "beta_risk_bucket": risk_bucket,
    }


def _overnight_intraday(candles: list[dict]) -> tuple[list[float], list[float]]:
    overnight: list[float] = []
    intraday: list[float] = []
    for i in range(1, len(candles)):
        prev = candles[i - 1]
        cur = candles[i]
        if not isinstance(prev, dict) or not isinstance(cur, dict):
            continue
        prev_close = _f(prev.get("close"))
        cur_open = _f(cur.get("open"))
        cur_close = _f(cur.get("close"))
        if prev_close <= 0 or cur_open <= 0:
            continue
        overnight.append((cur_open / prev_close - 1) * 100)
        if cur_open > 0 and cur_close > 0:
            intraday.append((cur_close / cur_open - 1) * 100)
    return overnight, intraday


def _main_flow_20d(raw: dict) -> list[dict]:
    d = _get_dim_data(raw, "12_capital_flow").get("main_fund_flow_20d") or []
    return d if isinstance(d, list) else []


def _sum_main_net_yi(rows: list[dict], n: int) -> float:
    total = 0.0
    for r in rows[:n]:
        if not isinstance(r, dict):
            continue
        total += _f(r.get("主力净流入-净额"))
    return total / 1e8


def _flow_indicator_proxies(rows: list[dict]) -> dict:
    if not rows:
        return {
            "obv_proxy": 0.0,
            "vpt_proxy": 0.0,
            "mfi_proxy": 50.0,
            "cmf_proxy": 0.0,
            "flow_shock_z": 0.0,
            "flow_reversal_days": 0,
        }

    net_amt: list[float] = []
    net_pct: list[float] = []
    chg_pct: list[float] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        net_amt.append(_f(r.get("主力净流入-净额")))
        net_pct.append(_f(r.get("主力净流入-净占比")))
        chg_pct.append(_f(r.get("涨跌幅")))

    if not net_pct:
        return {
            "obv_proxy": 0.0,
            "vpt_proxy": 0.0,
            "mfi_proxy": 50.0,
            "cmf_proxy": 0.0,
            "flow_shock_z": 0.0,
            "flow_reversal_days": 0,
        }

    obv = 0.0
    for r, a in zip(chg_pct, net_amt):
        if r >= 0:
            obv += abs(a) / 1e8
        else:
            obv -= abs(a) / 1e8

    vpt = 0.0
    for r, p in zip(chg_pct, net_pct):
        vpt += (r / 100.0) * p

    pos_flow = sum(max(v, 0.0) for v in net_pct)
    neg_flow = sum(abs(min(v, 0.0)) for v in net_pct)
    if neg_flow <= 1e-9:
        mfi = 100.0 if pos_flow > 0 else 50.0
    else:
        ratio = pos_flow / neg_flow
        mfi = 100.0 - 100.0 / (1.0 + ratio)

    cmf = mean(net_pct[-10:]) if len(net_pct) >= 3 else mean(net_pct)
    recent = net_pct[-5:] if len(net_pct) >= 5 else net_pct
    hist = net_pct[:-5] if len(net_pct) > 5 else net_pct
    hist_mean = mean(hist) if hist else 0.0
    hist_std = pstdev(hist) if len(hist) > 1 else 1.0
    flow_shock_z = (mean(recent) - hist_mean) / max(hist_std, 1e-6)

    rev_days = 0
    for i in range(1, len(net_pct)):
        if net_pct[i] == 0 or net_pct[i - 1] == 0:
            continue
        if net_pct[i] * net_pct[i - 1] < 0:
            rev_days += 1

    return {
        "obv_proxy": round(obv, 3),
        "vpt_proxy": round(vpt, 3),
        "mfi_proxy": round(mfi, 3),
        "cmf_proxy": round(cmf, 3),
        "flow_shock_z": round(flow_shock_z, 3),
        "flow_reversal_days": rev_days,
    }


def _industry_growth_pct(raw: dict, features: dict) -> float:
    ind = _get_dim_data(raw, "7_industry")
    return _f(ind.get("growth"), default=_f(features.get("industry_growth_pct")))


def _safe_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    head = s.split("·", 1)[0].strip()
    if len(head) >= 10:
        head = head[:10]
    try:
        return datetime.strptime(head, "%Y-%m-%d")
    except Exception:
        return None


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


def _spring_festival_delta_days(dt: datetime) -> int | None:
    cny_s = CNY_DATES.get(dt.year)
    if not cny_s:
        return None
    cny = _safe_date(cny_s)
    if cny is None:
        return None
    return (dt.date() - cny.date()).days


def _candle_date(c: dict) -> datetime | None:
    if not isinstance(c, dict):
        return None
    for key in ("date", "day", "time", "datetime"):
        raw = c.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        if len(s) >= 10:
            s = s[:10]
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            continue
    return None


def _event_date_from_item(item: dict | str) -> datetime | None:
    if isinstance(item, dict):
        for key in ("date", "日期", "day", "time", "datetime"):
            if key in item and item.get(key):
                dt = _safe_date(str(item.get(key)))
                if dt:
                    return dt
        title = str(item.get("title") or item.get("event") or "")
        dt = _safe_date(title)
        if dt:
            return dt
        return None
    return _safe_date(str(item))


def _event_signal_blocks(events: dict) -> list[dict]:
    out: list[dict] = []
    for line in events.get("event_timeline") or []:
        text = str(line or "")
        if not text.strip():
            continue
        out.append({"date": _event_date_from_item(text), "text": text, "source": "timeline"})
    for n in events.get("recent_news") or []:
        if not isinstance(n, dict):
            continue
        text = str(n.get("title") or "")
        if not text.strip():
            continue
        out.append({"date": _event_date_from_item(n), "text": text, "source": "news"})
    for n in events.get("recent_notices") or []:
        if not isinstance(n, dict):
            continue
        text = str(n.get("title") or "")
        if not text.strip():
            continue
        out.append({"date": _event_date_from_item(n), "text": text, "source": "notice"})
    for c in events.get("catalyst") or []:
        if isinstance(c, dict):
            text = str(c.get("event") or "")
            if not text.strip():
                continue
            out.append({"date": _event_date_from_item(c), "text": text, "source": "catalyst"})
        else:
            text = str(c or "")
            if not text.strip():
                continue
            out.append({"date": _event_date_from_item(text), "text": text, "source": "catalyst"})
    return out


def _surprise_score_text(text: str) -> int:
    pos_rules = [
        ("预增", 2), ("扭亏", 2), ("超预期", 2), ("增长", 1), ("回购", 1),
        ("分红", 1), ("中标", 1), ("合同", 1), ("获批", 1), ("新品", 1),
    ]
    neg_rules = [
        ("预减", -2), ("预亏", -2), ("亏损", -2), ("下修", -2), ("减值", -1),
        ("减持", -1), ("诉讼", -1), ("处罚", -1), ("立案", -1), ("退市", -2),
    ]
    score = 0
    s = (text or "").lower()
    for kw, w in pos_rules:
        if kw in s:
            score += w
    for kw, w in neg_rules:
        if kw in s:
            score += w
    return score


def _event_surprise_stats(events: dict) -> dict:
    blocks = _event_signal_blocks(events)
    pos = neg = 0
    net = 0
    strongest_pos = ""
    strongest_neg = ""
    pos_best = -999
    neg_best = 999
    for b in blocks:
        text = str(b.get("text") or "")
        sc = _surprise_score_text(text)
        net += sc
        if sc > 0:
            pos += 1
            if sc > pos_best:
                pos_best = sc
                strongest_pos = text[:80]
        elif sc < 0:
            neg += 1
            if sc < neg_best:
                neg_best = sc
                strongest_neg = text[:80]
    return {
        "surprise_net": net,
        "surprise_positive_count": pos,
        "surprise_negative_count": neg,
        "strongest_positive": strongest_pos,
        "strongest_negative": strongest_neg,
        "blocks": blocks,
    }


def _pead_proxy(candles: list[dict], events: dict) -> dict:
    if len(candles) < 8:
        return {
            "pead_samples": 0,
            "pead_drift_1d_pct": 0.0,
            "pead_drift_5d_pct": 0.0,
            "pead_drift_20d_pct": 0.0,
        }
    date_to_idx: dict[str, int] = {}
    closes = _close_series(candles)
    for i, c in enumerate(candles):
        dt = _candle_date(c)
        if not dt:
            continue
        date_to_idx[dt.strftime("%Y-%m-%d")] = i

    rows = _event_signal_blocks(events)
    d1: list[float] = []
    d5: list[float] = []
    d20: list[float] = []
    for r in rows:
        dt = r.get("date")
        text = str(r.get("text") or "")
        if not isinstance(dt, datetime):
            continue
        key = dt.strftime("%Y-%m-%d")
        if key not in date_to_idx:
            continue
        idx = date_to_idx[key]
        if idx + 1 >= len(closes):
            continue
        score = _surprise_score_text(text)
        if score == 0:
            continue
        sign = 1.0 if score > 0 else -1.0
        c0 = closes[idx]
        c1 = closes[idx + 1] if idx + 1 < len(closes) else c0
        c5 = closes[min(idx + 5, len(closes) - 1)]
        c20 = closes[min(idx + 20, len(closes) - 1)]
        if c0 <= 0:
            continue
        d1.append(sign * (c1 / c0 - 1.0) * 100.0)
        d5.append(sign * (c5 / c0 - 1.0) * 100.0)
        d20.append(sign * (c20 / c0 - 1.0) * 100.0)

    return {
        "pead_samples": len(d5),
        "pead_drift_1d_pct": round(mean(d1), 3) if d1 else 0.0,
        "pead_drift_5d_pct": round(mean(d5), 3) if d5 else 0.0,
        "pead_drift_20d_pct": round(mean(d20), 3) if d20 else 0.0,
    }


def _limit_up_stats(candles: list[dict], market: str) -> dict:
    closes = _close_series(candles)
    rets = _daily_close_returns(closes)
    if not rets:
        return {"limit_hits": 0, "broken_hits": 0, "broken_rate": 0.0, "max_streak": 0}

    up_th = 9.5 if market == "A" else 14.0
    dn_fail = -3.0 if market == "A" else -5.0
    hits = [i for i, r in enumerate(rets) if r >= up_th]
    broken = 0
    for i in hits:
        nxt = i + 1
        if nxt < len(rets) and rets[nxt] <= dn_fail:
            broken += 1

    # consecutive streak on returns index
    max_streak = 0
    cur = 0
    hit_set = set(hits)
    for i in range(len(rets)):
        if i in hit_set:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0

    return {
        "limit_hits": len(hits),
        "broken_hits": broken,
        "broken_rate": (broken / len(hits) * 100) if hits else 0.0,
        "max_streak": max_streak,
    }


def _board_behavior_proxies(candles: list[dict], rows: list[dict]) -> dict:
    closes = _close_series(candles)
    rets = _daily_close_returns(closes)
    tail_rets = rets[-20:] if len(rets) > 20 else rets
    strong_up_idx = [i for i, r in enumerate(tail_rets) if r >= 7.0]

    pullback = 0
    for i in strong_up_idx:
        nxt = i + 1
        if nxt < len(tail_rets) and tail_rets[nxt] <= -2.0:
            pullback += 1
    fake_board_rate = pullback / max(len(strong_up_idx), 1) * 100.0

    net_pct = []
    for r in rows[-20:]:
        if isinstance(r, dict):
            net_pct.append(_f(r.get("主力净流入-净占比")))
    horizon = min(len(tail_rets), len(net_pct))
    flow_decay = 0.0
    decay_cnt = 0
    if horizon >= 3:
        rets_h = tail_rets[-horizon:]
        pct_h = net_pct[-horizon:]
        for i, rv in enumerate(rets_h):
            if rv < 7.0:
                continue
            nxt = i + 1
            if nxt < len(pct_h):
                flow_decay += (pct_h[i] - pct_h[nxt])
                decay_cnt += 1

    return {
        "strong_up_days_20d": len(strong_up_idx),
        "fake_board_rate_pct": round(fake_board_rate, 2),
        "flow_decay_after_spike_pct": round(flow_decay / max(decay_cnt, 1), 3),
    }


def _limit_pattern_stats(candles: list[dict], market: str) -> dict:
    closes = _close_series(candles)
    opens = _open_series(candles)
    highs = _high_series(candles)
    lows = _low_series(candles)
    rets = _daily_close_returns(closes)
    if not rets or len(closes) < 3:
        return {
            "first_board_hits": 0,
            "second_board_hits": 0,
            "third_plus_board_hits": 0,
            "one_word_board_hits": 0,
            "t_board_hits": 0,
            "first_yin_break_hits": 0,
            "first_break_after_streak_hits": 0,
            "height_cycle_score": 0.0,
            "height_cycle_label": "flat",
        }

    up_th = 9.5 if market == "A" else 14.0
    limit_days = [i for i, r in enumerate(rets) if r >= up_th]
    limit_set = set(limit_days)
    if not limit_days:
        return {
            "first_board_hits": 0,
            "second_board_hits": 0,
            "third_plus_board_hits": 0,
            "one_word_board_hits": 0,
            "t_board_hits": 0,
            "first_yin_break_hits": 0,
            "first_break_after_streak_hits": 0,
            "height_cycle_score": 0.0,
            "height_cycle_label": "flat",
        }

    streaks: list[tuple[int, int, int]] = []
    run_pos: dict[int, int] = {}
    i = 0
    while i < len(rets):
        if i not in limit_set:
            i += 1
            continue
        s = i
        while i + 1 in limit_set:
            i += 1
        e = i
        ln = e - s + 1
        streaks.append((s, e, ln))
        for k in range(s, e + 1):
            run_pos[k] = k - s + 1
        i += 1

    first_board_hits = 0
    second_board_hits = 0
    third_plus_board_hits = 0
    one_word_board_hits = 0
    t_board_hits = 0
    first_yin_break_hits = 0
    first_break_after_streak_hits = 0

    for idx in limit_days:
        pos = run_pos.get(idx, 1)
        if pos == 1:
            first_board_hits += 1
        elif pos == 2:
            second_board_hits += 1
        elif pos >= 3:
            third_plus_board_hits += 1

        c_idx = idx + 1
        if c_idx >= len(closes) or c_idx >= len(opens) or c_idx >= len(highs) or c_idx >= len(lows):
            continue
        c = closes[c_idx]
        o = opens[c_idx]
        h = highs[c_idx]
        l = lows[c_idx]
        if c <= 0:
            continue
        day_range = max(h - l, 0.0)
        upper_shadow = max(h - max(o, c), 0.0)
        lower_shadow = max(min(o, c) - l, 0.0)
        # 日线近似识别一字板/ T 字板（分钟序列在 T313 再接入）
        if day_range / c <= 0.012 and abs(c - o) / c <= 0.004 and upper_shadow / c <= 0.004:
            one_word_board_hits += 1
        if lower_shadow / c >= 0.02 and upper_shadow / c <= 0.006 and abs(c - o) / c <= 0.01:
            t_board_hits += 1

    for s, e, ln in streaks:
        nxt = e + 1
        if nxt >= len(rets):
            continue
        c_idx = nxt + 1
        if c_idx >= len(closes) or c_idx >= len(opens):
            continue
        is_break = nxt not in limit_set and rets[nxt] <= -2.0
        first_yin = closes[c_idx] < opens[c_idx] and rets[nxt] < 0
        if ln >= 2 and is_break:
            first_break_after_streak_hits += 1
        if ln >= 2 and is_break and first_yin:
            first_yin_break_hits += 1

    max_streak = max(ln for _, _, ln in streaks)
    heat_raw = (
        first_board_hits * 9
        + second_board_hits * 12
        + third_plus_board_hits * 15
        + one_word_board_hits * 8
        + t_board_hits * 6
        - first_break_after_streak_hits * 10
        - first_yin_break_hits * 9
        + max_streak * 7
    )
    height_cycle_score = max(0.0, min(100.0, heat_raw))
    if max_streak >= 5 and first_break_after_streak_hits >= 1:
        height_cycle_label = "blowoff_and_break"
    elif max_streak >= 4:
        height_cycle_label = "high_cycle"
    elif max_streak >= 2:
        height_cycle_label = "mid_cycle"
    else:
        height_cycle_label = "early_cycle"

    return {
        "first_board_hits": first_board_hits,
        "second_board_hits": second_board_hits,
        "third_plus_board_hits": third_plus_board_hits,
        "one_word_board_hits": one_word_board_hits,
        "t_board_hits": t_board_hits,
        "first_yin_break_hits": first_yin_break_hits,
        "first_break_after_streak_hits": first_break_after_streak_hits,
        "height_cycle_score": round(height_cycle_score, 2),
        "height_cycle_label": height_cycle_label,
    }


def _sector_lhb_theme_diffusion(raw: dict) -> dict:
    lhb_data = _get_dim_data(raw, "16_lhb")
    sector_rows = lhb_data.get("sector_lhb_top50") or []
    if not isinstance(sector_rows, list) or not sector_rows:
        return {
            "sector_lhb_sample_n": 0,
            "sector_hot_count": 0,
            "sector_momentum_count": 0,
            "theme_diffusion_score": 0.0,
            "theme_concentration_pct": 0.0,
        }

    rows = [r for r in sector_rows[:30] if isinstance(r, dict)]
    if not rows:
        return {
            "sector_lhb_sample_n": 0,
            "sector_hot_count": 0,
            "sector_momentum_count": 0.0,
            "theme_diffusion_score": 0.0,
            "theme_concentration_pct": 0.0,
        }

    hot = 0
    momentum = 0
    counts: list[float] = []
    for r in rows:
        up_cnt = _f(r.get("上榜次数"))
        m1 = _f(r.get("近1个月涨跌幅"))
        counts.append(max(up_cnt, 0.0))
        if up_cnt >= 6:
            hot += 1
        if m1 >= 20:
            momentum += 1

    total = sum(counts)
    top3 = sum(sorted(counts, reverse=True)[:3]) if counts else 0.0
    concentration = (top3 / total * 100.0) if total > 0 else 0.0
    diffusion = (hot / len(rows) * 60.0) + (momentum / len(rows) * 40.0)
    return {
        "sector_lhb_sample_n": len(rows),
        "sector_hot_count": hot,
        "sector_momentum_count": momentum,
        "theme_diffusion_score": round(max(0.0, min(100.0, diffusion)), 2),
        "theme_concentration_pct": round(max(0.0, min(100.0, concentration)), 2),
    }


def _style_rotation_proxies(features: dict, raw: dict, candles: list[dict]) -> dict:
    market_cap = _f(features.get("market_cap_yi"))
    pe = _f(features.get("pe"))
    pb = _f(features.get("pb"))
    dividend_yield = _f(features.get("dividend_yield"))
    earnings_yield = _f(features.get("earnings_yield"))
    rev_growth = _f(features.get("revenue_growth_latest"))
    np_growth = _f(features.get("net_profit_growth_latest"))
    ind_growth = _industry_growth_pct(raw, features)
    ytd = _f(features.get("ytd_return"))
    closes = _close_series(candles)
    mom20 = _window_return(closes, 20)
    industry = str(features.get("industry") or "")

    growth_kw = ("半导体", "光学", "电池", "算力", "软件", "人工智能", "机器人", "创新药", "军工", "新能源", "电子")
    dividend_kw = ("银行", "煤炭", "电力", "公用", "运营商", "石油", "交运", "港口", "高速", "钢铁", "红利")
    tech_theme = any(k in industry for k in growth_kw)
    dividend_theme = any(k in industry for k in dividend_kw)

    size_style = "mid_cap"
    if market_cap > 0 and market_cap <= 250:
        size_style = "small_cap"
    elif market_cap >= 1200:
        size_style = "large_cap"

    growth_score = 0
    if rev_growth >= 15 or np_growth >= 20:
        growth_score += 1
    if ind_growth >= 10:
        growth_score += 1
    if mom20 >= 6 and ytd >= 0:
        growth_score += 1
    if pe >= 28 or _f(features.get("pe_quantile_5y")) >= 65:
        growth_score += 1
    if tech_theme:
        growth_score += 1

    value_dividend_score = 0
    if earnings_yield >= 6:
        value_dividend_score += 1
    if pb > 0 and pb <= 1.8:
        value_dividend_score += 1
    if dividend_yield >= 2.8:
        value_dividend_score += 1
    if pe > 0 and pe <= 18:
        value_dividend_score += 1
    if dividend_theme:
        value_dividend_score += 1

    style_bias = "balanced"
    if growth_score >= value_dividend_score + 2:
        style_bias = "growth_tech"
    elif value_dividend_score >= growth_score + 2:
        style_bias = "value_dividend"

    return {
        "size_style": size_style,
        "style_bias": style_bias,
        "growth_style_score": growth_score,
        "value_dividend_style_score": value_dividend_score,
        "tech_theme_bias": tech_theme,
        "dividend_theme_bias": dividend_theme,
        "mom_20d_pct": round(mom20, 3),
        "industry_growth_pct": round(ind_growth, 3),
    }


def _regime(features: dict) -> dict:
    stage_num = int(features.get("stage_num") or 0)
    vol = _f(features.get("volatility_1y"))
    heat = _f(features.get("sentiment_heat"))
    turnover = _f(features.get("turnover_rate"))

    if stage_num == 2 and vol < 35:
        market_regime = "trend_up"
    elif vol > 50:
        market_regime = "high_vol"
    else:
        market_regime = "range_or_mixed"

    if heat >= 70:
        sentiment_regime = "crowded_hot"
    elif heat <= 35:
        sentiment_regime = "cold"
    else:
        sentiment_regime = "neutral"

    if turnover >= 8:
        liquidity_regime = "high_turnover"
    elif turnover > 0:
        liquidity_regime = "normal_turnover"
    else:
        liquidity_regime = "unknown"

    return {
        "market_regime": market_regime,
        "sentiment_regime": sentiment_regime,
        "liquidity_regime": liquidity_regime,
    }


def _mk_signal(
    signal: str,
    strength: float,
    confidence: float,
    horizon: str,
    regime_fit: float,
    explain: str,
    evidence: dict | None = None,
) -> dict:
    s = signal if signal in ("bullish", "bearish", "neutral", "skip") else "neutral"
    return {
        "signal": s,
        "strength": round(max(0.0, min(100.0, _f(strength))), 1),
        "confidence": round(max(0.0, min(100.0, _f(confidence))), 1),
        "horizon": horizon if horizon in ("intraday", "swing", "position") else "swing",
        "regime_fit": round(max(0.0, min(100.0, _f(regime_fit))), 1),
        "explain": (explain or "no explanation").strip(),
        "evidence": evidence or {},
    }


def _strategy_trend_momentum(features: dict, regime: dict, ctx: dict) -> dict:
    stage = int(features.get("stage_num") or 0)
    rsi = _f(features.get("rsi"))
    ma_bull = bool(features.get("ma_bull_aligned"))
    ytd = _f(features.get("ytd_return"))
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    closes = _close_series(candles)
    market = str(features.get("market") or "A")
    raw_mom_20d = _window_return(closes, 20)
    daily_rets = _daily_close_returns(closes)
    indicators = _get_dim_data(raw, "2_kline").get("indicators") or {}

    # 修正动量：剔除涨跌停/极端限制日，再看 20d 方向
    extreme_th = 9.5 if market == "A" else 14.0
    filtered = [r for r in daily_rets[-20:] if abs(r) < extreme_th]
    corrected_mom_20d = sum(filtered) if filtered else 0.0
    removed_extreme_days = max(0, len(daily_rets[-20:]) - len(filtered))

    # 隔夜动量
    overnight, intraday = _overnight_intraday(candles)
    ov_mean_10d = mean(overnight[-10:]) if len(overnight) >= 3 else 0.0
    intra_mean_10d = mean(intraday[-10:]) if len(intraday) >= 3 else 0.0

    # 残差动量代理：以行业年化增速做粗略去暴露（Phase1 baseline）
    ind_growth_annual = _industry_growth_pct(raw, features)
    industry_20d_equiv = ind_growth_annual * (20.0 / 252.0)
    residual_proxy_20d = raw_mom_20d - industry_20d_equiv

    # T305 · 趋势增强：ADX + 唐奇安突破 + N 日新高 + 波动放大确认
    adx = _adx14(candles)
    don = _donchian_stats(candles, n=20)
    vol_5_vs_20 = _f(indicators.get("vol_5_vs_20"))
    pct_from_year_high = _f(indicators.get("pct_from_year_high"))

    bull_hits = 0
    bear_hits = 0

    # 经典动量骨架
    if residual_proxy_20d >= 4:
        bull_hits += 1
    if corrected_mom_20d >= 5:
        bull_hits += 1
    if ov_mean_10d > 0 and ov_mean_10d >= intra_mean_10d:
        bull_hits += 1
    if residual_proxy_20d <= -4:
        bear_hits += 1
    if corrected_mom_20d <= -5:
        bear_hits += 1
    if ov_mean_10d < 0 and abs(ov_mean_10d) >= abs(intra_mean_10d):
        bear_hits += 1

    # ADX 趋势强度 + 唐奇安突破 / N 日新高
    if adx["adx14"] >= 22 and adx["plus_di14"] > adx["minus_di14"]:
        bull_hits += 1
    if adx["adx14"] >= 22 and adx["minus_di14"] > adx["plus_di14"]:
        bear_hits += 1
    if don["breakout_up"]:
        bull_hits += 1
    if don["breakout_down"]:
        bear_hits += 1
    if don["n_day_new_high"] and vol_5_vs_20 >= 0.95:
        bull_hits += 1
    if (not don["n_day_new_high"]) and pct_from_year_high <= -16:
        bear_hits += 1

    # 趋势跟随里把量能确认作为 ETF/行业篮子可迁移触发器
    if vol_5_vs_20 >= 1.15 and raw_mom_20d > 0:
        bull_hits += 1
    elif vol_5_vs_20 < 0.85 and raw_mom_20d < 0:
        bear_hits += 1

    if bull_hits >= 3 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 62 + bull_hits * 3, 60, "swing", 76,
            "Momentum stack is bullish: residual/corrected/overnight trend plus ADX/Donchian confirmation.",
            {
                "stage_num": stage,
                "rsi": rsi,
                "ma_bull_aligned": ma_bull,
                "ytd_return": ytd,
                "raw_mom_20d_pct": round(raw_mom_20d, 2),
                "corrected_mom_20d_pct": round(corrected_mom_20d, 2),
                "residual_proxy_20d_pct": round(residual_proxy_20d, 2),
                "overnight_mean_10d_pct": round(ov_mean_10d, 2),
                "intraday_mean_10d_pct": round(intra_mean_10d, 2),
                "removed_extreme_days_20d": removed_extreme_days,
                "adx14": adx["adx14"],
                "plus_di14": adx["plus_di14"],
                "minus_di14": adx["minus_di14"],
                "donchian_breakout_up": don["breakout_up"],
                "donchian_breakout_down": don["breakout_down"],
                "n_day_new_high": don["n_day_new_high"],
                "donchian_width_pct": don["width_pct"],
                "vol_5_vs_20": round(vol_5_vs_20, 3),
                "pct_from_year_high": round(pct_from_year_high, 3),
            },
        )
    if bear_hits >= 3 and bear_hits > bull_hits:
        return _mk_signal(
            "bearish", 62 + bear_hits * 3, 59, "swing", 75,
            "Momentum stack is bearish: residual/corrected/overnight trend and ADX/Donchian downside alignment.",
            {
                "stage_num": stage,
                "rsi": rsi,
                "ma_bull_aligned": ma_bull,
                "ytd_return": ytd,
                "raw_mom_20d_pct": round(raw_mom_20d, 2),
                "corrected_mom_20d_pct": round(corrected_mom_20d, 2),
                "residual_proxy_20d_pct": round(residual_proxy_20d, 2),
                "overnight_mean_10d_pct": round(ov_mean_10d, 2),
                "intraday_mean_10d_pct": round(intra_mean_10d, 2),
                "removed_extreme_days_20d": removed_extreme_days,
                "adx14": adx["adx14"],
                "plus_di14": adx["plus_di14"],
                "minus_di14": adx["minus_di14"],
                "donchian_breakout_up": don["breakout_up"],
                "donchian_breakout_down": don["breakout_down"],
                "n_day_new_high": don["n_day_new_high"],
                "donchian_width_pct": don["width_pct"],
                "vol_5_vs_20": round(vol_5_vs_20, 3),
                "pct_from_year_high": round(pct_from_year_high, 3),
            },
        )
    return _mk_signal(
        "neutral", 50, 44, "swing", 60,
        "Trend/momentum stack is mixed; ADX and Donchian have not confirmed a clean direction.",
        {
            "stage_num": stage,
            "rsi": rsi,
            "ma_bull_aligned": ma_bull,
            "ytd_return": ytd,
            "raw_mom_20d_pct": round(raw_mom_20d, 2),
            "corrected_mom_20d_pct": round(corrected_mom_20d, 2),
            "residual_proxy_20d_pct": round(residual_proxy_20d, 2),
            "overnight_mean_10d_pct": round(ov_mean_10d, 2),
            "intraday_mean_10d_pct": round(intra_mean_10d, 2),
            "removed_extreme_days_20d": removed_extreme_days,
            "adx14": adx["adx14"],
            "plus_di14": adx["plus_di14"],
            "minus_di14": adx["minus_di14"],
            "donchian_breakout_up": don["breakout_up"],
            "donchian_breakout_down": don["breakout_down"],
            "n_day_new_high": don["n_day_new_high"],
            "donchian_width_pct": don["width_pct"],
            "vol_5_vs_20": round(vol_5_vs_20, 3),
            "pct_from_year_high": round(pct_from_year_high, 3),
        },
    )


def _strategy_reversal(features: dict, regime: dict, ctx: dict) -> dict:
    rsi = _f(features.get("rsi"))
    change_pct = _f(features.get("change_pct"))
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    closes = _close_series(candles)
    r1 = _window_return(closes, 1)
    r3 = _window_return(closes, 3)
    r5 = _window_return(closes, 5)
    r20 = _window_return(closes, 20)
    boll = _bollinger_stats(closes, n=20, k=2.0)
    kdj = _kdj_stats(candles, n=9)
    cci = _cci20(candles, n=20)
    gaps = _gap_fill_stats(candles, lookback=20)
    ind_growth = _industry_growth_pct(raw, features)
    industry_20d_equiv = ind_growth * (20.0 / 252.0)
    rel_dev_20d = r20 - industry_20d_equiv

    bull_hits = 0
    bear_hits = 0
    if (r1 <= -3 and r3 <= -6) or (rsi <= 32 and r5 <= -8):
        bull_hits += 2
    if (r1 >= 3 and r3 >= 6) or (rsi >= 68 and r5 >= 8):
        bear_hits += 2
    if boll["zscore"] <= -1.4:
        bull_hits += 1
    if boll["zscore"] >= 1.4:
        bear_hits += 1
    if kdj["j"] <= 10 or kdj["k"] <= 20:
        bull_hits += 1
    if kdj["j"] >= 90 or kdj["k"] >= 80:
        bear_hits += 1
    if cci <= -110:
        bull_hits += 1
    if cci >= 110:
        bear_hits += 1
    if gaps["down_gap_count"] >= 1 and gaps["down_gap_fill_rate_pct"] >= 40:
        bull_hits += 1
    if gaps["up_gap_count"] >= 1 and gaps["up_gap_fill_rate_pct"] <= 35:
        bear_hits += 1
    if rel_dev_20d <= -6:
        bull_hits += 1
    if rel_dev_20d >= 6:
        bear_hits += 1

    evidence = {
        "rsi": rsi,
        "change_pct": change_pct,
        "ret_1d_pct": round(r1, 2),
        "ret_3d_pct": round(r3, 2),
        "ret_5d_pct": round(r5, 2),
        "ret_20d_pct": round(r20, 2),
        "boll_zscore": boll["zscore"],
        "kdj_k": kdj["k"],
        "kdj_d": kdj["d"],
        "kdj_j": kdj["j"],
        "cci20": cci,
        "up_gap_count_20d": gaps["up_gap_count"],
        "down_gap_count_20d": gaps["down_gap_count"],
        "up_gap_fill_rate_pct": gaps["up_gap_fill_rate_pct"],
        "down_gap_fill_rate_pct": gaps["down_gap_fill_rate_pct"],
        "industry_relative_deviation_20d_pct": round(rel_dev_20d, 3),
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
    }

    if bull_hits >= 3 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 58 + bull_hits * 4, 57, "swing", 72,
            "Mean-reversion setup is active (BOLL/KDJ/CCI + short-horizon oversold extension).",
            evidence,
        )
    if bear_hits >= 3 and bear_hits > bull_hits:
        return _mk_signal(
            "bearish", 58 + bear_hits * 4, 56, "swing", 71,
            "Mean-reversion pullback risk is elevated (overbought extension and weak gap-fill pattern).",
            evidence,
        )
    return _mk_signal(
        "neutral", 50, 44, "swing", 58,
        "Reversal indicators are mixed; no decisive oversold/overbought edge.",
        evidence,
    )


def _strategy_flow_turnover(features: dict, regime: dict, ctx: dict) -> dict:
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    indicators = _get_dim_data(raw, "2_kline").get("indicators") or {}
    rows = _main_flow_20d(raw)
    main_5d = _sum_main_net_yi(rows, 5) if rows else _f(features.get("main_fund_5d_net_yi"))
    main_20d = _sum_main_net_yi(rows, 20) if rows else main_5d
    proxies = _flow_indicator_proxies(rows)
    board = _board_behavior_proxies(candles, rows)
    pos_days = 0
    for r in rows[:20]:
        if isinstance(r, dict) and _f(r.get("主力净流入-净额")) > 0:
            pos_days += 1
    pos_ratio = (pos_days / min(len(rows), 20) * 100) if rows else 0.0
    turnover = _f(features.get("turnover_rate"))
    mcap = _f(features.get("market_cap_yi"))
    vol_5_vs_20 = _f(indicators.get("vol_5_vs_20"))
    flow_vs_mcap = (main_20d / mcap * 100) if mcap > 0 else 0.0
    liquidity_ok = turnover >= 1.2 or mcap >= 80
    bull_hits = 0
    bear_hits = 0
    if main_20d > 0 and pos_ratio >= 55 and liquidity_ok:
        bull_hits += 2
    if main_20d < 0 and pos_ratio <= 45 and liquidity_ok:
        bear_hits += 2
    if proxies["cmf_proxy"] >= 0.6:
        bull_hits += 1
    if proxies["cmf_proxy"] <= -0.6:
        bear_hits += 1
    if proxies["mfi_proxy"] >= 55 and proxies["mfi_proxy"] <= 85:
        bull_hits += 1
    if proxies["mfi_proxy"] <= 35:
        bear_hits += 1
    if proxies["obv_proxy"] > 0 and proxies["vpt_proxy"] > 0:
        bull_hits += 1
    if proxies["obv_proxy"] < 0 and proxies["vpt_proxy"] < 0:
        bear_hits += 1
    if proxies["flow_shock_z"] >= 0.8:
        bull_hits += 1
    if proxies["flow_shock_z"] <= -0.8:
        bear_hits += 1
    if board["fake_board_rate_pct"] >= 45:
        bear_hits += 1
    if board["fake_board_rate_pct"] <= 20 and board["strong_up_days_20d"] >= 1:
        bull_hits += 1
    if vol_5_vs_20 >= 1.1:
        bull_hits += 1
    if vol_5_vs_20 < 0.85:
        bear_hits += 1

    evidence = {
        "main_fund_5d_net_yi": round(main_5d, 2),
        "main_fund_20d_net_yi": round(main_20d, 2),
        "positive_flow_days_20d_pct": round(pos_ratio, 1),
        "flow_vs_mcap_pct": round(flow_vs_mcap, 2),
        "turnover_rate": turnover,
        "vol_5_vs_20": round(vol_5_vs_20, 3),
        "rows_used": len(rows),
        "obv_proxy": proxies["obv_proxy"],
        "vpt_proxy": proxies["vpt_proxy"],
        "mfi_proxy": proxies["mfi_proxy"],
        "cmf_proxy": proxies["cmf_proxy"],
        "flow_shock_z": proxies["flow_shock_z"],
        "flow_reversal_days": proxies["flow_reversal_days"],
        "strong_up_days_20d": board["strong_up_days_20d"],
        "fake_board_rate_pct": board["fake_board_rate_pct"],
        "flow_decay_after_spike_pct": board["flow_decay_after_spike_pct"],
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
    }

    if bull_hits >= 4 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 58 + bull_hits * 4, 57, "swing", 72,
            "Flow/turnover indicators are aligned (CMF/MFI/OBV/VPT proxies + liquidity regime).",
            evidence,
        )
    if bear_hits >= 4 and bear_hits > bull_hits:
        return _mk_signal(
            "bearish", 58 + bear_hits * 4, 56, "swing", 71,
            "Flow/turnover behavior suggests distribution and failed continuation (fake-board risk elevated).",
            evidence,
        )
    return _mk_signal(
        "neutral", 50, 44, "swing", 58,
        "Flow/turnover proxies are mixed; no clear continuation vs distribution edge.",
        evidence,
    )


def _strategy_low_vol(features: dict, regime: dict, ctx: dict) -> dict:
    vol = _f(features.get("volatility_1y"))
    dd = _f(features.get("max_drawdown_1y"))
    stage = int(features.get("stage_num") or 0)
    candles = ctx.get("candles_60d") or []
    closes = _close_series(candles)
    rets = _daily_close_returns(closes)
    atr = _atr14(candles)
    ivol = _ivol_stats(rets)
    vt = _vol_targeting_proxy(_f(ivol.get("ivol_20d_daily_pct")))
    rp = _risk_parity_proxy(vol, _f(ivol.get("ivol_20d_annual_pct")), _f(atr.get("atr14_pct")), dd)

    bull_hits = 0
    bear_hits = 0
    if vol > 0 and vol <= 32:
        bull_hits += 1
    if vol >= 58:
        bear_hits += 1
    if dd >= -30:
        bull_hits += 1
    if dd <= -45:
        bear_hits += 1
    if _f(atr.get("atr14_pct")) <= 3.8 and _f(atr.get("atr14_pct")) > 0:
        bull_hits += 1
    if _f(atr.get("atr14_pct")) >= 5.8:
        bear_hits += 1
    if _f(ivol.get("ivol_20d_daily_pct")) <= 2.0 and _f(ivol.get("ivol_20d_daily_pct")) > 0:
        bull_hits += 1
    if _f(ivol.get("ivol_20d_daily_pct")) >= 3.8:
        bear_hits += 1
    if _f(vt.get("target_leverage")) >= 1.0:
        bull_hits += 1
    if _f(vt.get("target_leverage")) <= 0.65:
        bear_hits += 1
    if _f(rp.get("risk_budget_score")) >= 55:
        bull_hits += 1
    if _f(rp.get("risk_budget_score")) <= 35:
        bear_hits += 1
    if stage in (1, 2):
        bull_hits += 1
    elif stage == 4:
        bear_hits += 1

    confidence = min(
        82.0,
        34.0
        + (12.0 if vol > 0 else 0.0)
        + (10.0 if _f(atr.get("atr14_pct")) > 0 else 0.0)
        + (10.0 if _f(ivol.get("ivol_20d_daily_pct")) > 0 else 0.0)
        + min(12.0, (bull_hits + bear_hits) * 1.8),
    )
    regime_fit = min(82.0, 56.0 + max(bull_hits, bear_hits) * 2.8)

    evidence = {
        "volatility_1y_pct": round(vol, 2),
        "max_drawdown_1y_pct": round(dd, 2),
        "stage_num": stage,
        "atr14": _f(atr.get("atr14")),
        "atr14_pct": _f(atr.get("atr14_pct")),
        "ivol_20d_daily_pct": _f(ivol.get("ivol_20d_daily_pct")),
        "ivol_60d_daily_pct": _f(ivol.get("ivol_60d_daily_pct")),
        "ivol_20d_annual_pct": _f(ivol.get("ivol_20d_annual_pct")),
        "target_annual_vol_pct": _f(vt.get("target_annual_vol_pct")),
        "target_leverage": _f(vt.get("target_leverage")),
        "risk_parity_weight_proxy_pct": _f(rp.get("risk_parity_weight_proxy_pct")),
        "risk_budget_score": _f(rp.get("risk_budget_score")),
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
    }

    if bull_hits >= 4 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 58 + min(20, bull_hits * 4), confidence, "position", regime_fit + 4,
            "Risk metrics are constructive (ATR/IVOL controlled, vol-target leverage >= 1, risk budget supportive).",
            evidence,
        )
    if bear_hits >= 4 and bear_hits > bull_hits:
        return _mk_signal(
            "bearish", 58 + min(20, bear_hits * 4), confidence, "position", regime_fit,
            "Risk profile is stressed (elevated ATR/IVOL, weak vol-target leverage, constrained risk budget).",
            evidence,
        )
    return _mk_signal(
        "neutral", 52, confidence - 8.0, "position", regime_fit - 4.0,
        "Risk factors are mixed; no clear low-vol edge or explicit risk-off warning.",
        evidence,
    )


def _strategy_limit_up(features: dict, regime: dict, ctx: dict) -> dict:
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    market = str(features.get("market") or "A")
    lu = _limit_up_stats(candles, market)
    patt = _limit_pattern_stats(candles, market)
    sector_heat = _sector_lhb_theme_diffusion(raw)
    lhb_count = _f(features.get("lhb_30d_count"))
    youzi_count = _f(features.get("matched_youzi_count"))
    flow_5d = _f(features.get("main_fund_5d_net_yi"))
    change_pct = _f(features.get("change_pct"))
    sentiment_heat = _f(features.get("sentiment_heat"))

    bull_hits = 0
    bear_hits = 0
    if patt["first_board_hits"] >= 1:
        bull_hits += 1
    if patt["second_board_hits"] >= 1:
        bull_hits += 1
    if patt["one_word_board_hits"] >= 1:
        bull_hits += 1
    if lu["broken_rate"] <= 35 and patt["first_break_after_streak_hits"] == 0:
        bull_hits += 1
    if (lhb_count >= 1 or youzi_count >= 1) and flow_5d >= 0:
        bull_hits += 1
    if sector_heat["theme_diffusion_score"] >= 40 and sector_heat["theme_concentration_pct"] <= 58:
        bull_hits += 1

    if lu["broken_rate"] >= 50:
        bear_hits += 1
    if patt["first_break_after_streak_hits"] >= 1:
        bear_hits += 1
    if patt["first_yin_break_hits"] >= 1:
        bear_hits += 1
    if patt["height_cycle_label"] in ("high_cycle", "blowoff_and_break") and sentiment_heat >= 75:
        bear_hits += 1
    if patt["t_board_hits"] >= 1 and flow_5d < 0:
        bear_hits += 1
    if sector_heat["theme_diffusion_score"] >= 70 and sector_heat["theme_concentration_pct"] >= 55:
        bear_hits += 1

    evidence = {
        "change_pct": change_pct,
        "lhb_30d_count": lhb_count,
        "matched_youzi_count": youzi_count,
        "main_fund_5d_net_yi": round(flow_5d, 2),
        "sentiment_heat": sentiment_heat,
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
        **lu,
        **patt,
        **sector_heat,
    }

    confidence = min(
        82.0,
        32.0
        + (12.0 if lu["limit_hits"] > 0 else 0.0)
        + min(16.0, (bull_hits + bear_hits) * 2.0)
        + (8.0 if sector_heat["sector_lhb_sample_n"] > 0 else 0.0),
    )
    regime_fit = min(84.0, 56.0 + max(bull_hits, bear_hits) * 2.8)

    if bull_hits >= 4 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 60 + min(22, bull_hits * 4), confidence, "intraday", regime_fit + 4,
            "Board ecology is constructive (first/second-board continuity with controllable break risk and theme diffusion).",
            evidence,
        )
    if bear_hits >= 4 and bear_hits >= bull_hits:
        return _mk_signal(
            "bearish", 60 + min(22, bear_hits * 4), confidence, "intraday", regime_fit,
            "Board cycle shows late-stage stress (break-board/first-yin risk and crowded high-cycle behavior).",
            evidence,
        )
    return _mk_signal(
        "neutral", 52, confidence - 7.0, "intraday", regime_fit - 5.0,
        "Board-ecology signals are mixed; continuity and break risk are both present.",
        evidence,
    )


def _strategy_rotation(features: dict, regime: dict, ctx: dict) -> dict:
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    ind_growth = _industry_growth_pct(raw, features)
    lifecycle = str(_get_dim_data(raw, "7_industry").get("lifecycle") or "")
    main_5d = _sum_main_net_yi(_main_flow_20d(raw), 5)
    ytd = _f(features.get("ytd_return"))
    style = _style_rotation_proxies(features, raw, candles)
    theme = _sector_lhb_theme_diffusion(raw)
    sentiment = _f(features.get("sentiment_heat"))

    bull_hits = 0
    bear_hits = 0
    if style["style_bias"] == "growth_tech" and ind_growth >= 8 and main_5d > 0:
        bull_hits += 2
    if style["style_bias"] == "value_dividend" and main_5d >= -2 and ytd > -10:
        bull_hits += 2
    if style["size_style"] == "small_cap" and style["mom_20d_pct"] >= 8 and theme["theme_diffusion_score"] >= 45:
        bull_hits += 1
    if style["size_style"] == "large_cap" and style["value_dividend_style_score"] >= 3 and sentiment <= 75:
        bull_hits += 1
    if lifecycle and "成" in lifecycle:
        bull_hits += 1

    if ind_growth <= 0 and main_5d < 0:
        bear_hits += 2
    if style["style_bias"] == "growth_tech" and style["mom_20d_pct"] <= -5:
        bear_hits += 1
    if style["size_style"] == "small_cap" and sentiment >= 80 and theme["theme_concentration_pct"] >= 55:
        bear_hits += 1
    if lifecycle and ("衰退" in lifecycle or "下行" in lifecycle):
        bear_hits += 1
    if theme["theme_diffusion_score"] <= 25 and style["growth_style_score"] <= 2 and style["value_dividend_style_score"] <= 2:
        bear_hits += 1

    evidence = {
        "industry_growth_pct": ind_growth,
        "industry_lifecycle": lifecycle,
        "main_fund_5d_net_yi": round(main_5d, 2),
        "ytd_return": ytd,
        "sentiment_heat": round(sentiment, 1),
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
        **style,
        **theme,
    }

    confidence = min(
        82.0,
        34.0
        + min(14.0, (bull_hits + bear_hits) * 2.0)
        + (10.0 if theme["sector_lhb_sample_n"] > 0 else 0.0)
        + (8.0 if abs(ind_growth) > 0.1 else 0.0),
    )
    regime_fit = min(83.0, 56.0 + max(bull_hits, bear_hits) * 3.0)

    if bull_hits >= 4 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 58 + min(22, bull_hits * 4), confidence, "swing", regime_fit + 4,
            "Style rotation is supportive: size/style bias and theme diffusion align with positive flow backdrop.",
            evidence,
        )
    if bear_hits >= 4 and bear_hits >= bull_hits:
        return _mk_signal(
            "bearish", 58 + min(22, bear_hits * 4), confidence, "swing", regime_fit,
            "Style/theme rotation is fragile: growth/value legs lose support and diffusion narrows.",
            evidence,
        )
    return _mk_signal(
        "neutral", 52, confidence - 7.0, "swing", regime_fit - 5.0,
        "Style and theme rotation signals are mixed; wait for clearer cross-style confirmation.",
        evidence,
    )


def _strategy_value_repair(features: dict, regime: dict, ctx: dict) -> dict:
    pe = _f(features.get("pe"))
    pb = _f(features.get("pb"))
    pe_q_5y = _f(features.get("pe_quantile_5y"))
    pe_vs_ind = _f(features.get("pe_vs_industry"))
    safety_margin = _f(features.get("safety_margin"))
    dividend_yield = _f(features.get("dividend_yield"))
    earnings_yield = _f(features.get("earnings_yield"))
    cf_to_price = _f(features.get("cf_to_price"))
    fcf_to_price = _f(features.get("fcf_to_price"))
    ev_ebitda = _f(features.get("ev_ebitda"))
    is_state_owned = bool(features.get("is_state_owned"))
    is_below_book = bool(features.get("is_below_book"))

    roe = _f(features.get("roe_latest"))
    net_margin = _f(features.get("net_margin"))
    debt_ratio = _f(features.get("debt_ratio"))
    rev_growth = _f(features.get("revenue_growth_latest"))
    np_growth = _f(features.get("net_profit_growth_latest"))
    fcf_margin = _f(features.get("fcf_margin"))

    valuation_hits = 0
    if pe > 0 and pe <= 12:
        valuation_hits += 1
    if pb > 0 and pb <= 1.8:
        valuation_hits += 1
    if 0 < pe_q_5y <= 30:
        valuation_hits += 1
    if pe_vs_ind <= -20:
        valuation_hits += 1
    if safety_margin >= 20:
        valuation_hits += 1
    if dividend_yield >= 2.5:
        valuation_hits += 1
    if earnings_yield >= 8:
        valuation_hits += 1
    if cf_to_price >= 5:
        valuation_hits += 1
    if fcf_to_price >= 3:
        valuation_hits += 1
    if 0 < ev_ebitda <= 9:
        valuation_hits += 1
    if is_state_owned and (is_below_book or pb <= 1.15):
        valuation_hits += 1

    overvalued_hits = 0
    if pe >= 45:
        overvalued_hits += 1
    if pb >= 6:
        overvalued_hits += 1
    if pe_q_5y >= 80:
        overvalued_hits += 1
    if pe_vs_ind >= 35:
        overvalued_hits += 1
    if safety_margin <= -25 and safety_margin > -95:
        overvalued_hits += 1
    if pe > 0 and earnings_yield <= 2.5:
        overvalued_hits += 1
    if cf_to_price > 0 and cf_to_price <= 1.0:
        overvalued_hits += 1
    if fcf_to_price > 0 and fcf_to_price <= 0.8:
        overvalued_hits += 1
    if ev_ebitda >= 20:
        overvalued_hits += 1

    quality_support = 0
    if roe >= 10:
        quality_support += 1
    if net_margin >= 8:
        quality_support += 1
    if 0 < debt_ratio <= 70:
        quality_support += 1
    if rev_growth > 0 or np_growth > 0:
        quality_support += 1
    if fcf_margin > 3:
        quality_support += 1
    if cf_to_price >= 4:
        quality_support += 1
    if fcf_to_price >= 2:
        quality_support += 1

    trap_hits = 0
    if pe <= 0 and pb >= 3:
        trap_hits += 2
    if debt_ratio >= 75:
        trap_hits += 1
    if roe < 5 and abs(roe) > 0.1:
        trap_hits += 1
    if net_margin < 3 and abs(net_margin) > 0.1:
        trap_hits += 1
    if (rev_growth < 0 and np_growth <= 0) and (abs(rev_growth) > 0.1 or abs(np_growth) > 0.1):
        trap_hits += 1
    if fcf_margin < -2:
        trap_hits += 1
    if cf_to_price < 0:
        trap_hits += 1
    if fcf_to_price < 0:
        trap_hits += 1
    if ev_ebitda > 0 and ev_ebitda >= 18 and quality_support <= 1:
        trap_hits += 1

    valuation_known = any(
        [
            pe > 0,
            pb > 0,
            pe_q_5y not in (0, 50),
            abs(pe_vs_ind) > 0.1,
            safety_margin > -95,
            dividend_yield > 0,
            abs(earnings_yield) > 0.1,
            abs(cf_to_price) > 0.1,
            abs(fcf_to_price) > 0.1,
            ev_ebitda > 0,
        ]
    )
    quality_known = any(
        [
            abs(roe) > 0.1,
            abs(net_margin) > 0.1,
            abs(debt_ratio) > 0.1,
            abs(rev_growth) > 0.1,
            abs(np_growth) > 0.1,
            abs(fcf_margin) > 0.1,
            abs(cf_to_price) > 0.1,
            abs(fcf_to_price) > 0.1,
        ]
    )

    confidence = 35.0 + (18.0 if valuation_known else 0.0) + (12.0 if quality_known else 0.0)
    confidence = min(78.0, confidence)
    regime_fit = 58.0 + min(12.0, valuation_hits * 2.5)

    evidence = {
        "pe": round(pe, 2),
        "pb": round(pb, 2),
        "pe_quantile_5y": round(pe_q_5y, 1),
        "pe_vs_industry_pct": round(pe_vs_ind, 2),
        "safety_margin_pct": round(safety_margin, 2),
        "dividend_yield_pct": round(dividend_yield, 2),
        "earnings_yield_pct": round(earnings_yield, 3),
        "cf_to_price_pct": round(cf_to_price, 3),
        "fcf_to_price_pct": round(fcf_to_price, 3),
        "ev_ebitda": round(ev_ebitda, 3),
        "is_state_owned": is_state_owned,
        "is_below_book": is_below_book,
        "roe_latest_pct": round(roe, 2),
        "net_margin_pct": round(net_margin, 2),
        "debt_ratio_pct": round(debt_ratio, 2),
        "revenue_growth_latest_pct": round(rev_growth, 2),
        "net_profit_growth_latest_pct": round(np_growth, 2),
        "fcf_margin_pct": round(fcf_margin, 2),
        "valuation_hits": valuation_hits,
        "overvalued_hits": overvalued_hits,
        "quality_support_hits": quality_support,
        "value_trap_hits": trap_hits,
        "valuation_known": valuation_known,
        "quality_known": quality_known,
    }

    if not valuation_known and not quality_known:
        return _mk_signal(
            "neutral", 48, 28, "position", 52,
            "Valuation-repair inputs are too sparse; keep neutral until data is richer.",
            evidence,
        )

    if trap_hits >= 2 and quality_support == 0:
        return _mk_signal(
            "bearish", 62 + min(14, trap_hits * 4), confidence, "position", regime_fit,
            "Cheap-looking multiples are likely a value trap due to weak quality/risk metrics.",
            evidence,
        )

    if valuation_hits >= 2 and trap_hits == 0 and (quality_support >= 1 or not quality_known):
        return _mk_signal(
            "bullish", 58 + min(18, valuation_hits * 4 + quality_support * 2), confidence, "position", regime_fit + 5,
            "Valuation is compressed while fundamentals are stable enough for potential value-repair.",
            evidence,
        )

    if overvalued_hits >= 2 and (quality_support == 0 or trap_hits >= 1):
        return _mk_signal(
            "bearish", 58 + min(18, overvalued_hits * 4 + trap_hits * 3), confidence, "position", regime_fit,
            "Valuation is stretched without sufficient quality support, limiting repair upside.",
            evidence,
        )

    return _mk_signal(
        "neutral", 52, confidence - 4, "position", regime_fit - 2,
        "Valuation and quality signals are mixed; no clean value-repair setup yet.",
        evidence,
    )


def _strategy_quality_improvement(features: dict, regime: dict, ctx: dict) -> dict:
    roe = _f(features.get("roe_latest"))
    roe_5y_avg = _f(features.get("roe_5y_avg"))
    roe_trend_up = bool(features.get("roe_trend_up"))
    roic = _f(features.get("roic"))
    fcf_margin = _f(features.get("fcf_margin"))
    net_margin = _f(features.get("net_margin"))
    debt_ratio = _f(features.get("debt_ratio"))
    current_ratio = _f(features.get("current_ratio"))
    receivable_turnover = _f(features.get("receivable_turnover"))
    inventory_turnover = _f(features.get("inventory_turnover"))
    asset_turnover = _f(features.get("asset_turnover"))
    asset_growth = _f(features.get("asset_growth"))
    gross_profitability = _f(features.get("gross_profitability"))
    rev_growth = _f(features.get("revenue_growth_latest"))
    np_growth = _f(features.get("net_profit_growth_latest"))
    accrual_proxy = np_growth - rev_growth

    improve_hits = 0
    if roe >= 10:
        improve_hits += 1
    if (roe - roe_5y_avg) >= 1.5 or roe_trend_up:
        improve_hits += 1
    if roic >= 8:
        improve_hits += 1
    if fcf_margin >= 3:
        improve_hits += 1
    if net_margin >= 8:
        improve_hits += 1
    if debt_ratio > 0 and debt_ratio <= 65:
        improve_hits += 1
    if current_ratio >= 1.2:
        improve_hits += 1
    if np_growth > 0 and (rev_growth > 0 or np_growth >= rev_growth - 5):
        improve_hits += 1
    if receivable_turnover >= 3.5:
        improve_hits += 1
    if inventory_turnover >= 3.0:
        improve_hits += 1
    if asset_turnover >= 0.4:
        improve_hits += 1
    if 0 < asset_growth <= 25:
        improve_hits += 1
    if gross_profitability >= 0.22:
        improve_hits += 1

    degrade_hits = 0
    if roe < 5 and abs(roe) > 0.1:
        degrade_hits += 1
    if roic < 5 and abs(roic) > 0.1:
        degrade_hits += 1
    if fcf_margin < -2:
        degrade_hits += 1
    if net_margin < 3 and abs(net_margin) > 0.1:
        degrade_hits += 1
    if debt_ratio >= 75:
        degrade_hits += 1
    if 0 < current_ratio < 1:
        degrade_hits += 1
    if rev_growth <= 0 and np_growth < 0:
        degrade_hits += 1
    if np_growth > 0 and rev_growth < 0:
        degrade_hits += 1
    if 0 < receivable_turnover < 2:
        degrade_hits += 1
    if 0 < inventory_turnover < 1.5:
        degrade_hits += 1
    if 0 < asset_turnover < 0.2:
        degrade_hits += 1
    if asset_growth >= 35 or asset_growth <= -20:
        degrade_hits += 1
    if 0 < gross_profitability < 0.1:
        degrade_hits += 1

    # Proxy for earnings-quality deterioration: profit growth far above revenue growth.
    accrual_risk = accrual_proxy > 40 and np_growth > 0
    if accrual_risk:
        degrade_hits += 1
    expansion_quality_mismatch = (
        asset_growth >= 25
        and (receivable_turnover <= 2.2 or inventory_turnover <= 1.8)
        and (receivable_turnover > 0 or inventory_turnover > 0)
    )
    if expansion_quality_mismatch:
        degrade_hits += 1

    quality_known = any(
        [
            abs(roe) > 0.1,
            abs(roe_5y_avg) > 0.1,
            abs(roic) > 0.1,
            abs(fcf_margin) > 0.1,
            abs(net_margin) > 0.1,
            abs(debt_ratio) > 0.1,
            abs(current_ratio) > 0.1,
            abs(rev_growth) > 0.1,
            abs(np_growth) > 0.1,
            abs(receivable_turnover) > 0.1,
            abs(inventory_turnover) > 0.1,
            abs(asset_turnover) > 0.1,
            abs(asset_growth) > 0.1,
            abs(gross_profitability) > 0.01,
        ]
    )

    confidence = 34.0 + (25.0 if quality_known else 0.0) + min(15.0, (improve_hits + degrade_hits) * 1.5)
    confidence = min(82.0, confidence)
    regime_fit = 55.0 + min(16.0, improve_hits * 2.2)

    evidence = {
        "roe_latest_pct": round(roe, 2),
        "roe_5y_avg_pct": round(roe_5y_avg, 2),
        "roe_trend_up": roe_trend_up,
        "roic_pct": round(roic, 2),
        "fcf_margin_pct": round(fcf_margin, 2),
        "net_margin_pct": round(net_margin, 2),
        "debt_ratio_pct": round(debt_ratio, 2),
        "current_ratio": round(current_ratio, 2),
        "receivable_turnover": round(receivable_turnover, 3),
        "inventory_turnover": round(inventory_turnover, 3),
        "asset_turnover": round(asset_turnover, 3),
        "asset_growth_pct": round(asset_growth, 3),
        "gross_profitability": round(gross_profitability, 3),
        "revenue_growth_latest_pct": round(rev_growth, 2),
        "net_profit_growth_latest_pct": round(np_growth, 2),
        "accrual_proxy_pct": round(accrual_proxy, 2),
        "accrual_risk": accrual_risk,
        "expansion_quality_mismatch": expansion_quality_mismatch,
        "improve_hits": improve_hits,
        "degrade_hits": degrade_hits,
        "quality_known": quality_known,
    }

    if not quality_known:
        return _mk_signal(
            "neutral", 48, 30, "position", 50,
            "Quality-improvement inputs are sparse; keep neutral until richer financial data is available.",
            evidence,
        )

    if improve_hits >= 3 and degrade_hits == 0:
        return _mk_signal(
            "bullish", 58 + min(20, improve_hits * 4), confidence, "position", regime_fit + 7,
            "Profitability and cash-flow quality are improving with controlled balance-sheet risk.",
            evidence,
        )

    if degrade_hits >= 3 and improve_hits <= 1:
        return _mk_signal(
            "bearish", 58 + min(20, degrade_hits * 4), confidence, "position", regime_fit,
            "Core quality metrics are deteriorating, raising downside and value-trap risk.",
            evidence,
        )

    if degrade_hits >= 2 and accrual_risk:
        return _mk_signal(
            "bearish", 62 + min(14, degrade_hits * 3), confidence, "position", regime_fit - 2,
            "Earnings quality looks fragile (profit/revenue mismatch plus weak supporting metrics).",
            evidence,
        )

    return _mk_signal(
        "neutral", 52, confidence - 5, "position", regime_fit - 3,
        "Quality factors are mixed; no clear improvement or deterioration trend.",
        evidence,
    )


def _strategy_calendar_seasonality(features: dict, regime: dict, ctx: dict) -> dict:
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    kline_data = _get_dim_data(raw, "2_kline")
    seasonality = kline_data.get("seasonality_1y") or {}
    now = datetime.now()
    wd_keys = ["mon", "tue", "wed", "thu", "fri"]
    today_wd = min(4, now.weekday())
    today_key = wd_keys[today_wd]
    long_sample = int(_f(seasonality.get("sample_days")))

    if long_sample >= 80 and isinstance(seasonality.get("weekday_mean_return_pct"), dict):
        wd_mean_map = seasonality.get("weekday_mean_return_pct") or {}
        wd_win_map = seasonality.get("weekday_win_rate_pct") or {}
        today_mean = _f(wd_mean_map.get(today_key))
        today_win_rate = _f(wd_win_map.get(today_key), default=50.0)
        thursday_mean = _f(seasonality.get("thursday_mean_return_pct"))
        thursday_win = _f(seasonality.get("thursday_win_rate_pct"), default=50.0)
        month_start_mean = _f(seasonality.get("month_start_mean_return_pct"))
        month_end_mean = _f(seasonality.get("month_end_mean_return_pct"))
        month_start_win = _f(seasonality.get("month_start_win_rate_pct"), default=50.0)
        month_end_win = _f(seasonality.get("month_end_win_rate_pct"), default=50.0)
        spring_mean = _f(seasonality.get("spring_festival_window_mean_return_pct"))
        spring_pre = _f(seasonality.get("spring_festival_pre_window_mean_return_pct"))
        spring_post = _f(seasonality.get("spring_festival_post_window_mean_return_pct"))
        spring_samples = int(_f(seasonality.get("spring_festival_samples")))

        # 月初/月末长期统计驱动
        month_position_score = 0.0
        if now.day <= 5:
            month_position_score = month_start_mean * 5.5 + (month_start_win - 50.0) * 0.25
        elif now.day >= 25:
            month_position_score = month_end_mean * 5.5 + (month_end_win - 50.0) * 0.25

        # 周四效应（A 股历史里常见“周四偏弱/择时”）
        thursday_bias_score = 0.0
        if today_wd == 3:
            thursday_bias_score = thursday_mean * 8.0 + (thursday_win - 50.0) * 0.35

        # 农历/春节效应：以春节窗口近似农历季节性。
        spring_live_bias = 0.0
        spring_delta = _spring_festival_delta_days(now)
        if spring_delta is not None:
            if -7 <= spring_delta <= -1:
                spring_live_bias = spring_pre * 4.5
            elif 1 <= spring_delta <= 10:
                spring_live_bias = spring_post * 4.5
            elif -7 <= spring_delta <= 10:
                spring_live_bias = spring_mean * 3.5

        seasonality_score = (
            today_mean * 6.0
            + (today_win_rate - 50.0) * 0.6
            + month_position_score
            + thursday_bias_score
            + spring_live_bias
        )
        confidence = min(
            84.0,
            40.0 + min(38.0, long_sample / 6.0) + (8.0 if spring_samples >= 10 else 0.0),
        )
        regime_fit = min(85.0, 56.0 + min(abs(seasonality_score), 20.0) * 1.25)
        evidence = {
            "calendar_mode": "long_stats",
            "seasonality_sample_days": long_sample,
            "today_weekday": today_wd,
            "weekday_mean_return_pct": round(today_mean, 4),
            "weekday_win_rate_pct": round(today_win_rate, 2),
            "thursday_mean_return_pct": round(thursday_mean, 4),
            "thursday_win_rate_pct": round(thursday_win, 2),
            "month_start_mean_return_pct": round(month_start_mean, 4),
            "month_end_mean_return_pct": round(month_end_mean, 4),
            "month_start_win_rate_pct": round(month_start_win, 2),
            "month_end_win_rate_pct": round(month_end_win, 2),
            "spring_festival_window_mean_return_pct": round(spring_mean, 4),
            "spring_festival_pre_window_mean_return_pct": round(spring_pre, 4),
            "spring_festival_post_window_mean_return_pct": round(spring_post, 4),
            "spring_festival_samples": spring_samples,
            "spring_festival_delta_days": spring_delta if spring_delta is not None else "n/a",
            "month_position_score": round(month_position_score, 3),
            "thursday_bias_score": round(thursday_bias_score, 3),
            "spring_live_bias": round(spring_live_bias, 3),
            "seasonality_score": round(seasonality_score, 3),
        }
        if seasonality_score >= 12:
            return _mk_signal(
                "bullish", min(78.0, 56.0 + seasonality_score * 0.75), confidence, "swing", regime_fit,
                "Long-window calendar effects are supportive (weekday/month-position/lunar-holiday signals align).",
                evidence,
            )
        if seasonality_score <= -12:
            return _mk_signal(
                "bearish", min(78.0, 56.0 + abs(seasonality_score) * 0.75), confidence, "swing", regime_fit,
                "Long-window calendar effects are adverse (weekday/month-position/lunar-holiday signals align to risk-off).",
                evidence,
            )
        return _mk_signal(
            "neutral", 52, confidence - 7.0, "swing", regime_fit - 4.0,
            "Long-window calendar factors are mixed; no clear seasonal edge right now.",
            evidence,
        )

    # Fallback for legacy cache (no seasonality_1y): keep prior 60d heuristic path.
    if len(candles) < 20:
        return _mk_signal(
            "neutral", 48, 30, "swing", 50,
            "Not enough bars to estimate calendar/seasonality effects.",
            {"candles_used": len(candles), "calendar_mode": "fallback_short"},
        )
    weekday_returns: dict[int, list[float]] = {i: [] for i in range(5)}
    weekday_wins: dict[int, int] = {i: 0 for i in range(5)}
    for i in range(1, len(candles)):
        prev = candles[i - 1]
        cur = candles[i]
        dt = _candle_date(cur)
        if dt is None:
            continue
        wd = dt.weekday()
        if wd > 4:
            continue
        prev_close = _f(prev.get("close"))
        cur_close = _f(cur.get("close"))
        if prev_close <= 0 or cur_close <= 0:
            continue
        r = (cur_close / prev_close - 1) * 100
        weekday_returns[wd].append(r)
        if r > 0:
            weekday_wins[wd] += 1
    today_vals = weekday_returns.get(today_wd) or []
    today_mean = mean(today_vals) if today_vals else 0.0
    today_win_rate = weekday_wins.get(today_wd, 0) / len(today_vals) * 100 if today_vals else 50.0
    tom_bias = 1.0 if now.day <= 5 else (-0.6 if now.day >= 25 else 0.0)
    qend_bias = -0.8 if (now.month in (3, 6, 9, 12) and now.day >= 20) else 0.0
    seasonality_score = today_mean * 6.0 + (today_win_rate - 50.0) * 0.8 + tom_bias * 10.0 + qend_bias * 8.0
    confidence = min(72.0, 30.0 + len(today_vals) * 3.5)
    regime_fit = min(78.0, 52.0 + min(abs(seasonality_score), 20.0) * 1.1)
    evidence = {
        "calendar_mode": "fallback_short",
        "today_weekday": today_wd,
        "weekday_sample_n": len(today_vals),
        "weekday_mean_return_pct": round(today_mean, 3),
        "weekday_win_rate_pct": round(today_win_rate, 1),
        "turn_of_month_bias": tom_bias,
        "quarter_end_bias": qend_bias,
        "seasonality_score": round(seasonality_score, 2),
    }
    if seasonality_score >= 12:
        return _mk_signal(
            "bullish", min(74.0, 55.0 + seasonality_score * 0.7), confidence, "swing", regime_fit,
            "Short-window calendar effects are supportive.",
            evidence,
        )
    if seasonality_score <= -12:
        return _mk_signal(
            "bearish", min(74.0, 55.0 + abs(seasonality_score) * 0.7), confidence, "swing", regime_fit,
            "Short-window calendar effects indicate elevated pullback risk.",
            evidence,
        )
    return _mk_signal(
        "neutral", 52, confidence - 6.0, "swing", regime_fit - 4.0,
        "Calendar effects are mixed under fallback mode.",
        evidence,
    )


def _strategy_intraday_timing(features: dict, regime: dict, ctx: dict) -> dict:
    candles = ctx.get("candles_60d") or []
    overnight, intraday = _overnight_intraday(candles)
    ov_3 = mean(overnight[-3:]) if len(overnight) >= 3 else 0.0
    intra_3 = mean(intraday[-3:]) if len(intraday) >= 3 else 0.0
    micro_ok = bool(features.get("intraday_minute_available"))
    open_auction_ret = _f(features.get("open_auction_ret_pct"))
    open_15m_ret = _f(features.get("open_15m_ret_pct"))
    tail_30m_ret = _f(features.get("tail_30m_ret_pct"))
    open_auction_vol_ratio = _f(features.get("open_auction_volume_ratio"))
    close_auction_vol_ratio = _f(features.get("close_auction_volume_ratio"))
    close_auction_jump = _f(features.get("close_auction_jump_pct"))
    intraday_amp = _f(features.get("intraday_amplitude_pct"))

    turnover = _f(features.get("turnover_rate"))
    flow_5d = _f(features.get("main_fund_5d_net_yi"))
    sentiment = _f(features.get("sentiment_heat"))
    lhb_count = _f(features.get("lhb_30d_count"))
    youzi_count = _f(features.get("matched_youzi_count"))
    chg_1d = _f(features.get("change_pct"))

    momentum_follow = ov_3 > 0 and intra_3 >= 0
    gap_fade = ov_3 > 0 and intra_3 < 0
    panic_reversal = ov_3 < 0 and intra_3 > 0 and flow_5d > 0

    support_hits = 0
    if turnover >= 2:
        support_hits += 1
    if flow_5d > 0:
        support_hits += 1
    if lhb_count >= 1 or youzi_count >= 1:
        support_hits += 1
    if sentiment >= 45:
        support_hits += 1
    if micro_ok and open_15m_ret > 0 and tail_30m_ret >= 0:
        support_hits += 1
    if micro_ok and close_auction_vol_ratio >= 1.1 and close_auction_jump >= -0.2:
        support_hits += 1

    risk_hits = 0
    if sentiment >= 80:
        risk_hits += 1
    if turnover >= 10:
        risk_hits += 1
    if flow_5d < 0:
        risk_hits += 1
    if chg_1d >= 7 and gap_fade:
        risk_hits += 1
    if micro_ok and open_auction_ret > 0 and open_15m_ret < -0.8:
        risk_hits += 1
    if micro_ok and tail_30m_ret < -0.6 and close_auction_vol_ratio >= 1.2:
        risk_hits += 1
    if micro_ok and intraday_amp >= 6.5:
        risk_hits += 1

    evidence = {
        "overnight_mean_3d_pct": round(ov_3, 3),
        "intraday_mean_3d_pct": round(intra_3, 3),
        "intraday_minute_available": micro_ok,
        "open_auction_ret_pct": round(open_auction_ret, 3),
        "open_15m_ret_pct": round(open_15m_ret, 3),
        "tail_30m_ret_pct": round(tail_30m_ret, 3),
        "open_auction_volume_ratio": round(open_auction_vol_ratio, 3),
        "close_auction_volume_ratio": round(close_auction_vol_ratio, 3),
        "close_auction_jump_pct": round(close_auction_jump, 3),
        "intraday_amplitude_pct": round(intraday_amp, 3),
        "change_1d_pct": round(chg_1d, 2),
        "turnover_rate_pct": round(turnover, 2),
        "main_fund_5d_net_yi": round(flow_5d, 2),
        "sentiment_heat": round(sentiment, 1),
        "lhb_30d_count": int(lhb_count),
        "matched_youzi_count": int(youzi_count),
        "support_hits": support_hits,
        "risk_hits": risk_hits,
    }

    confidence = min(
        82.0,
        35.0 + support_hits * 7.0 + min(len(overnight), 10) * 1.2 + (8.0 if micro_ok else 0.0)
    )
    regime_fit = min(82.0, 54.0 + support_hits * 4.0)

    if (momentum_follow and support_hits >= 2 and risk_hits <= 1) or (panic_reversal and support_hits >= 2):
        return _mk_signal(
            "bullish", 58 + min(18, support_hits * 4), confidence, "intraday", regime_fit + 5,
            "Overnight/intraday microstructure is supportive with adequate liquidity/flow backing.",
            evidence,
        )

    if gap_fade and (risk_hits >= 2 or support_hits <= 1):
        return _mk_signal(
            "bearish", 60 + min(16, risk_hits * 4), confidence, "intraday", regime_fit,
            "Gap-fade and crowding risk suggest weak intraday follow-through quality.",
            evidence,
        )

    if risk_hits >= 3:
        return _mk_signal(
            "bearish", 64 + min(12, risk_hits * 3), confidence, "intraday", regime_fit - 2,
            "Microstructure risk is elevated (crowding/turnover/flow mismatch).",
            evidence,
        )

    return _mk_signal(
        "neutral", 52, confidence - 8.0, "intraday", regime_fit - 4.0,
        "Intraday microstructure is mixed; no clean timing edge.",
        evidence,
    )


def _strategy_pair_relative_strength(features: dict, regime: dict, ctx: dict) -> dict:
    peers_count = int(_f(features.get("peers_count")))
    vs_peer_avg_pe = _f(features.get("vs_peer_avg_pe"))
    pe_vs_ind = _f(features.get("pe_vs_industry"))
    ytd = _f(features.get("ytd_return"))
    flow_5d = _f(features.get("main_fund_5d_net_yi"))
    ind_growth = _f(features.get("industry_growth_pct"))
    stage = int(features.get("stage_num") or 0)

    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    closes = _close_series(candles)
    mom20 = _window_return(closes, 20)
    ind_proxy_20d = ind_growth * (20.0 / 252.0)
    rel_mom_vs_ind = mom20 - ind_proxy_20d

    style_rotation = _style_rotation_proxies(features, raw, candles)
    size_style = str(style_rotation.get("size_style") or "mid_cap")
    style_bias = str(style_rotation.get("style_bias") or "balanced")
    etf_pair = _etf_pair_hint(
        size_style=size_style,
        style_bias=style_bias,
        industry=str(features.get("industry") or ""),
        mom20=mom20,
        ytd=ytd,
    )
    beta_proxy = _beta_neutral_proxy(
        candles=candles,
        features=features,
        size_style=size_style,
        style_bias=style_bias,
    )

    bull_hits = 0
    bear_hits = 0

    # 1) 行业内配对：估值偏离 + 行业相对动量 + 资金
    if peers_count > 1 and vs_peer_avg_pe <= -8:
        bull_hits += 1
    if peers_count > 1 and vs_peer_avg_pe >= 12:
        bear_hits += 1

    if rel_mom_vs_ind >= 2.5:
        bull_hits += 1
    elif rel_mom_vs_ind <= -2.5:
        bear_hits += 1

    if ytd > 0:
        bull_hits += 1
    elif ytd < 0:
        bear_hits += 1

    if flow_5d >= 0.5:
        bull_hits += 1
    elif flow_5d <= -0.5:
        bear_hits += 1

    if ind_growth > 5:
        bull_hits += 1
    elif ind_growth < 0:
        bear_hits += 1

    # 2) ETF 配对：风格腿 + 宽基/主题对冲腿
    etf_pair_bias = _f(etf_pair.get("etf_pair_bias_score"))
    if etf_pair_bias >= 2.5:
        bull_hits += 1
    elif etf_pair_bias <= -2.5:
        bear_hits += 1

    # 3) beta-neutral 代理：高 beta 时要求更强 alpha 才能给多头结论
    beta_vol = _f(beta_proxy.get("beta_vol_proxy"))
    beta_risk = str(beta_proxy.get("beta_risk_bucket") or "balanced_beta")
    if beta_risk == "high_beta":
        bear_hits += 1
    elif beta_risk == "balanced_beta":
        bull_hits += 1

    if stage in (1, 2):
        bull_hits += 1
    elif stage == 4:
        bear_hits += 1

    if pe_vs_ind <= -20:
        bull_hits += 1
    elif pe_vs_ind >= 30:
        bear_hits += 1

    valuation_edge = -vs_peer_avg_pe
    flow_edge = flow_5d * 3.0
    stage_edge = 2.0 if stage in (1, 2) else (-2.0 if stage == 4 else 0.0)
    alpha_score = (
        rel_mom_vs_ind * 1.2
        + valuation_edge * 0.42
        + flow_edge
        + stage_edge
        + etf_pair_bias * 0.9
    )

    proxy_mode = peers_count <= 1
    confidence = (
        44.0
        + min(22.0, (bull_hits + bear_hits) * 2.8)
        + min(10.0, peers_count * 1.2)
        + (6.0 if not proxy_mode else 0.0)
    )
    if proxy_mode:
        confidence -= 8.0
    if beta_risk == "high_beta":
        confidence -= 4.0
    confidence = max(25.0, min(80.0, confidence))
    regime_fit = 58.0 + min(18.0, abs(alpha_score) * 0.85)
    if beta_risk == "high_beta":
        regime_fit -= 2.0

    evidence = {
        "peers_count": peers_count,
        "vs_peer_avg_pe_pct": round(vs_peer_avg_pe, 2),
        "pe_vs_industry_pct": round(pe_vs_ind, 2),
        "ytd_return_pct": round(ytd, 2),
        "mom_20d_pct": round(mom20, 2),
        "industry_proxy_20d_pct": round(ind_proxy_20d, 3),
        "rel_mom_vs_industry_pct": round(rel_mom_vs_ind, 3),
        "main_fund_5d_net_yi": round(flow_5d, 2),
        "industry_growth_pct": round(ind_growth, 2),
        "stage_num": stage,
        "size_style": size_style,
        "style_bias": style_bias,
        "etf_pair_leg": etf_pair.get("etf_pair_leg"),
        "etf_hedge_leg": etf_pair.get("etf_hedge_leg"),
        "etf_pair_theme": etf_pair.get("etf_pair_theme"),
        "industry_overlay": etf_pair.get("industry_overlay"),
        "etf_pair_bias_score": round(etf_pair_bias, 3),
        "beta_vol_proxy": round(beta_vol, 3),
        "beta_target_proxy": _f(beta_proxy.get("beta_target_proxy")),
        "beta_neutral_hedge_ratio": _f(beta_proxy.get("beta_neutral_hedge_ratio")),
        "post_hedge_beta_proxy": _f(beta_proxy.get("post_hedge_beta_proxy")),
        "beta_risk_bucket": beta_risk,
        "pair_alpha_score": round(alpha_score, 3),
        "proxy_mode": proxy_mode,
        "bull_hits": bull_hits,
        "bear_hits": bear_hits,
    }

    if alpha_score >= 12 and bull_hits >= 3 and bull_hits > bear_hits:
        return _mk_signal(
            "bullish", 57 + min(20, bull_hits * 4), confidence, "position", regime_fit + 4,
            "Industry pair spread, ETF style-pair bias, and beta-neutral proxy jointly support long-leg alpha.",
            evidence,
        )
    if alpha_score <= -12 and bear_hits >= 3 and bear_hits > bull_hits:
        return _mk_signal(
            "bearish", 57 + min(20, bear_hits * 4), confidence, "position", regime_fit,
            "Industry pair spread is adverse and beta/ETF pair proxies indicate relative underperformance risk.",
            evidence,
        )
    return _mk_signal(
        "neutral", 52, confidence - 6.0, "position", regime_fit - 4.0,
        "Pair-neutral signals are mixed; keep neutral until industry spread and ETF/beta legs re-align.",
        evidence,
    )


def _strategy_event_drift(features: dict, regime: dict, ctx: dict) -> dict:
    raw = ctx.get("raw") or {}
    candles = ctx.get("candles_60d") or []
    events = _get_dim_data(raw, "15_events")
    timeline = events.get("event_timeline") or []
    catalysts = events.get("catalyst") or []
    warnings = events.get("warnings") or []
    disclosures_count = int(_f(events.get("disclosures_count")))
    news_count = int(_f(events.get("news_count")))

    pos_kw = ["中标", "合同", "合作", "回购", "分红", "预增", "增长", "获批", "新品", "突破"]
    neg_kw = ["减持", "立案", "处罚", "风险", "下滑", "减值", "诉讼", "退市", "亏损", "预减"]

    surprise = _event_surprise_stats(events)
    pead = _pead_proxy(candles, events)
    blocks = surprise.get("blocks") or []
    timeline_text = " ".join([str(x) for x in timeline]).lower()
    catalyst_text = " ".join(
        [str((c.get("event") if isinstance(c, dict) else c) or "") for c in catalysts]
    ).lower()
    warning_text = " ".join([str(w) for w in warnings]).lower()
    all_text = f"{timeline_text} {catalyst_text} {warning_text}"

    pos_hits = sum(1 for kw in pos_kw if kw in all_text)
    neg_hits = sum(1 for kw in neg_kw if kw in all_text)

    now = datetime.now()
    recent_7d = recent_30d = 0
    recent_1_5d = recent_6_20d = 0
    for b in blocks:
        dt = b.get("date")
        if not isinstance(dt, datetime):
            continue
        if dt >= now - timedelta(days=7):
            recent_7d += 1
            recent_30d += 1
        elif dt >= now - timedelta(days=30):
            recent_30d += 1
        if dt >= now - timedelta(days=5):
            recent_1_5d += 1
        elif dt >= now - timedelta(days=20):
            recent_6_20d += 1

    event_intensity = min(
        100.0,
        15.0
        + disclosures_count * 1.2
        + news_count * 1.0
        + len(catalysts) * 6.0
        + recent_7d * 6.0
        + recent_30d * 1.5,
    )
    drift_score = (
        (pos_hits - neg_hits) * 10
        + len(catalysts) * 3
        - len(warnings) * 6
        + _f(surprise.get("surprise_net")) * 8
        + _f(pead.get("pead_drift_5d_pct")) * 2.0
        + _f(pead.get("pead_drift_20d_pct")) * 1.0
    )
    has_positive = bool(features.get("has_positive_catalyst"))
    has_negative = bool(features.get("has_negative_catalyst"))
    if has_positive:
        drift_score += 6
    if has_negative:
        drift_score -= 6

    confidence = min(
        85.0,
        35.0
        + min(event_intensity, 40.0) * 0.8
        + min(disclosures_count + news_count, 15) * 1.2,
    )
    regime_fit = min(85.0, 50.0 + event_intensity * 0.35)

    evidence = {
        "event_intensity": round(event_intensity, 1),
        "drift_score": round(drift_score, 1),
        "recent_events_count": int(features.get("recent_events_count") or 0),
        "disclosures_count": disclosures_count,
        "news_count": news_count,
        "catalyst_count": len(catalysts),
        "warning_count": len(warnings),
        "recent_7d_events": recent_7d,
        "recent_30d_events": recent_30d,
        "recent_1_5d_events": recent_1_5d,
        "recent_6_20d_events": recent_6_20d,
        "positive_kw_hits": pos_hits,
        "negative_kw_hits": neg_hits,
        "surprise_net": surprise.get("surprise_net", 0),
        "surprise_positive_count": surprise.get("surprise_positive_count", 0),
        "surprise_negative_count": surprise.get("surprise_negative_count", 0),
        "strongest_positive": surprise.get("strongest_positive", ""),
        "strongest_negative": surprise.get("strongest_negative", ""),
        "pead_samples": pead.get("pead_samples", 0),
        "pead_drift_1d_pct": pead.get("pead_drift_1d_pct", 0.0),
        "pead_drift_5d_pct": pead.get("pead_drift_5d_pct", 0.0),
        "pead_drift_20d_pct": pead.get("pead_drift_20d_pct", 0.0),
        "has_positive_catalyst": has_positive,
        "has_negative_catalyst": has_negative,
    }

    if event_intensity < 20:
        return _mk_signal(
            "neutral", 48, max(25.0, confidence - 15.0), "swing", max(45.0, regime_fit - 10.0),
            "Event visibility is sparse; no reliable post-event drift edge.",
            evidence,
        )

    if drift_score >= 16 and (pos_hits >= neg_hits or _f(pead.get("pead_drift_5d_pct")) > 0):
        return _mk_signal(
            "bullish", min(80.0, 55.0 + drift_score * 0.6), confidence, "swing", regime_fit,
            "Positive event surprise and PEAD proxy support upside drift in the next 1-20 trading days.",
            evidence,
        )
    if drift_score <= -16 and (neg_hits >= pos_hits or _f(pead.get("pead_drift_5d_pct")) < 0):
        return _mk_signal(
            "bearish", min(80.0, 55.0 + abs(drift_score) * 0.6), confidence, "swing", regime_fit,
            "Negative event surprise and PEAD proxy indicate elevated downside drift risk.",
            evidence,
        )
    return _mk_signal(
        "neutral", 52, confidence - 6.0, "swing", regime_fit - 4.0,
        "Event intensity exists, but positive and negative catalysts are mixed.",
        evidence,
    )


def _strategy_placeholder(_: dict, __: dict, spec: StrategySpec) -> dict:
    return _mk_signal(
        "neutral", 45, 20, "position", 45,
        f"{spec.title} skeleton is registered; calibration is planned in {spec.phase_hint}.",
        {"status": "skeleton"},
    )


_IMPL: dict[str, Callable[[dict, dict, dict], dict]] = {
    "trend_momentum": _strategy_trend_momentum,
    "reversal_mean_revert": _strategy_reversal,
    "flow_turnover_behavior": _strategy_flow_turnover,
    "low_vol_risk_management": _strategy_low_vol,
    "value_repair": _strategy_value_repair,
    "quality_improvement": _strategy_quality_improvement,
    "calendar_seasonality": _strategy_calendar_seasonality,
    "intraday_timing": _strategy_intraday_timing,
    "pair_relative_strength": _strategy_pair_relative_strength,
    "limit_up_ecology": _strategy_limit_up,
    "industry_style_rotation": _strategy_rotation,
    "event_drift": _strategy_event_drift,
}


def _run_spec(spec: StrategySpec, features: dict, regime: dict, market: str, ctx: dict) -> dict:
    if spec.requires_a_share and market != "A":
        sig = _mk_signal(
            "skip", 0, 100, "position", 0,
            "Strategy is currently scoped to A-share microstructure.",
            {"market": market},
        )
    else:
        fn = _IMPL.get(spec.strategy_id)
        sig = fn(features, regime, ctx) if fn else _strategy_placeholder(features, regime, spec)

    sig["strategy_id"] = spec.strategy_id
    sig["family"] = spec.family
    sig["title"] = spec.title
    return sig


def _summary(signals: list[dict]) -> dict:
    bullish = [s for s in signals if s.get("signal") == "bullish"]
    bearish = [s for s in signals if s.get("signal") == "bearish"]
    neutral = [s for s in signals if s.get("signal") == "neutral"]
    skip = [s for s in signals if s.get("signal") == "skip"]

    bullish_sorted = sorted(bullish, key=lambda x: -_f(x.get("strength")))
    bearish_sorted = sorted(bearish, key=lambda x: -_f(x.get("strength")))

    return {
        "bullish_count": len(bullish),
        "bearish_count": len(bearish),
        "neutral_count": len(neutral),
        "skip_count": len(skip),
        "top_bullish": [
            {"strategy_id": s.get("strategy_id"), "strength": s.get("strength"), "explain": s.get("explain")}
            for s in bullish_sorted[:3]
        ],
        "top_bearish": [
            {"strategy_id": s.get("strategy_id"), "strength": s.get("strength"), "explain": s.get("explain")}
            for s in bearish_sorted[:3]
        ],
    }


def validate_signal_schema(signals_doc: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(signals_doc, dict):
        return ["signals_doc is not a dict"]
    sigs = signals_doc.get("signals")
    if not isinstance(sigs, list):
        return ["signals_doc.signals is not a list"]
    required = {"strategy_id", "signal", "strength", "confidence", "horizon", "regime_fit", "explain", "evidence"}
    for i, s in enumerate(sigs):
        if not isinstance(s, dict):
            errs.append(f"signals[{i}] not dict")
            continue
        missing = [k for k in required if k not in s]
        if missing:
            errs.append(f"signals[{i}] missing keys: {','.join(missing)}")
    return errs


def build_strategy_outputs(ticker: str, raw: dict, dims_scored: dict, depth: str) -> tuple[dict, dict, dict]:
    now = datetime.now().isoformat(timespec="seconds")
    base = extract_features(raw, raw.get("dimensions", {}))
    market = str(raw.get("market") or base.get("market") or "A")
    regime = _regime(base)
    candles = _candles_60d(raw)
    closes = _close_series(candles)
    rets = _daily_close_returns(closes)
    overnight, intraday = _overnight_intraday(candles)
    raw_mom_20d = _window_return(closes, 20)
    market = str(raw.get("market") or base.get("market") or "A")
    extreme_th = 9.5 if market == "A" else 14.0
    filtered = [r for r in rets[-20:] if abs(r) < extreme_th]
    corrected_mom_20d = sum(filtered) if filtered else 0.0
    ind_growth = _industry_growth_pct(raw, base)
    residual_proxy_20d = raw_mom_20d - ind_growth * (20.0 / 252.0)
    ov_mean_10d = mean(overnight[-10:]) if len(overnight) >= 3 else 0.0
    intra_mean_10d = mean(intraday[-10:]) if len(intraday) >= 3 else 0.0
    limit_up = _limit_up_stats(candles, market)
    indicators = _get_dim_data(raw, "2_kline").get("indicators") or {}
    flow_rows = _main_flow_20d(raw)
    adx = _adx14(candles)
    don = _donchian_stats(candles, n=20)
    boll = _bollinger_stats(closes, n=20, k=2.0)
    kdj = _kdj_stats(candles, n=9)
    cci = _cci20(candles, n=20)
    gaps = _gap_fill_stats(candles, lookback=20)
    flow_proxies = _flow_indicator_proxies(flow_rows)
    board_proxies = _board_behavior_proxies(candles, flow_rows)
    limit_patterns = _limit_pattern_stats(candles, market)
    theme_diffusion = _sector_lhb_theme_diffusion(raw)
    style_rotation = _style_rotation_proxies(base, raw, candles)
    pead = _pead_proxy(candles, _get_dim_data(raw, "15_events"))
    atr = _atr14(candles)
    ivol = _ivol_stats(rets)
    vt = _vol_targeting_proxy(_f(ivol.get("ivol_20d_daily_pct")))
    rp = _risk_parity_proxy(
        _f(base.get("volatility_1y")),
        _f(ivol.get("ivol_20d_annual_pct")),
        _f(atr.get("atr14_pct")),
        _f(base.get("max_drawdown_1y")),
    )
    ctx = {"raw": raw, "candles_60d": candles}

    features_doc = {
        "ticker": ticker,
        "generated_at": now,
        "market": market,
        "depth": depth,
        "features": {
            "price": _f(base.get("price")),
            "change_pct": _f(base.get("change_pct")),
            "market_cap_yi": _f(base.get("market_cap_yi")),
            "industry": base.get("industry", "—"),
            "stage_num": int(base.get("stage_num") or 0),
            "ma_bull_aligned": bool(base.get("ma_bull_aligned")),
            "rsi": _f(base.get("rsi")),
            "ytd_return": _f(base.get("ytd_return")),
            "volatility_1y": _f(base.get("volatility_1y")),
            "max_drawdown_1y": _f(base.get("max_drawdown_1y")),
            "turnover_rate": _f(base.get("turnover_rate")),
            "main_fund_5d_net_yi": _f(base.get("main_fund_5d_net_yi")),
            "sentiment_heat": _f(base.get("sentiment_heat")),
            "lhb_30d_count": _f(base.get("lhb_30d_count")),
            "matched_youzi_count": _f(base.get("matched_youzi_count")),
            "pe": _f(base.get("pe")),
            "pb": _f(base.get("pb")),
            "earnings_yield_pct": _f(base.get("earnings_yield")),
            "cf_to_price_pct": _f(base.get("cf_to_price")),
            "fcf_to_price_pct": _f(base.get("fcf_to_price")),
            "ev_ebitda": _f(base.get("ev_ebitda")),
            "is_state_owned": bool(base.get("is_state_owned")),
            "is_below_book": bool(base.get("is_below_book")),
            "industry_growth_pct": _f(base.get("industry_growth_pct")),
            "fundamental_score": _f((dims_scored or {}).get("fundamental_score")),
            "raw_momentum_20d_pct": round(raw_mom_20d, 2),
            "corrected_momentum_20d_pct": round(corrected_mom_20d, 2),
            "residual_proxy_momentum_20d_pct": round(residual_proxy_20d, 2),
            "overnight_momentum_10d_pct": round(ov_mean_10d, 2),
            "intraday_momentum_10d_pct": round(intra_mean_10d, 2),
            "intraday_minute_available": bool(base.get("intraday_minute_available")),
            "intraday_minute_bars_count": _f(base.get("intraday_minute_bars_count")),
            "open_auction_ret_pct": _f(base.get("open_auction_ret_pct")),
            "open_15m_ret_pct": _f(base.get("open_15m_ret_pct")),
            "tail_30m_ret_pct": _f(base.get("tail_30m_ret_pct")),
            "open_auction_volume_ratio": _f(base.get("open_auction_volume_ratio")),
            "close_auction_volume_ratio": _f(base.get("close_auction_volume_ratio")),
            "close_auction_jump_pct": _f(base.get("close_auction_jump_pct")),
            "intraday_amplitude_pct": _f(base.get("intraday_amplitude_pct")),
            "seasonality_sample_days": _f(base.get("seasonality_sample_days")),
            "thursday_mean_return_pct_1y": _f(base.get("thursday_mean_return_pct_1y")),
            "month_start_mean_return_pct_1y": _f(base.get("month_start_mean_return_pct_1y")),
            "month_end_mean_return_pct_1y": _f(base.get("month_end_mean_return_pct_1y")),
            "spring_festival_window_mean_return_pct_1y": _f(base.get("spring_festival_window_mean_return_pct_1y")),
            "removed_extreme_days_20d": max(0, len(rets[-20:]) - len(filtered)),
            "limit_up_hits_20d": limit_up["limit_hits"],
            "limit_up_broken_rate_pct": round(limit_up["broken_rate"], 1),
            "first_board_hits_20d": limit_patterns["first_board_hits"],
            "second_board_hits_20d": limit_patterns["second_board_hits"],
            "third_plus_board_hits_20d": limit_patterns["third_plus_board_hits"],
            "one_word_board_hits_20d": limit_patterns["one_word_board_hits"],
            "t_board_hits_20d": limit_patterns["t_board_hits"],
            "first_yin_break_hits_20d": limit_patterns["first_yin_break_hits"],
            "height_cycle_score": _f(limit_patterns.get("height_cycle_score")),
            "theme_diffusion_score": _f(theme_diffusion.get("theme_diffusion_score")),
            "theme_concentration_pct": _f(theme_diffusion.get("theme_concentration_pct")),
            "size_style": style_rotation.get("size_style"),
            "style_bias": style_rotation.get("style_bias"),
            "growth_style_score": _f(style_rotation.get("growth_style_score")),
            "value_dividend_style_score": _f(style_rotation.get("value_dividend_style_score")),
            "adx14": adx["adx14"],
            "plus_di14": adx["plus_di14"],
            "minus_di14": adx["minus_di14"],
            "donchian_breakout_up": don["breakout_up"],
            "donchian_breakout_down": don["breakout_down"],
            "donchian_width_pct": don["width_pct"],
            "n_day_new_high": don["n_day_new_high"],
            "vol_5_vs_20": _f(indicators.get("vol_5_vs_20")),
            "pct_from_year_high": _f(indicators.get("pct_from_year_high")),
            "boll_zscore": boll["zscore"],
            "kdj_k": kdj["k"],
            "kdj_d": kdj["d"],
            "kdj_j": kdj["j"],
            "cci20": cci,
            "up_gap_count_20d": gaps["up_gap_count"],
            "down_gap_count_20d": gaps["down_gap_count"],
            "up_gap_fill_rate_pct": gaps["up_gap_fill_rate_pct"],
            "down_gap_fill_rate_pct": gaps["down_gap_fill_rate_pct"],
            "obv_proxy": flow_proxies["obv_proxy"],
            "vpt_proxy": flow_proxies["vpt_proxy"],
            "mfi_proxy": flow_proxies["mfi_proxy"],
            "cmf_proxy": flow_proxies["cmf_proxy"],
            "flow_shock_z": flow_proxies["flow_shock_z"],
            "flow_reversal_days": flow_proxies["flow_reversal_days"],
            "fake_board_rate_pct": board_proxies["fake_board_rate_pct"],
            "flow_decay_after_spike_pct": board_proxies["flow_decay_after_spike_pct"],
            "pead_drift_5d_pct": pead["pead_drift_5d_pct"],
            "pead_drift_20d_pct": pead["pead_drift_20d_pct"],
            "atr14": _f(atr.get("atr14")),
            "atr14_pct": _f(atr.get("atr14_pct")),
            "ivol_20d_daily_pct": _f(ivol.get("ivol_20d_daily_pct")),
            "ivol_60d_daily_pct": _f(ivol.get("ivol_60d_daily_pct")),
            "ivol_20d_annual_pct": _f(ivol.get("ivol_20d_annual_pct")),
            "target_leverage": _f(vt.get("target_leverage")),
            "risk_parity_weight_proxy_pct": _f(rp.get("risk_parity_weight_proxy_pct")),
            "risk_budget_score": _f(rp.get("risk_budget_score")),
            "receivable_turnover": _f(base.get("receivable_turnover")),
            "inventory_turnover": _f(base.get("inventory_turnover")),
            "asset_turnover": _f(base.get("asset_turnover")),
            "asset_growth_pct": _f(base.get("asset_growth")),
            "gross_profitability": _f(base.get("gross_profitability")),
        },
        "regime": regime,
    }

    specs = enabled_strategies(depth)
    signals = [_run_spec(spec, base, regime, market, ctx) for spec in specs]
    signals_doc = {
        "ticker": ticker,
        "generated_at": now,
        "market": market,
        "depth": depth,
        "signals": signals,
        "summary": _summary(signals),
    }

    schema_errors = validate_signal_schema(signals_doc)
    meta_doc = {
        "ticker": ticker,
        "generated_at": now,
        "engine_version": "phase1-v2",
        "depth": depth,
        "enabled_strategy_ids": [s.strategy_id for s in specs],
        "enabled_strategy_count": len(specs),
        "registered_strategy_count": len(all_strategies()),
        "schema_version": "strategy-signal-v1",
        "schema_valid": len(schema_errors) == 0,
        "schema_errors": schema_errors,
    }
    return features_doc, signals_doc, meta_doc
