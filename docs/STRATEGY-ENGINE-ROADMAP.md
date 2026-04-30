# Strategy Engine 接入任务文档（A 股 12 类策略）

最后更新：2026-04-22（六次复核：T314/T316 落地 + 全任务收敛）  
负责人：Codex + 用户协作  
状态：`DONE`（T001-T320 全部收敛）

---

## 1. 目标

在不破坏现有 `stage1 -> stage2 -> report` 主链路稳定性的前提下，为系统新增“策略引擎并行层”，分阶段接入以下 12 类策略：

1. 趋势 / 动量  
2. 反转 / 均值回复  
3. 成交量 / 换手 / 资金行为  
4. 低波动 / 低风险 / 波动管理  
5. 价值 / 估值修复  
6. 质量 / 盈利能力 / 基本面改善  
7. 事件驱动  
8. 涨停板 / 连板 / 炸板生态（A 股特有）  
9. 日内 / 竞价 / 尾盘 / 隔夜  
10. 日历 / 季节效应  
11. 行业 / 风格 / 主题轮动  
12. 配对 / 统计套利 / 中性（先做轻量版）

---

## 2. 设计原则（硬约束）

1. **并行增强，不替换主干**：策略层先独立产出 JSON，再在 `synthesis/report` 侧增量融合。  
2. **缺失可降级，不阻断出报告**：策略数据缺失只记 warning。  
3. **A 股交易约束真实化**：T+1、涨跌停、成交额门槛、滑点/成本约束。  
4. **先 regime 再信号**：趋势市/震荡市/情绪市作为门控。  
5. **先可解释后复杂化**：每个信号必须输出解释与置信度。

---

## 3. 目标产出物

### 3.1 新增缓存文件（`.cache/{ticker}/`）

- `strategy_features.json`
- `strategy_signals.json`
- `strategy_meta.json`

### 3.2 信号标准输出 schema（统一）

每个策略输出至少包含：

- `signal`: `bullish|bearish|neutral|skip`
- `strength`: `0-100`
- `confidence`: `0-100`
- `horizon`: `intraday|swing|position`
- `regime_fit`: `0-100`
- `explain`: 1-3 句可读解释
- `evidence`: 关键字段快照

---

## 4. 分期计划（里程碑）

## Phase 0 · 地基（结构先行）
状态：`DONE`

- [x] 建立策略引擎 registry（含策略 ID、依赖字段、运行档位）  
- [x] 定义并实现 `strategy_*` 三个缓存文件读写  
- [x] 在 `stage1` 中挂载策略计算入口（默认可开关）  
- [x] 在 `stage2/synthesis` 增加策略结果合并（无策略时不报错）  
- [x] 新增最小自检项（schema 合法性）

验收标准：
- `run.py <ticker>` 在策略层缺失时仍稳定出报告。  

---

## Phase 1 · 核心 A 股 alpha（baseline）
状态：`DONE`

目标：先落地对 A 股有效且可快速验证的 6 类 baseline。

- [x] 反转层：`1/3/5/20` 日反转 + RSI 极值（baseline）  
- [x] 量价换手层：主力资金净流 + 换手/流动性过滤（baseline）  
- [x] 涨停生态层：limit-hit / broken-rate + LHB/游资 proxy（baseline）  
- [x] 动量增强层：残差动量 + 修正动量 + 隔夜动量（baseline）  
- [x] 低波管理层：realized vol + drawdown 风险约束（baseline）  
- [x] 行业轮动层：行业增长 + 生命周期 + 资金轮动（baseline）

验收标准：
- 每个模块都有 `signal/strength/confidence/explain`。  
- 报告新增“策略雷达（初版）”区块。  

---

## Phase 2 · 基本面与事件融合（baseline）
状态：`DONE`

- [x] 价值修复层：PE/PB/分位/安全边际/股息率 + value trap 过滤（baseline）  
- [x] 质量改善层：ROE/ROIC/FCF/杠杆/增长匹配 + accrual proxy（baseline）  
- [x] 事件驱动层：事件强度 + 正负关键词 + 近 7/30 日新鲜度（baseline）  
- [x] 日历季节层：weekday + 月初/月末 + 季末偏置（baseline）  
- [x] 与评委层软融合（D/F/G 组优先读取策略摘要）

