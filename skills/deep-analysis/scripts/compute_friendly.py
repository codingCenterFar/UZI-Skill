"""Tier 4 友好层计算器.

输入：raw_data.json + dimensions.json
输出：friendly 字段 for synthesis.json
  - scenarios: 5 情景模拟（基于历史波动率）
  - exit_triggers: 5 条自动生成的离场触发条件
  - similar_stocks: pass-through from fetch_similar_stocks

Usage:
  python compute_friendly.py {ticker}
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from lib.cache import read_task_output  # noqa: E402


def _parse_pct(s) -> float:
    try:
        return float(str(s).replace("%", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0.0


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _last_nonzero(values: list) -> float | None:
    for v in reversed(values or []):
        x = _f(v, 0.0)
        if abs(x) > 1e-9:
            return x
    return None


def _signal_map(strategy_signals: dict | None) -> dict:
    out = {}
    if not isinstance(strategy_signals, dict):
        return out
    for s in strategy_signals.get("signals") or []:
        if isinstance(s, dict) and s.get("strategy_id"):
            out[s.get("strategy_id")] = s
    return out


def _strategy_edge(sig: dict | None, bull_w: float, bear_w: float, neutral_w: float = 0.0) -> float:
    if not isinstance(sig, dict):
        return 0.0
    signal = str(sig.get("signal") or "neutral")
    strength = _f(sig.get("strength"), 50.0)
    z = (strength - 50.0) / 50.0
    if signal == "bullish":
        return z * bull_w
    if signal == "bearish":
        return -z * bear_w
    return z * neutral_w


def compute_scenarios(raw: dict, dimensions: dict) -> dict:
    """5 级情景（最坏/偏差/合理/乐观/极致乐观）基于历史波动率。"""
    basic = (raw.get("dimensions", {}).get("0_basic") or {}).get("data") or {}
    kline = (raw.get("dimensions", {}).get("2_kline") or {}).get("data") or {}
    research = (raw.get("dimensions", {}).get("6_research") or {}).get("data") or {}

    entry_price = basic.get("price") or 0
    stats = kline.get("kline_stats") or {}
    # 1 年化波动率
    vol_str = stats.get("volatility", "30%")
    sigma = _parse_pct(vol_str) or 30.0

    # 研报目标价隐含的预期收益
    upside_str = research.get("upside", "+15%")
    base_return = _parse_pct(upside_str) or 15.0

    return {
        "entry_price": entry_price,
        "cases": [
            {"name": "最坏情况", "probability": "5%",  "return": round(-2 * sigma, 1)},
            {"name": "偏差情况", "probability": "25%", "return": round(-1 * sigma + base_return * 0.2, 1)},
            {"name": "合理情况", "probability": "40%", "return": round(base_return, 1)},
            {"name": "乐观情况", "probability": "25%", "return": round(1 * sigma + base_return * 0.5, 1)},
            {"name": "极致乐观", "probability": "5%",  "return": round(2 * sigma + base_return, 1)},
        ],
    }


def compute_exit_triggers(
    raw: dict,
    dimensions: dict,
    synthesis: dict,
    strategy_summary: dict | None = None,
) -> list[str]:
    """自动从已有数据生成 5 条离场触发条件。"""
    triggers = []
    basic = (raw.get("dimensions", {}).get("0_basic") or {}).get("data") or {}
    kline = (raw.get("dimensions", {}).get("2_kline") or {}).get("data") or {}
    val = (raw.get("dimensions", {}).get("10_valuation") or {}).get("data") or {}
    chain = (raw.get("dimensions", {}).get("5_chain") or {}).get("data") or {}
    lhb = (raw.get("dimensions", {}).get("16_lhb") or {}).get("data") or {}
    research = (raw.get("dimensions", {}).get("6_research") or {}).get("data") or {}

    # 0. 策略层风向（T205：让 exit_triggers 显式消费 strategy 输出）
    if isinstance(strategy_summary, dict) and strategy_summary:
        bull_n = int(strategy_summary.get("bullish_count", 0) or 0)
        bear_n = int(strategy_summary.get("bearish_count", 0) or 0)
        top_bear = (strategy_summary.get("top_bearish") or [{}])[0]
        top_bull = (strategy_summary.get("top_bullish") or [{}])[0]
        if bear_n > bull_n and top_bear:
            sid = top_bear.get("strategy_id", "bearish_strategy")
            strength = top_bear.get("strength", "—")
            triggers.append(f"策略层转空：{sid} 强度 {strength} 持续走高 → 先减仓再观察")
        elif bull_n >= bear_n and top_bull:
            sid = top_bull.get("strategy_id", "bullish_strategy")
            strength = top_bull.get("strength", "—")
            triggers.append(f"策略共振失效：{sid} 强度由 {strength} 明显回落且 bull<=bear → 执行减仓")

    # 1. 技术止损 ~ MA60
    ma60 = (kline.get("ma60_60d") or [])
    ma60_last = next((v for v in reversed(ma60) if v), None)
    if ma60_last:
        triggers.append(f"股价跌破 ¥{ma60_last:.2f}（60 日均线支撑位）→ 无条件止损")
    else:
        price = basic.get("price") or 0
        triggers.append(f"股价跌破 ¥{price * 0.88:.2f}（当前价 -12%）→ 无条件止损")

    # 2. 基本面恶化 — 大客户
    downstream = chain.get("downstream", "")
    if downstream and downstream != "—":
        main_client = downstream.split("/")[0].strip()
        triggers.append(f"{main_client} 季度指引下修 > 10% → 产业链逻辑动摇")
    else:
        triggers.append("下季度营收同比转负 → 基本面反转信号")

    # 3. 业绩不达
    growth_str = research.get("upside", "+15%")
    g = _parse_pct(growth_str)
    if g > 0:
        min_growth = max(10, int(g - 15))
        triggers.append(f"下次业绩预告低于 +{min_growth}% → 预期管理失守")
    else:
        triggers.append("连续两期业绩不及券商预期中位数 → 逻辑失效")

    # 4. 游资撤离
    matched = lhb.get("matched_youzi", "")
    if isinstance(matched, list):
        matched_str = " / ".join(matched[:2])
    else:
        matched_str = str(matched).split("/")[0] if matched else "顶级游资"
    if matched_str and matched_str not in ("", "—"):
        triggers.append(f"{matched_str} 席位大额卖出 > 2 亿 → 顶级资金撤离信号")

    # 5. 估值泡沫
    pe_quant = val.get("pe_quantile", "")
    import re
    m = re.search(r'(\d+)', str(pe_quant))
    if m:
        cur_q = int(m.group(1))
        target = min(95, cur_q + 15)
        triggers.append(f"PE 站上 5 年 {target} 分位（≈ {val.get('pe', '—')} × {1 + (target - cur_q) / 100:.2f}）→ 泡沫区获利了结")
    else:
        triggers.append("PE 站上 5 年 90 分位 → 泡沫区获利了结")

    return triggers[:5]


def compute_short_trading_module(
    raw: dict,
    dimensions: dict,
    synthesis: dict,
    strategy_signals: dict | None = None,
    strategy_summary: dict | None = None,
) -> dict:
    """短线交易模块（快进快出）：
    - 节奏评分（0-100）
    - 实盘触发条件（入场/加仓/止盈/止损/禁做）
    - 关键盘中数据快照
    """
    basic = (raw.get("dimensions", {}).get("0_basic") or {}).get("data") or {}
    kline = (raw.get("dimensions", {}).get("2_kline") or {}).get("data") or {}
    cap = (raw.get("dimensions", {}).get("12_capital_flow") or {}).get("data") or {}
    lhb = (raw.get("dimensions", {}).get("16_lhb") or {}).get("data") or {}
    sent = (raw.get("dimensions", {}).get("17_sentiment") or {}).get("data") or {}

    price = _f(basic.get("price"))
    stage = str(kline.get("stage") or "—")
    ma_align = str(kline.get("ma_align") or "—")
    rsi = _f(kline.get("rsi"))
    intraday = kline.get("intraday_micro") or {}
    open_15m = _f(intraday.get("open_15m_ret_pct"))
    tail_30m = _f(intraday.get("tail_30m_ret_pct"))
    open_auc_vol = _f(intraday.get("open_auction_volume_ratio"))
    close_auc_vol = _f(intraday.get("close_auction_volume_ratio"))
    micro_available = bool(intraday.get("micro_available"))

    lhb_30d = int(_f(lhb.get("lhb_count_30d")))
    matched_youzi_count = len(lhb.get("matched_youzi") or [])
    sentiment_heat = _f(sent.get("thermometer_value"))
    sentiment_pos = _parse_pct(sent.get("positive_pct"))

    flow_5d_net = 0.0
    for rec in (cap.get("main_fund_flow_20d") or [])[:5]:
        if isinstance(rec, dict):
            flow_5d_net += _f(rec.get("主力净流入-净额"))
    flow_5d_net_yi = round(flow_5d_net / 1e8, 2)

    sig_map = _signal_map(strategy_signals)
    trend_sig = sig_map.get("trend_momentum")
    flow_sig = sig_map.get("flow_turnover_behavior")
    event_sig = sig_map.get("event_drift")
    intraday_sig = sig_map.get("intraday_timing")
    limit_sig = sig_map.get("limit_up_ecology")

    summary = strategy_summary if isinstance(strategy_summary, dict) else {}
    if not summary and isinstance(strategy_signals, dict):
        summary = strategy_signals.get("summary") or {}
    bull_n = int(_f(summary.get("bullish_count")))
    bear_n = int(_f(summary.get("bearish_count")))
    neutral_n = int(_f(summary.get("neutral_count")))

    score = 50.0
    if "Stage 2" in stage:
        score += 10
    elif "Stage 3" in stage or "Stage 4" in stage:
        score -= 10

    if "多头" in ma_align:
        score += 7
    else:
        score -= 6

    # 策略层作为短线核心权重
    score += _strategy_edge(intraday_sig, bull_w=14.0, bear_w=14.0)
    score += _strategy_edge(trend_sig, bull_w=10.0, bear_w=10.0)
    score += _strategy_edge(limit_sig, bull_w=8.0, bear_w=8.0)
    score += _strategy_edge(flow_sig, bull_w=5.0, bear_w=12.0)
    score += _strategy_edge(event_sig, bull_w=4.0, bear_w=10.0)

    # 盘口/资金/情绪加减分
    if flow_5d_net_yi > 0:
        score += min(6.0, flow_5d_net_yi * 1.5)
    elif flow_5d_net_yi < 0:
        score -= min(6.0, abs(flow_5d_net_yi) * 1.5)
    if lhb_30d >= 1:
        score += 3
    if matched_youzi_count >= 1:
        score += 3
    if sentiment_heat >= 85:
        score += 2
    if sentiment_heat >= 95 and sentiment_pos >= 90:
        score -= 2  # 情绪过热，防止尾盘追高
    if micro_available and open_15m > 0:
        score += 2
    if micro_available and tail_30m < 0:
        score -= 2

    score = max(0.0, min(100.0, score))

    if bull_n >= bear_n + 2:
        bias = "偏多"
    elif bear_n >= bull_n + 2:
        bias = "偏空"
    else:
        bias = "中性"

    if score >= 70:
        rhythm = "进攻节奏"
        init_pos = "30%"
        max_pos = "70%"
    elif score >= 55:
        rhythm = "试错节奏"
        init_pos = "20%"
        max_pos = "50%"
    else:
        rhythm = "防守节奏"
        init_pos = "10%"
        max_pos = "30%"

    candles = kline.get("candles_60d") or []
    highs_20 = [_f(c.get("high")) for c in candles[-20:] if isinstance(c, dict)]
    lows_10 = [_f(c.get("low")) for c in candles[-10:] if isinstance(c, dict)]
    ma20_last = _last_nonzero(kline.get("ma20_60d") or [])
    ma60_last = _last_nonzero(kline.get("ma60_60d") or [])

    breakout = round(max(highs_20), 2) if highs_20 else round(price * 1.02, 2) if price else 0.0
    retest = round(ma20_last, 2) if ma20_last else round(price * 0.97, 2) if price else 0.0
    _stop_candidates = [
        retest * 0.985 if retest else 0,
        ma60_last if ma60_last else 0,
        (min(lows_10) if lows_10 else 0),
    ]
    _stop_candidates = [v for v in _stop_candidates if v > 0]
    hard_stop = round(min(_stop_candidates), 2) if _stop_candidates else round(price * 0.94, 2) if price else 0.0
    tp1 = round(price * 1.03, 2) if price else 0.0
    tp2 = round(price * 1.06, 2) if price else 0.0

    pre_open_checks = [
        f"竞价量比 > 1.2（当前统计 {open_auc_vol:.2f}）再考虑开仓",
        "开盘 15 分钟不追高，先等第一波回踩确认承接",
        "若策略层看空数量继续高于看多，今天仅做低吸，不做追涨",
    ]
    if bias == "偏空":
        pre_open_checks[2] = "策略层偏空，今日只允许轻仓试错，不允许情绪化加仓"

    entry_rules = [
        f"突破入场：放量突破 ¥{breakout:.2f} 且 5 分钟不回落到突破位下方",
        f"回踩入场：回踩 ¥{retest:.2f} 附近止跌，出现分时二次放量再进",
        "二选一执行，单日最多触发一次，避免来回打脸",
    ]
    add_rules = [
        "首仓盈利 > +1.5% 且回踩不破成本线，再加仓 10%-20%",
        "若尾盘 30 分钟转强（量价齐升）可留隔夜；否则减回初始仓位",
        f"总仓位上限 {max_pos}，不满条件不加仓",
    ]
    take_profit_rules = [
        f"分批止盈：+3% 附近先落袋 1/3（参考 ¥{tp1:.2f}）",
        f"+6% 附近再落袋 1/3（参考 ¥{tp2:.2f}），剩余仓位设移动止盈",
        "若冲高回落并跌破 5 分钟均线，执行当日保护性减仓",
    ]
    stop_rules = [
        f"硬止损：跌破 ¥{hard_stop:.2f} 直接离场，不补仓",
        "开仓后 30-60 分钟仍无正反馈（量能不跟/反抽无力），减仓到观察仓",
        "出现资金分歧放大（放量滞涨 + 回落）立即降风险",
    ]
    avoid_rules = [
        "竞价高开过大且量能不跟，不追",
        "开盘前 30 分钟振幅过大但成交萎缩，不做",
        "高位连续一致预期（情绪过热）时，优先等分歧转一致后的确认点",
    ]

    quick_stats = {
        "stage": stage,
        "ma_align": ma_align,
        "rsi": round(rsi, 1),
        "main_fund_5d_net_yi": flow_5d_net_yi,
        "lhb_30d": lhb_30d,
        "matched_youzi_count": matched_youzi_count,
        "sentiment_heat": round(sentiment_heat, 1),
        "sentiment_positive_pct": round(sentiment_pos, 1),
        "open_15m_ret_pct": round(open_15m, 3),
        "tail_30m_ret_pct": round(tail_30m, 3),
        "open_auction_volume_ratio": round(open_auc_vol, 3),
        "close_auction_volume_ratio": round(close_auc_vol, 3),
        "intraday_micro_available": micro_available,
    }

    top_bull = (summary.get("top_bullish") or [])[:2]
    top_bear = (summary.get("top_bearish") or [])[:2]
    short_signal_board = [
        {
            "strategy_id": s.get("strategy_id"),
            "signal": "bullish",
            "strength": s.get("strength"),
            "explain": s.get("explain"),
        }
        for s in top_bull
    ] + [
        {
            "strategy_id": s.get("strategy_id"),
            "signal": "bearish",
            "strength": s.get("strength"),
            "explain": s.get("explain"),
        }
        for s in top_bear
    ]

    return {
        "mode": "short_term_fast_in_out",
        "holding_window": "1-3 个交易日（严格执行 T+1 约束）",
        "setup_score": round(score, 1),
        "rhythm": rhythm,
        "bias": bias,
        "position_plan": {
            "initial": init_pos,
            "max": max_pos,
            "notes": "先试错、再确认、再扩仓；永远先控回撤再谈收益。",
        },
        "levels": {
            "breakout_entry": breakout,
            "retest_entry": retest,
            "hard_stop": hard_stop,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
        },
        "pre_open_checks": pre_open_checks,
        "entry_rules": entry_rules,
        "add_rules": add_rules,
        "take_profit_rules": take_profit_rules,
        "stop_rules": stop_rules,
        "avoid_rules": avoid_rules,
        "quick_stats": quick_stats,
        "strategy_summary": {
            "bullish_count": bull_n,
            "bearish_count": bear_n,
            "neutral_count": neutral_n,
        },
        "signal_board": short_signal_board,
    }


def main(ticker: str) -> dict:
    raw = read_task_output(ticker, "raw_data") or {}
    dimensions = read_task_output(ticker, "dimensions") or {}
    synthesis = read_task_output(ticker, "synthesis") or {}
    strategy_signals = read_task_output(ticker, "strategy_signals") or {}
    strategy_summary = (strategy_signals.get("summary") or {}) if isinstance(strategy_signals, dict) else {}

    scenarios = compute_scenarios(raw, dimensions)
    exit_triggers = compute_exit_triggers(raw, dimensions, synthesis, strategy_summary=strategy_summary)
    short_trading = compute_short_trading_module(
        raw,
        dimensions,
        synthesis,
        strategy_signals=strategy_signals if isinstance(strategy_signals, dict) else None,
        strategy_summary=strategy_summary,
    )

    # Similar stocks: 从 raw_data 的 similar_stocks stub 或独立 cache
    similar = (raw.get("similar_stocks") or [])[:4]

    friendly = {
        "scenarios": scenarios,
        "exit_triggers": exit_triggers,
        "similar_stocks": similar,
        "short_trading": short_trading,
    }

    return friendly


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else "002273.SZ"), ensure_ascii=False, indent=2, default=str))
