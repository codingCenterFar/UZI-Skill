from __future__ import annotations

from typing import Any

DEFAULT_LANG = "zh-CN"


def normalize_lang(lang: str | None) -> str:
    value = (lang or DEFAULT_LANG).strip().lower().replace("_", "-")
    if value in {"zh", "zh-cn", "cn", "chinese"}:
        return "zh-CN"
    if value in {"en", "en-us", "english"}:
        return "en"
    return DEFAULT_LANG


_TEXT_ZH = {
    "analysis_age": "分析年龄",
    "analysis_freshness": "分析新鲜度",
    "auto_refresh": "自动刷新",
    "avg": "均价",
    "avg_px": "成交均价",
    "basis": "依据",
    "bucket": "分组",
    "candidate_pool": "候选池",
    "candidates": "候选数",
    "cash": "现金",
    "change": "涨跌幅",
    "equity": "总权益",
    "event_lag": "事件积压",
    "events": "事件",
    "filled": "已成交",
    "flags": "标记",
    "generated": "生成时间",
    "health": "健康状态",
    "health_error": "健康错误",
    "intent": "意图",
    "last": "最新价",
    "last_loop": "最近循环",
    "lots": "批次",
    "market_value": "持仓市值",
    "orders": "订单",
    "positions": "持仓",
    "price": "价格",
    "print": "打印",
    "qty": "数量",
    "quote": "行情",
    "quote_age": "行情年龄",
    "quote_freshness": "行情新鲜度",
    "quote_risk": "行情风险",
    "rank": "排名",
    "reasons": "原因",
    "refresh": "刷新",
    "return": "收益",
    "runtime": "运行时",
    "runtime_error": "运行错误",
    "score": "分数",
    "sellable": "可卖",
    "session": "会话",
    "side": "方向",
    "source": "来源",
    "status": "状态",
    "ticker": "代码",
    "trigger": "触发价",
    "watchlist": "观察列表",
    "wait_avoid": "等待/回避",
}

_TEXT_EN = {
    "analysis_age": "Analysis Age",
    "analysis_freshness": "Analysis Freshness",
    "auto_refresh": "auto refresh",
    "avg": "Avg",
    "avg_px": "Avg Px",
    "basis": "Basis",
    "bucket": "Bucket",
    "candidate_pool": "Candidate Pool",
    "candidates": "Candidates",
    "cash": "Cash",
    "change": "Chg",
    "equity": "Equity",
    "event_lag": "Event Lag",
    "events": "Events",
    "filled": "Filled",
    "flags": "Flags",
    "generated": "generated",
    "health": "health",
    "health_error": "Health Error",
    "intent": "Intent",
    "last": "Last",
    "last_loop": "Last Loop",
    "lots": "Lots",
    "market_value": "Market Value",
    "orders": "Orders",
    "positions": "Positions",
    "price": "Price",
    "print": "Print",
    "qty": "Qty",
    "quote": "Quote",
    "quote_age": "Quote Age",
    "quote_freshness": "Quote Freshness",
    "quote_risk": "Quote Risk",
    "rank": "Rank",
    "reasons": "Reasons",
    "refresh": "Refresh",
    "return": "Return",
    "runtime": "runtime",
    "runtime_error": "Runtime Error",
    "score": "Score",
    "sellable": "Sellable",
    "session": "Session",
    "side": "Side",
    "source": "Source",
    "status": "Status",
    "ticker": "Ticker",
    "trigger": "Trigger",
    "watchlist": "Watchlist",
    "wait_avoid": "Wait / Avoid",
}

_LABEL_ZH = {
    "ok": "正常",
    "unknown": "未知",
    "warning": "警告",
    "error": "错误",
    "failed": "失败",
    "missing": "缺失",
    "stopped": "已停止",
    "running": "运行中",
    "completed": "已完成",
    "queued": "排队中",
    "partial": "部分完成",
    "published": "已发布",
    "filled": "已成交",
    "rejected": "已拒绝",
    "dead": "失效",
    "BUY": "买入",
    "SELL": "卖出",
    "POSITION": "已持仓",
    "WAIT": "等待",
    "AVOID": "回避",
    "CANDIDATE_A": "候选A",
    "PAPER_BUY_A": "模拟买入A",
    "PAPER_WATCH_B": "模拟关注B",
    "CANDIDATE_OBSERVE": "候选观察",
    "OBSERVE": "观察",
    "BREAKOUT_WAIT": "等突破",
    "PULLBACK_WAIT": "等回踩",
    "NO_TRADE_AVOID": "不交易/回避",
    "BUY_READY": "买入就绪",
    "position_candidate": "持仓候选",
    "agent_reviewed": "已完成 Agent 复核",
    "wait_pullback": "等待回踩",
    "quality_or_reviewed_wait_pullback": "质量/复核通过但等待回踩",
    "quote_failed": "行情失败",
    "quote_missing": "行情缺失",
    "quote_stale": "行情过期",
    "analysis_stale": "分析过期",
    "analysis_missing": "分析缺失",
    "cache_synthesis": "缓存研判",
    "from_cache": "来自缓存",
    "realtime_quote": "实时行情",
    "watcher_overlay": "盯盘叠加",
    "manual": "手动",
    "pytest_provider": "测试行情源",
    "signal": "信号",
    "order": "订单",
    "position": "持仓",
    "runtime": "运行时",
    "runtime_session": "运行会话",
    "portfolio_nav": "组合净值",
    "loop": "循环",
    "nav.updated": "净值更新",
    "loop.started": "循环开始",
    "loop.finished": "循环结束",
    "signal.generated": "信号生成",
    "session.after_close_review": "盘后复盘",
}


def t(key: str, *, lang: str | None = None, default: str | None = None) -> str:
    normalized = normalize_lang(lang)
    table = _TEXT_ZH if normalized == "zh-CN" else _TEXT_EN
    return table.get(key, default if default is not None else key)


def display_label(value: Any, *, lang: str | None = None) -> str:
    if value is None or value == "":
        return "-"
    raw = str(value)
    normalized = normalize_lang(lang)
    if normalized == "en":
        return raw

    if raw in _LABEL_ZH:
        return _LABEL_ZH[raw]
    lower = raw.lower()
    if lower in _LABEL_ZH:
        return _LABEL_ZH[lower]

    if ":" in raw:
        prefix, suffix = raw.split(":", 1)
        prefix_lower = prefix.lower()
        if prefix_lower == "latest_signal":
            return f"最新信号：{display_label(suffix, lang=normalized)}"
        if prefix_lower == "short_bias":
            return f"短线偏向：{suffix}"
        if prefix_lower == "strategy_bull_bear":
            return f"策略多空：{suffix}"
        translated_prefix = _LABEL_ZH.get(prefix, _LABEL_ZH.get(prefix_lower))
        if translated_prefix:
            return f"{translated_prefix}：{display_label(suffix, lang=normalized)}"

    return raw