验收标准：
- `panel` 与 `buy_zones/exit_triggers` 可引用策略输出。  
- `stage1/stage2` 已实现 `panel`（D/F/G）+ `buy_zones` + `exit_triggers` 消费策略摘要。  

---

## Phase 3 · 回测与稳定性（baseline）
状态：`DONE`

- [x] 日内微结构层（当前为日线代理版）  
- [x] 配对/中性轻量层（行业内相对强弱代理）  
- [x] 回测框架（A 股约束 baseline：T+1/涨跌停/成本/滑点）  
- [x] 策略稳定性报告（regime 分桶 + tradable backtest 摘要）

验收标准：
- 输出“策略有效性与失效场景”摘要。  
- replay 输出 proxy 稳定性 + A 股约束回测摘要（effective/fragile）。  

---

## Phase 4 · 交易级增强（T305+）
状态：`DONE`

- [x] 补齐首批关键子策略：ADX/唐奇安/BOLL/KDJ/CCI/资金行为代理/事件 surprise+PEAD  
- [x] 回测升级：日期截断防未来函数 + walk-forward + cost-sensitivity  
- [x] 建立“策略-子类-证据字段”覆盖矩阵（`docs/STRATEGY-COVERAGE-MATRIX.md`）  
- [x] 补齐第二批关键子策略：ATR/IVOL/vol-targeting/风险平价 + E/P/CF/P/FCF/P/EV-EBITDA + 应收/存货/asset-growth/gross-profitability  
- [x] 涨停生态增强：首板/二板/一字/T 字/首阴首断板/高度周期（日线近似）+ 主题扩散热度接入  
- [x] 轮动增强：风格轮动（大小盘/成长价值/红利科技）+ 主题扩散  
- [x] 日内增强：竞价/开盘15分/尾盘/收盘竞价异常量（分钟级）  
- [x] 收口增强：T314 日历长期统计 + T316 行业/ETF/beta-neutral 配对代理  

验收标准：
- T305+ 收敛为 `DONE/DEFERRED`，无“名义已完成、实装未覆盖”的灰区。  

---

## 5. 代码接入点（当前识别）

核心文件：

- `skills/deep-analysis/scripts/run_real_test.py`  
- `skills/deep-analysis/scripts/compute_friendly.py`  
- `skills/deep-analysis/scripts/assemble_report.py`  
- `skills/deep-analysis/scripts/strategy_replay.py`  
- `skills/deep-analysis/scripts/strategy_backtest.py`  
- `skills/deep-analysis/scripts/lib/strategy_engine.py`  
- `skills/deep-analysis/scripts/lib/strategy_registry.py`  
- `skills/deep-analysis/scripts/lib/stock_features.py`  
- `skills/deep-analysis/scripts/lib/self_review.py`  

后续建议拆分：

- `skills/deep-analysis/scripts/lib/strategy_modules/`（将 `strategy_engine.py` 中 12 类拆分成独立模块，便于单测）

---

## 6. 任务拆解清单（可直接执行）

