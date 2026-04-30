"""Regression tests for strategy roadmap completion (T205 / T303 / T304)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


def test_stage1_panel_soft_fusion_hooked():
    src = (SCRIPTS_DIR / "run_real_test.py").read_text(encoding="utf-8")
    assert "def _apply_strategy_soft_fusion(" in src, "T205 missing: strategy soft fusion helper not found"
    assert "panel = _apply_strategy_soft_fusion(panel, strategy_signals)" in src, \
        "T205 missing: stage1 should fuse panel with strategy summary"


def test_stage2_synthesis_consumes_strategy_summary():
    src = (SCRIPTS_DIR / "run_real_test.py").read_text(encoding="utf-8")
    assert "strategy_soft = panel.get(\"strategy_soft_fusion\") or {}" in src, \
        "T205 missing: synthesis should read panel.strategy_soft_fusion"
    assert "compute_exit_triggers(raw, dims_scored, {}, strategy_summary=strategy_summary)" in src, \
        "T205 missing: exit_triggers should consume strategy_summary"
    assert "default_buy_zones" in src and "strategy_net_bias" in src, \
        "T205 missing: default buy_zones should reference strategy bias"


def test_compute_friendly_strategy_trigger_enabled():
    src = (SCRIPTS_DIR / "compute_friendly.py").read_text(encoding="utf-8")
    assert "strategy_summary: dict | None = None" in src, \
        "compute_exit_triggers should accept strategy_summary"
    assert "策略层转空" in src or "策略共振失效" in src, \
        "exit triggers should include strategy-layer condition"
    assert "def compute_short_trading_module(" in src, \
        "short-term module missing: compute_friendly should provide compute_short_trading_module"
    assert "\"holding_window\": \"1-3 个交易日（严格执行 T+1 约束）\"" in src, \
        "short-term module should explicitly encode fast in/out holding window"


def test_stage2_synthesis_contains_short_trading_module():
    src = (SCRIPTS_DIR / "run_real_test.py").read_text(encoding="utf-8")
    assert "compute_short_trading_module(" in src, \
        "stage2 missing short-term module integration"
    assert "\"short_trading\": short_trading" in src, \
        "synthesis should expose short_trading for report rendering"


def test_report_template_and_renderer_have_short_trading_placeholder():
    tpl = (SCRIPTS_DIR.parent / "assets" / "report-template.html").read_text(encoding="utf-8")
    src = (SCRIPTS_DIR / "assemble_report.py").read_text(encoding="utf-8")
    assert "INJECT_SHORT_TRADING_MODULE" in tpl, \
        "template missing short-term module placeholder"
    assert "render_short_trading_module(syn, raw)" in src, \
        "assemble_report should render short-term module block"


def test_strategy_backtest_framework_exists():
    src = (SCRIPTS_DIR / "strategy_backtest.py").read_text(encoding="utf-8")
    assert "A-share constraint-aware strategy backtest framework" in src
    assert "\"t_plus_one\": True" in src, "T304 missing: T+1 constraint should be explicit"
    assert "\"limit_up_pct\": 9.5" in src and "\"limit_down_pct\": 9.5" in src, \
        "T304 missing: limit-up/limit-down constraints should exist"
    assert "\"slippage_bps\": 5.0" in src and "\"commission_bps\": 2.0" in src, \
        "T304 missing: cost/slippage constraints should exist"


def test_replay_merges_tradable_backtest_summary():
    src = (SCRIPTS_DIR / "strategy_replay.py").read_text(encoding="utf-8")
    assert "tradable_backtest" in src, "T303 missing: replay should expose tradable_backtest section"
    assert "--run-backtest" in src, "T303 missing: replay CLI should support running backtest inline"
    assert "_load_backtest_report" in src, "T303 missing: replay should load saved backtest report"


def test_strategy_engine_has_t305_t306_t307_t311_enhancements():
    src = (SCRIPTS_DIR / "lib" / "strategy_engine.py").read_text(encoding="utf-8")
    assert "def _adx14(" in src and "def _donchian_stats(" in src, \
        "T305 missing: trend enhancements should include ADX and Donchian helpers"
    assert "def _bollinger_stats(" in src and "def _kdj_stats(" in src and "def _cci20(" in src, \
        "T306 missing: reversal enhancements should include BOLL/KDJ/CCI helpers"
    assert "def _flow_indicator_proxies(" in src and "obv_proxy" in src and "mfi_proxy" in src, \
        "T307 missing: flow enhancements should expose OBV/MFI style proxies"
    assert "def _event_surprise_stats(" in src and "def _pead_proxy(" in src, \
        "T311 missing: event enhancements should include surprise and PEAD proxies"


def test_strategy_engine_has_t308_t309_t310_enhancements():
    src = (SCRIPTS_DIR / "lib" / "strategy_engine.py").read_text(encoding="utf-8")
    features_src = (SCRIPTS_DIR / "lib" / "stock_features.py").read_text(encoding="utf-8")
    fetch_src = (SCRIPTS_DIR / "fetch_financials.py").read_text(encoding="utf-8")

    assert "def _atr14(" in src and "def _ivol_stats(" in src and "def _vol_targeting_proxy(" in src, \
        "T308 missing: risk enhancement should include ATR/IVOL/vol-targeting helpers"
    assert "def _risk_parity_proxy(" in src and "risk_budget_score" in src, \
        "T308 missing: risk enhancement should include risk-parity proxy outputs"

    assert "earnings_yield" in src and "cf_to_price" in src and "ev_ebitda" in src, \
        "T309 missing: value enhancement should include E/P, CF/P, and EV/EBITDA factors"
    assert "is_state_owned" in src and "is_below_book" in src, \
        "T309 missing: value enhancement should include SOE and below-book repair tags"

    assert "receivable_turnover" in src and "inventory_turnover" in src and "asset_growth" in src, \
        "T310 missing: quality enhancement should include receivable/inventory/asset-growth factors"
    assert "gross_profitability" in src, \
        "T310 missing: quality enhancement should include gross profitability factor"

    assert "receivable_turnover" in features_src and "inventory_turnover" in features_src, \
        "T310 missing: stock_features should expose receivable/inventory turnover fields"
    assert "asset_growth" in features_src and "gross_profitability" in features_src, \
        "T310 missing: stock_features should expose asset growth and gross profitability fields"
    assert "extra_metric_map" in fetch_src and "receivable_turnover" in fetch_src and "inventory_turnover" in fetch_src, \
        "T310 missing: fetch_financials should try to pull receivable/inventory turnover metrics"


def test_strategy_engine_has_t312_t315_enhancements():
    src = (SCRIPTS_DIR / "lib" / "strategy_engine.py").read_text(encoding="utf-8")

    assert "def _limit_pattern_stats(" in src and "first_board_hits" in src and "t_board_hits" in src, \
        "T312 missing: limit-up ecology should include first/second board and T-board pattern stats"
    assert "height_cycle_score" in src and "first_yin_break_hits" in src, \
        "T312 missing: limit-up ecology should include break-board and height-cycle indicators"
    assert "def _strategy_limit_up(" in src and "涨停生态偏建设性" in src, \
        "T312 missing: strategy_limit_up should consume enhanced board-ecology evidence"

    assert "def _style_rotation_proxies(" in src and "growth_style_score" in src and "value_dividend_style_score" in src, \
        "T315 missing: rotation should include growth/value-dividend style scoring"
    assert "theme_diffusion_score" in src and "size_style" in src and "style_bias" in src, \
        "T315 missing: rotation should include theme diffusion and size/style regime tags"
    assert "风格轮动提供支撑" in src and "风格/主题轮动偏脆弱" in src, \
        "T315 missing: strategy_rotation should emit enhanced style/theme narratives"


def test_strategy_engine_has_t313_minute_intraday_enhancement():
    src = (SCRIPTS_DIR / "lib" / "strategy_engine.py").read_text(encoding="utf-8")
    kline_src = (SCRIPTS_DIR / "fetch_kline.py").read_text(encoding="utf-8")
    features_src = (SCRIPTS_DIR / "lib" / "stock_features.py").read_text(encoding="utf-8")

    assert "def fetch_intraday_minutes(" in kline_src and "def _intraday_micro_features(" in kline_src, \
        "T313 missing: fetch_kline should provide minute-bar fetch and micro-feature aggregation helpers"
    assert "intraday_minutes" in kline_src and "intraday_micro" in kline_src, \
        "T313 missing: kline dimension output should include intraday minute stream + micro summary"

    assert "open_15m_ret_pct" in features_src and "tail_30m_ret_pct" in features_src, \
        "T313 missing: stock_features should expose open/first-15m and tail-30m factors"
    assert "close_auction_volume_ratio" in features_src and "intraday_minute_available" in features_src, \
        "T313 missing: stock_features should expose close-auction volume ratio and minute availability flag"

    assert "open_auction_ret_pct" in src and "open_15m_ret_pct" in src and "tail_30m_ret_pct" in src, \
        "T313 missing: intraday strategy should consume minute-level open/15m/tail features"
    assert "close_auction_volume_ratio" in src and "intraday_minute_available" in src, \
        "T313 missing: intraday strategy should consume close-auction intensity and minute availability"


def test_strategy_engine_has_t314_calendar_long_stats_enhancement():
    src = (SCRIPTS_DIR / "lib" / "strategy_engine.py").read_text(encoding="utf-8")
    kline_src = (SCRIPTS_DIR / "fetch_kline.py").read_text(encoding="utf-8")
    features_src = (SCRIPTS_DIR / "lib" / "stock_features.py").read_text(encoding="utf-8")

    assert "def _seasonality_long_stats(" in kline_src and "seasonality_1y" in kline_src, \
        "T314 missing: fetch_kline should build long-window seasonality stats and expose seasonality_1y"
    assert "thursday_mean_return_pct" in kline_src and "spring_festival_pre_window_mean_return_pct" in kline_src, \
        "T314 missing: seasonality stats should include Thursday and spring-festival windows"

    assert "calendar_mode\": \"long_stats\"" in src and "thursday_bias_score" in src and "spring_live_bias" in src, \
        "T314 missing: calendar strategy should consume long-window weekday/month/spring-festival signals"
    assert "month_start_mean_return_pct" in src and "month_end_mean_return_pct" in src, \
        "T314 missing: calendar strategy should include month-start/month-end long statistics"

    assert "seasonality_sample_days" in features_src and "thursday_mean_return_pct_1y" in features_src, \
        "T314 missing: stock_features should expose long-window seasonality fields"


def test_strategy_engine_has_t316_pair_neutral_enhancement():
    src = (SCRIPTS_DIR / "lib" / "strategy_engine.py").read_text(encoding="utf-8")

    assert "def _beta_neutral_proxy(" in src and "def _etf_pair_hint(" in src, \
        "T316 missing: pair-neutral enhancement should include ETF leg hints and beta-neutral proxy helpers"
    assert "etf_pair_leg" in src and "etf_hedge_leg" in src and "beta_neutral_hedge_ratio" in src, \
        "T316 missing: pair strategy evidence should include ETF pair legs and hedge ratio"
    assert "pair_alpha_score" in src and "rel_mom_vs_industry_pct" in src, \
        "T316 missing: pair strategy should include industry-relative spread score"
    assert "行业配对价差、ETF 风格配对偏向与 beta 中性代理共同支持多头相对收益。" in src, \
        "T316 missing: pair strategy should emit upgraded narrative for neutral/pair framework"


def test_backtest_has_t317_walkforward_and_cost_sensitivity():
    src = (SCRIPTS_DIR / "strategy_backtest.py").read_text(encoding="utf-8")
    assert "def _slice_rows_by_date(" in src, \
        "T317 missing: backtest snapshots should slice dated rows to avoid lookahead leakage"
    assert "walk_forward" in src and "_walk_forward_summary" in src, \
        "T317 missing: backtest should export walk-forward summary"
    assert "cost_sensitivity" in src and "_cost_sensitivity" in src, \
        "T317 missing: backtest should export cost sensitivity summary"


def test_replay_exposes_walkforward_and_cost_sensitivity_summary():
    src = (SCRIPTS_DIR / "strategy_replay.py").read_text(encoding="utf-8")
    assert "walk_forward" in src, \
        "Replay should carry walk_forward summary from tradable backtest"
    assert "cost_sensitivity" in src, \
        "Replay should carry cost_sensitivity summary from tradable backtest"


def test_t320_etf_lof_basket_strategy_path_enabled():
    run_src = (SCRIPTS_DIR / "run_real_test.py").read_text(encoding="utf-8")
    basic_src = (SCRIPTS_DIR / "fetch_basic.py").read_text(encoding="utf-8")

    assert "def _build_basket_strategy_panel(" in run_src, \
        "T320 missing: stage1 should support ETF/LOF basket panel builder"
    assert "if sec_type in (\"etf\", \"lof\")" in run_src and "basket_mode" in run_src, \
        "T320 missing: stage1 should route ETF/LOF to basket_mode instead of early return"
    assert "panel_mode\": \"basket_strategy\"" in run_src, \
        "T320 missing: basket panel output should be explicitly tagged"
    assert "if sec_type in (\"etf\", \"lof\")" in basic_src and "strategy_mode\": \"basket\"" in basic_src, \
        "T320 missing: fetch_basic should no longer emit non_stock_security for ETF/LOF"
