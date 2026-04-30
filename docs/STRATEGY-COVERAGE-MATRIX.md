# Strategy Coverage Matrix（A 股 12 类）

最后更新：2026-04-22（六次复核）

## 状态定义

- `DONE`: 已在策略引擎中实现并可输出信号。
- `BASELINE`: 已有基础实现，后续可继续细化参数。
- `TODO`: 尚未接入或仅有占位逻辑。

## 覆盖矩阵

| 类别 | 当前状态 | 已落地子策略 | 主要证据字段 | 回测关注点 |
|---|---|---|---|---|
| 1) 趋势/动量 | DONE（A 股个股路径，BASELINE+） | 残差动量、修正动量、隔夜动量、ADX、唐奇安突破、N 日新高 | `residual_proxy_20d_pct`, `corrected_mom_20d_pct`, `adx14`, `donchian_breakout_up` | 趋势市与高波动市分桶稳定性 |
| 2) 反转/均值回复 | DONE（BASELINE+） | 1/3/5/20 日反转、RSI、BOLL zscore、KDJ、CCI、缺口回补率、行业偏离回归 | `ret_1d_pct`, `boll_zscore`, `kdj_j`, `cci20`, `down_gap_fill_rate_pct` | 震荡市胜率、过热区回撤控制 |
| 3) 成交量/换手/资金行为 | DONE（BASELINE+） | 主力资金净流、流动性过滤、OBV/VPT/MFI/CMF 代理、资金冲击 zscore、炸板风险代理 | `main_fund_20d_net_yi`, `obv_proxy`, `mfi_proxy`, `cmf_proxy`, `fake_board_rate_pct` | 高换手样本下收益衰减与滑点敏感性 |
| 4) 低波/风险管理 | DONE（BASELINE+） | realized volatility、max drawdown、ATR14、IVOL20/60、vol-target leverage、risk-parity budget proxy | `volatility_1y_pct`, `atr14_pct`, `ivol_20d_daily_pct`, `target_leverage`, `risk_budget_score` | 高波动 regime 回撤控制 |
| 5) 价值/估值修复 | DONE（BASELINE+） | PE/PB/分位/行业偏离/安全边际/股息 + E/P、CF/P、FCF/P、EV/EBITDA、SOE/破净修复 | `earnings_yield_pct`, `cf_to_price_pct`, `fcf_to_price_pct`, `ev_ebitda`, `is_state_owned` | 价值风格周期切换下稳定性 |
| 6) 质量/基本面改善 | DONE（BASELINE+） | ROE/ROIC/FCF/杠杆/增长匹配 + receivable/inventory turnover、asset growth、gross profitability | `receivable_turnover`, `inventory_turnover`, `asset_growth_pct`, `gross_profitability`, `accrual_proxy_pct` | 财报季前后信号漂移 |
| 7) 事件驱动 | DONE（BASELINE+） | 事件强度、正负关键词、surprise 净分、PEAD 1/5/20 日漂移代理 | `event_intensity`, `surprise_net`, `pead_drift_5d_pct` | 事件后 1-20 日持续性 |
| 8) 涨停/连板生态 | DONE（BASELINE+） | limit-hit、broken-rate、首板/二板/三板+、一字/T字、首阴首断板、高度周期 + 主题扩散 | `first_board_hits`, `second_board_hits`, `one_word_board_hits`, `t_board_hits`, `height_cycle_score` | 强情绪窗口失效率 |
| 9) 日内/竞价/尾盘/隔夜 | DONE（BASELINE+） | 隔夜/日内拆分代理 + 分钟级竞价/open15m/tail30m/收盘竞价量比与跳变特征 | `open_auction_ret_pct`, `open_15m_ret_pct`, `tail_30m_ret_pct`, `close_auction_volume_ratio`, `close_auction_jump_pct` | 次日跟随质量 |
| 10) 日历/季节效应 | DONE（BASELINE+） | 长窗口周内统计 + 周四效应 + 月初/月末 + 春节窗口偏置（含 fallback） | `seasonality_sample_days`, `thursday_mean_return_pct`, `month_start_mean_return_pct`, `spring_festival_window_mean_return_pct`, `seasonality_score` | 样本充分性与过拟合风险 |
| 11) 行业/风格/主题轮动 | DONE（BASELINE+） | 行业增长/生命周期 + 大小盘风格 + 成长价值/红利科技评分 + 主题扩散/集中度 | `size_style`, `style_bias`, `growth_style_score`, `value_dividend_style_score`, `theme_diffusion_score` | 行业切换滞后 |
| 12) 配对/中性 | DONE（BASELINE+） | 行业估值+动量配对、ETF 对冲腿映射、beta-neutral 对冲比例代理 | `rel_mom_vs_industry_pct`, `etf_pair_leg`, `etf_hedge_leg`, `beta_neutral_hedge_ratio`, `pair_alpha_score` | proxy 模式下信噪比 |

## 回测验收看板（当前）

- 回测引擎：`skills/deep-analysis/scripts/strategy_backtest.py`
- 交易约束：T+1、涨跌停可交易性、成本/滑点
- 新增：
  - walk-forward 摘要（按策略时序稳定性）
  - cost-sensitivity 摘要（low/base/high 交易成本场景）
  - 日期截断快照（资金流/事件/LHB 序列防未来函数）

## 下一步（仍建议）

- 涨停生态升级到盘口级封单金额/回封次数/分时炸板质量。
- 可转债路径（仍保留 `non_stock_security` 早退）需单独策略入口。

## 五次复核结论（2026-04-22）

- 覆盖矩阵状态与代码实现保持一致，`DONE(BASELINE/BASELINE+)` 无新增冲突项。
- 已登记增强任务 `T314/T316` 已完成并回填测试。