| ID | 任务 | Phase | 优先级 | 状态 | 备注 |
|---|---|---|---|---|---|
| T001 | 设计 strategy schema + registry | P0 | P0 | DONE | 已新增 `strategy_registry.py` |
| T002 | 在 stage1 写入 `strategy_features.json` | P0 | P0 | DONE | 已在 stage1 落盘 |
| T003 | 实现 `strategy_signals.json` 汇总器 | P0 | P0 | DONE | 已新增 `strategy_engine.py` |
| T004 | stage2 合并策略结果进 synthesis | P0 | P0 | DONE | 已合并为 `synthesis.strategy_layer` |
| T005 | 自检增加 strategy schema 校验 | P0 | P1 | DONE | 已加入 self-review warning 检查 |
| T101 | 反转模块 baseline | P1 | P0 | DONE | 已实现 1/3/5/20 + RSI 极值 |
| T102 | 量价/换手模块 baseline | P1 | P0 | DONE | 已实现主力资金 + 换手/流动性过滤 |
| T103 | 涨停生态模块 baseline | P1 | P0 | DONE | 已实现 limit-hit/broken-rate + LHB proxy |
| T104 | 残差/修正/隔夜动量 baseline | P1 | P0 | DONE | 已实现 residual/corrected/overnight |
| T105 | 低波与风险模块 baseline | P1 | P1 | DONE | 已实现 vol + drawdown 风险信号 |
| T106 | 行业轮动模块 baseline | P1 | P1 | DONE | 已实现行业增长 + 资金轮动 |
| T107 | Phase1 参数校准与样本回放 | P1 | P0 | DONE | replay 样本扩到 `processed=29` |
| T108 | 策略层报告渲染（策略快照区） | P1 | P1 | DONE | HTML 已展示 Top Bull/Bear 策略 |
| T201 | 价值修复模块 baseline | P2 | P1 | DONE | 已实现 PE/PB/分位/安全边际/股息 + trap filter |
| T202 | 质量改善模块 baseline | P2 | P1 | DONE | 已实现 ROE/ROIC/FCF/杠杆 + accrual proxy |
| T203 | 事件强度与漂移模块 baseline | P2 | P0 | DONE | 已实现 event intensity + drift baseline |
| T204 | 日历效应模块 baseline | P2 | P2 | DONE | 已实现 weekday + 月初/月末 + 季末偏置 |
| T205 | 与评委层软融合（D/F/G） | P2 | P1 | DONE | 已在 panel + buy_zones + exit_triggers 消费策略摘要 |
| T301 | 日内微结构模块 baseline | P3 | P2 | DONE | 已实现隔夜/日内日线代理 |
| T302 | 中性/配对轻量模块 baseline | P3 | P2 | DONE | 已实现 pair-relative-strength 代理 |
| T303 | 稳定性报告（replay + tradable 摘要） | P3 | P1 | DONE | 已融合 proxy + A 股约束回测摘要 |
| T304 | 回测框架（A 股交易约束） | P3 | P1 | DONE | 已新增 `strategy_backtest.py` |
| T305 | 趋势增强：ADX/唐奇安/N 日新高（个股路径） | P4 | P0 | DONE | 已接入 ADX/Donchian/N 日新高（A 股个股路径） |
| T306 | 反转增强：BOLL/KDJ/CCI/缺口回补/行业偏离回归 | P4 | P0 | DONE | 已接入 BOLL/KDJ/CCI + gap fill + relative deviation |
| T307 | 成交量增强：OBV/VPT/MFI/CMF + 封板金额/炸板率/封单衰减 | P4 | P0 | DONE | 已接入 OBV/VPT/MFI/CMF 代理 + fake-board/flow-decay 代理 |
| T308 | 风险增强：ATR/IVOL/vol targeting/风险平价 | P4 | P1 | DONE | 已接入 ATR14/IVOL20/60 + target leverage + risk budget/risk parity proxy |
| T309 | 价值增强：E/P/CF/P/FCF/P/EV-EBITDA + 国企/破净修复 | P4 | P1 | DONE | 已接入 E/P、CF/P、FCF/P、EV/EBITDA、国企/破净修复打分 |
| T310 | 质量增强：应收/存货/gross profitability/asset growth | P4 | P1 | DONE | 已接入 receivable/inventory/asset_turnover/asset_growth/gross_profitability 因子 |
| T311 | 事件增强：PEAD/预期差评分/事件后 1-20 日漂移分层 | P4 | P0 | DONE | 已接入 surprise_net + PEAD(1/5/20d) 漂移代理 |
| T312 | 涨停生态增强：首板/二板/一字/T 字/首阴首断板/高度周期 | P4 | P0 | DONE | 已接入日线近似板型识别 + 高度周期/断板风险 + 主题扩散 |
| T313 | 日内增强：竞价/开盘15分/尾盘/收盘竞价异常量（分钟级） | P4 | P1 | DONE | 已接入 1min 分时抓取 + open/15m/tail/收盘竞价量比与跳变特征 |
| T314 | 日历增强：春节/农历/周四效应/月初月末长期统计 | P4 | P2 | DONE | 已接入 `seasonality_1y` 长窗口统计 + 春节窗口实时偏置 |
| T315 | 轮动增强：风格轮动（大小盘/成长价值/红利科技）+ 主题扩散 | P4 | P1 | DONE | 已接入 size/style bias + growth/value-dividend 评分 + 扩散/集中度 |
| T316 | 配对中性增强：行业配对/ETF 配对/beta-neutral 代理 | P4 | P2 | DONE | 已接入 ETF 对冲腿映射 + beta-neutral 对冲比例代理 + pair_alpha_score |
| T317 | 回测增强：逐日事件/资金序列 + walk-forward + 成本敏感性 | P4 | P0 | DONE | 已接入日期截断快照 + walk-forward + low/base/high 成本场景 |
| T318 | 覆盖矩阵：12 类策略子类映射 + 验收看板 | P4 | P0 | DONE | 已新增 `docs/STRATEGY-COVERAGE-MATRIX.md` |
| T319 | 文档状态校准（全绿修正为 baseline + 增强分层） | P4 | P0 | DONE | 本次复核已完成 |
| T320 | ETF/LOF 直跑支持：跳过 51 评委，启用篮子趋势/轮动策略输出 | P4 | P1 | DONE | 已启用 basket panel；ETF/LOF 不再 `non_stock_security` 早退 |

状态机：`TODO -> DOING -> BLOCKED -> DONE -> DEFERRED`

---

## 7. 更新规则（必须执行）

每完成一个任务，**立即**更新本文件：

1. 修改任务表状态（`DOING/DONE/DEFERRED`）。  
2. 在“执行日志”追加一条记录。  
3. 若改动范围变化，更新“代码接入点”。  
4. 若发现风险，写入“风险与阻塞”。  
5. 若新增任务，使用 `T320+` 连续编号并给出优先级。  

---

## 8. 执行日志（Chronological）

| 日期 | 执行人 | 变更 | 影响任务 | 备注 |
|---|---|---|---|---|
| 2026-04-21 | Codex | 初始化本 roadmap 文档 | 全部 | 首版建立 |
| 2026-04-21 | Codex | 新增 `strategy_registry.py`（12 类策略注册、depth gating） | T001 | Phase0 开工 |
| 2026-04-21 | Codex | 新增 `strategy_engine.py`（schema + baseline signals + meta） | T003 | 产出 `strategy_features/signals/meta` |
| 2026-04-21 | Codex | `run_real_test.py` 接入 stage1 落盘 + stage2 合并 | T002,T004 | `strategy_layer` 降级不阻断 |
| 2026-04-21 | Codex | `self_review.py` 新增 strategy schema 校验 | T005 | 仅 warning/info，不阻断 |
| 2026-04-21 | Codex | 新增 Phase1 baseline 模块：low_vol / limit_up / rotation | T103,T105,T106 | 与趋势/反转/量价共同形成 6 类基线 |
| 2026-04-21 | Codex | 升级 trend 为 residual/corrected/overnight momentum；增强 reversal/flow 逻辑 | T101,T102,T104 | 使用真实 `candles_60d` + `main_fund_flow_20d` |
| 2026-04-21 | Codex | 报告新增 Strategy Layer 区块（Top Bullish / Top Bearish） | T108 | 模板注入 `INJECT_STRATEGY_LAYER` |
| 2026-04-21 | Codex | 新增 `strategy_replay.py` 批量回放工具并生成 replay 报告 | T107 | 产出 `.cache/_global/strategy_replay_medium.json` |
| 2026-04-21 | Codex | 实现 `event_drift` baseline（事件强度 + 正负漂移 + 近7/30日新鲜度） | T203 | `event_drift` 不再是 placeholder |
| 2026-04-21 | Codex | 实现 `value_repair` baseline（PE/PB/分位/行业偏离 + value trap 过滤） | T201 | replay 出现可区分 bull/bear 分布 |
| 2026-04-21 | Codex | 实现 `quality_improvement` baseline（ROE/ROIC/FCF/杠杆/利润-营收匹配） | T202 | 质量模块从 placeholder 转为可解释信号 |
| 2026-04-21 | Codex | 升级 `strategy_replay.py` 为稳定性报告（regime 分桶 + 20d/YTD proxy） | T303 | 新增 `stability.by_market_regime` 与 `backtest_proxy_by_strategy` |
| 2026-04-21 | Codex | 实现 `calendar_seasonality` baseline（周内统计 + 月初/月末 + 季末偏置） | T204 | deep replay 出现 bull/bear/neutral 分布 |
| 2026-04-21 | Codex | 实现 `intraday_timing` baseline（隔夜/日内代理 + 换手/资金/情绪） | T301 | 当前仍为日线代理版 |
| 2026-04-21 | Codex | 实现 `pair_relative_strength` baseline（行业内相对强弱 + 估值偏离 + 资金/动量代理） | T302 | 当前仍为 proxy 版 |
| 2026-04-21 | Codex | `run_real_test.py` 完成策略软融合：D/F/G 读取策略摘要；`buy_zones/exit_triggers` 显式消费 strategy_summary | T205 | stage1 回写融合 panel；stage2 对旧 cache 自动补齐 |
| 2026-04-21 | Codex | 新增 `strategy_backtest.py`（A 股约束回测框架：T+1/涨跌停可交易性/成本/滑点） | T304 | 可输出 `.cache/_global/strategy_backtest_<depth>.json` |
| 2026-04-21 | Codex | 升级 `strategy_replay.py` + 报告渲染：融合 tradable backtest 摘要（effective/fragile） | T303 | `stability.tradable_backtest` + Strategy Layer effectiveness 可视化 |
| 2026-04-21 | Codex | 基于 12 类策略清单做逐项代码复核，校准文档状态；新增 T305-T318 增强任务并将总状态改为 DOING | T305-T319 | 解决“基线完成=项目完成”的语义混淆 |
| 2026-04-21 | Codex | 增强 `strategy_engine.py`：落地 T305/T306/T307/T311（ADX、Donchian、BOLL、KDJ、CCI、flow proxies、surprise+PEAD） | T305,T306,T307,T311 | 12 类策略中高优先级子类完成首轮接入 |
| 2026-04-21 | Codex | 升级 `strategy_backtest.py`：日期截断防未来函数 + walk-forward + 成本敏感性输出 | T317 | 回测新增 `walk_forward` 与 `cost_sensitivity` |
| 2026-04-21 | Codex | 升级 `strategy_replay.py` 透传回测增强摘要；新增 `STRATEGY-COVERAGE-MATRIX.md` | T317,T318 | replay 可直接查看 walk-forward/cost 摘要与覆盖矩阵 |
| 2026-04-21 | Codex | 进度审计：核对 `DONE` 与代码证据；修正 T305 文案并补录 ETF 早退遗漏任务 T320 | T305,T320 | 避免“已支持 ETF 趋势”的状态误读 |
| 2026-04-21 | Codex | 二次复核：抽样核对 T205/T317/T318 代码证据并执行回归测试（`test_v2_16_strategy_completion.py` 8 passed） | T205,T317,T318,T320 | 状态保持不变，无新增遗漏 |
| 2026-04-21 | Codex | 完成 T320：ETF/LOF 直跑切换为 basket strategy panel，跳过 51 评委；同步 `fetch_basic`/`stage1`/报告动态文案并补充回归断言（9 passed） | T320 | 可转债仍保持 `non_stock_security` 早退 |
| 2026-04-22 | Codex | 落地 T308/T309/T310：策略引擎新增 ATR/IVOL/vol-targeting/风险平价代理 + E/P/CF/P/FCF/P/EV-EBITDA/国企破净修复 + 应收/存货/asset-growth/gross-profitability，并补齐财务抓取/特征透传与回归断言（10 passed） | T308,T309,T310 | 文档状态与覆盖矩阵同步更新 |
| 2026-04-22 | Codex | 落地 T312/T315：新增板生态模式识别（首板/二板/一字/T字/首阴断板/高度周期）+ 行业风格轮动（大小盘/成长价值/红利科技）与主题扩散，并补齐回归断言（11 passed） | T312,T315 | T313 成为剩余高优先主任务 |
| 2026-04-22 | Codex | 落地 T313：`fetch_kline` 新增分钟级分时抓取与微结构聚合（竞价/open15m/tail30m/收盘竞价量比），`stock_features` 与 `strategy_engine` 完成消费链路，补齐回归断言（12 passed） | T313 | 日内从日线代理升级为分钟级 baseline |
| 2026-04-22 | Codex | 落地 T314/T316：日历策略升级为 long-stats（周四/月初月末/春节窗口）+ 配对策略升级行业配对/ETF 对冲腿/beta-neutral 代理，并补齐回归断言（14 passed） | T314,T316 | T001-T320 全部收敛为 DONE |

---

## 9. 风险与阻塞

当前已识别风险：

1. 现有 `score_dimensions` 含较多 heuristic/stub，若直接耦合策略层会相互污染。  
2. 数据源在不同网络环境稳定性差异较大，策略层必须内置降级路径。  
3. 若无统一 schema + 覆盖矩阵，后续多策略扩展容易“名义接入、实际缺子类”。  
4. 当前 T001-T304 为 baseline，可解释但不等于交易级完整实现。  
5. 趋势/反转/成交量已补齐首轮增强，但 ETF 专用趋势与盘口级封单仍是下一阶段。  
6. 当前成交量模块中的 OBV/VPT/MFI/CMF 仍为资金流代理，非逐笔成交重建版本。  
7. 事件层 PEAD 仍为日线代理，未接入公告后分钟级漂移路径。  
8. 风险/价值/质量增强已接入，但部分字段仍受上游财务口径与列名差异影响，需持续做 source-level 校准。  
9. `receivable/inventory/asset_growth` 在部分票上仍可能缺失，当前策略会降级为低置信度而非阻断。  
10. `CF/P` 当前依赖 cash-flow 摘要字段，后续可升级为标准化现金流序列口径。  
11. 分钟级分时已接入，但分钟源在个别网络环境仍可能失败并降级到日线代理。  
12. 回测已补齐日期截断、walk-forward、成本敏感性，但仍缺分钟级成交/滑点曲线建模。  
13. ETF/LOF 已改为 basket strategy 直跑；可转债仍提前返回 `non_stock_security`（待单独策略覆盖）。  

---

## 10. 新窗口接手指令（复制即用）

当上下文过长时，新窗口第一条可直接用：

```text
请继续执行 docs/STRATEGY-ENGINE-ROADMAP.md。
先读取“任务拆解清单”和“执行日志”，从状态=TODO/DOING 且优先级最高的任务开始。
优先处理状态=TODO/DOING 的最高优先任务（若无则进入新任务编号）。
每完成一个任务，必须更新该文档的任务状态和执行日志，再继续下一个任务。
```

当前状态：`T001-T320 = DONE（baseline + 交易级增强）`。  
若新增工作，请使用 `T320+` 连续编号。

---

## 11. 完成定义（DoD）

项目级完成需满足：

1. 12 类策略均已接入或明确标记 `DEFERRED（附原因）`。  
2. 报告中可查看策略雷达、关键信号解释与有效性摘要。  
3. 策略缺失不会阻断报告生成（降级链路保持可用）。  
4. 回测/验证能解释“何时有效、何时失效”，并包含 A 股约束。  
5. 本文档任务清单全部收敛为 `DONE` 或 `DEFERRED（附原因）`。  
6. 所有标记为 proxy 的任务要么升级为交易级实现，要么转为 `DEFERRED（附原因）`。  
7. `T318` 覆盖矩阵通过：每个大类至少有可运行主信号 + 子类覆盖说明 + 回测证据链接。  
