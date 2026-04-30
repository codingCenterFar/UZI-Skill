# Papertrade P2 候选池与真实实时化任务文档

> 本文件是 `papertrade realtime/web` 的 P2 阶段继续任务表。  
> P0/P1 的实现与审计记录仍以 `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`、`docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`、`docs/PAPERTRADE-MODULE-REPORT.md` 为事实基线；本文件只承接下一阶段“候选池、实时行情、开收盘编排、非买入型机会状态”的增量改造。

## 1. 当前代码事实（2026-04-29）

1. `P0-1` 到 `P1-4` 已全部 `DONE`。
2. 最近聚焦回归：`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q` 结果为 `61 passed, 1 warning`。
3. SQLite 仍是唯一事实源：`.cache/paper_trade/paper.db`。
4. `run_cycle.py` 当前只处理调用方传入的 `--tickers`，不会自动生成候选股票池。
5. `run_realtime.py` 已支持显式 tickers、active candidate pool、watchlist 与持仓合并成 ticker universe，并输出 `pre_open/open_morning/lunch/open_afternoon/closing/after_close` 盘段计划；但真实节假日、集合竞价、复杂尾盘风控仍未接入。
6. `market_snapshot.py` 的实时行情覆盖只拉 `fetch_basic()` 快照，并覆盖 `raw.0_basic.price/change_pct`；策略信号、K 线、评委、synthesis 仍主要依赖最近一次 stage1/stage2 缓存。
7. `market_calendar_service.py` 当前是 MVP：工作日 + 上午/午休/下午/收盘后时段；尚未接真实节假日库、集合竞价、尾盘集合竞价、收盘撤单/复盘。
8. `watchlists` 表与 dashboard 已存在，但 watchlist 当前更像“持仓/历史信号聚合视图”，不是自动维护的候选池。
9. 神剑股份 `002361.SZ` 已完成一次全量深度闭环并接入 papertrade：`agent_reviewed=true`，最新 papertrade 信号 `action=AVOID`，`bonus_agent=4.0`，dashboard 已包含该票。
10. `P2-1` 已新增候选池批次/明细表、`candidate_pool.py` 写读归档原语、`GET/POST /api/v1/candidate-pool` 与归档 API。
11. `P2-2` 已新增候选池生成器 MVP：`candidate_generator.py` 可从 SQLite recent signals、`watchlists` 手工列表、本地 cache roots 生成候选批次；`POST /api/v1/candidate-pool/generate` 可触发生成；当前不访问实时网络，生成器本身不自动重建，但 `P2-5` 已让 `run_realtime.py` 可消费 active 候选池。
12. `P2-3` 已新增动作分层：`decision_policy.py` 现在区分 `PAPER_BUY_A / PAPER_WATCH_B / CANDIDATE_A / BREAKOUT_WAIT / PULLBACK_WAIT / NO_TRADE_AVOID / AVOID`；`AVOID` 仅用于硬性熊向，历史 SQLite 里旧 signal 的 `AVOID` 仍按历史事实保留。
13. `P2-4` 已新增批量行情快照事实层：`quote_snapshots` 表记录批次、ticker、source、quote_ts、quote_age_ms、失败原因与原始 payload；`quote_snapshots.py` 支持从显式 tickers、候选池和持仓合并刷新；API 增加 `POST /api/v1/quotes/refresh`、`GET /api/v1/quotes/latest`、`GET /api/v1/quotes/batch`。
14. `P2-5` 已让 `run_realtime.py` 支持不传 `--tickers` 时从 active candidate pool 或默认 watchlist 解析 ticker universe；也支持显式 tickers、候选池、watchlist、持仓合并去重，并新增 `--quote-only-between-full`，可在非 full-refresh 轮次只刷新 `quote_snapshots`。
15. `P2-6` 已新增盘段调度原语：`session_scheduler.py` 明确 `pre_open/open_morning/lunch/open_afternoon/closing/after_close` 动作；`run_realtime.py` 每轮输出 `session_plan`，仅在开盘/尾盘阶段触发 DAY 开盘撮合；收盘后会过期到期 DAY 队列、写入盘后复盘 outbox 事件，并可通过 `--after-close-rebuild-candidate-pool` 用本地事实源重建候选池。
16. `P2-7` 已新增 dashboard 候选池与新鲜度视图：read model/API 可返回 enriched candidate rows，dashboard HTML/payload 展示 Candidate Pool、bucket、action、trigger、quote status、analysis age、quote age、reasons 与 freshness flags。
17. `P2-8` 已新增 `p2_smoke.py` dry-run smoke：串联 dashboard summary、candidate pool、dashboard candidates、quote latest、runtime status、metrics、health 与 P2 schema/table count；smoke 会写入 `.cache/paper_trade/run_logs.jsonl`，用于记录真实 DB 当前候选池、quote snapshots、action states 与 schema/table 事实。
18. `P2-POST-1` 已修正候选生成器会把 `.cache/MOCK.SZ` 合成缓存带入真实候选池的问题；默认候选池已基于本地 facts 重建为 5 个真实 ticker（`002273.SZ`, `002361.SZ`, `002730.SZ`, `600519.SH`, `000533.SZ`），并写入受控 quote 失败快照用于 P2 dashboard/审计。真实 quote provider 本次阻塞，未写入伪造成功价格。
19. `P2-POST-2` 已给真实 quote provider 增加单票限时保护：`market_snapshot.fetch_realtime_snapshot()` 默认通过子进程调用 `fetch_basic()`，超时后返回 `QUOTE_PROVIDER_TIMEOUT` 失败快照；`quote_snapshots`、`POST /api/v1/quotes/refresh`、`run_realtime --quote-only-between-full` 和 `run_cycle --realtime-quote-overlay` 均可透传或读取 `quote_timeout_seconds`。最新真实库限时刷新批次为 `qbatch_timeout_smoke_c1d795bd907c4cb89573e549f7985d73`，5 个 active 候选均在 2 秒限时内超时失败，未写入伪造成功价格。

## 2. 当前暴露的问题

1. 动作分层、候选池展示和 dashboard 新鲜度视图已完成；真实 `.cache/paper_trade/paper.db` 当前已有 active `default` candidate pool 与 quote snapshot 审计记录；最新 active 候选 quote 刷新批次是 `fetch_basic_timeout` 失败快照，不是实时成功价格。
2. 自动股票池已接入 `run_realtime.py` 的 ticker universe 解析，但 `run_cycle.py` 直跑仍要求显式 `--tickers`；候选池生成器也尚未由 realtime loop 自动触发重建。
3. 实时性仍未完整闭环：系统已有 `quote_snapshots` 批量行情快照事实层，且 `run_realtime.py --quote-only-between-full` 可在非 full-refresh 轮次只刷行情快照；但 `--from-cache-only` 仍会依赖缓存，quote-only 和 `--quote-overlay` 都不会重算完整 K 线、策略信号、评委与 synthesis。
4. 开盘/闭盘逻辑仍是 MVP 但已有盘段调度：当前支持盘段动作日志、开盘/尾盘撮合、DAY 到期过期、盘后复盘 outbox 事件和可选盘后候选池重建；尚未接真实节假日库，盘后候选池重建默认不自动开启，也没有复杂尾盘风控。
5. dashboard 已有候选池优先级和新鲜度风险视图，但仍未做更细的交互筛选、触发价编辑或真实交易操作。

## 3. 不可变约束

1. SQLite 是唯一事实源，不引入 Kafka/Redis/MQ。
2. 不破坏已完成 P0/P1：intent、rule、lot、runtime lease、outbox、replay/rebuild、health、dashboard HTTP 包装必须保持兼容。
3. 原系统 22 维、51 评委、strategy_signals、synthesis、agent_analysis 仍是分析事实来源；P2 只做候选池、实时快照、调度和决策表现层增强。
4. 每完成一个任务，必须立即更新本文件的任务表与执行记录。
5. 发现文档与代码不一致，以代码事实为准，立即修正文档。
6. 不要 revert 用户或之前会话留下的无关改动。

## 4. P2 目标

把当前系统从“指定股票模拟盯盘”升级为“有候选池、有实时快照、有开收盘节奏、有等待触发状态的模拟交易工作台”。

验收口径：

1. 不传 `--tickers` 时，可以从候选池/默认 watchlist 中选出待评估标的。
2. dashboard 能显示候选池 Top N、候选原因、分数、动作状态、行情新鲜度、分析新鲜度。
3. `AVOID` 不再吞掉所有非买入机会；系统能区分“回避”和“等待回踩/等待突破/候选观察”。
4. 盘前、盘中、尾盘、收盘后有明确运行模式和日志记录。
5. 所有新状态、新快照、新候选记录都写入 SQLite，并可 replay/rebuild 或至少可审计。

## 5. P2 任务表

| ID | 优先级 | 任务 | 状态 | 验收标准 |
|---|---:|---|---|---|
| P2-1 | P0 | 候选池数据模型与 API 原语 | DONE | 新增 `candidate_pool` / `candidate_pool_items` 或等价表；可写入、读取、归档候选批次；API/read_model 可返回候选 Top N |
| P2-2 | P0 | 候选池生成器 MVP | DONE | 从现有缓存、recent signals、manual watchlist 生成候选池；输出 `candidate_score/reasons/source/staleness`；不依赖实时网络也可跑通 |
| P2-3 | P0 | 决策动作分层修正 | DONE | 增加非买入机会状态，如 `CANDIDATE_A`、`PULLBACK_WAIT`、`BREAKOUT_WAIT`、`NO_TRADE_AVOID`；`AVOID` 只保留给明确回避 |
| P2-4 | P0 | 批量实时行情快照层 | DONE | 新增 `quote_snapshots` 或等价表；支持批量刷新候选池/持仓/传入 tickers；记录 source、quote_ts、quote_age_ms、失败原因 |
| P2-5 | P1 | realtime loop 接入候选池 | DONE | `run_realtime.py` 可从候选池/watchlist 取 ticker universe；支持 full refresh 与 quote-only refresh 分离 |
| P2-6 | P1 | 盘前/盘中/尾盘/收盘后调度器 | DONE | 明确 `pre_open/open_morning/lunch/open_afternoon/closing/after_close` 动作；支持 DAY 订单收盘过期、盘后复盘和候选池重建 |
| P2-7 | P1 | dashboard 候选池与新鲜度视图 | DONE | 页面展示候选池、等待触发价、数据 age、quote age、候选原因；可区分持仓、观察、回避 |
| P2-8 | P1 | P2 回归与真实 smoke | DONE | 增加单测覆盖 candidate/quote/session/action；保留 `test_papertrade_*.py` 全绿；真实 DB smoke 记录写入执行日志 |

## 6. 下一步只做什么

`P2-1 候选池数据模型与 API 原语`、`P2-2 候选池生成器 MVP`、`P2-3 决策动作分层修正`、`P2-4 批量实时行情快照层`、`P2-5 realtime loop 接入候选池`、`P2-6 盘前/盘中/尾盘/收盘后调度器`、`P2-7 dashboard 候选池与新鲜度视图` 与 `P2-8 P2 回归与真实 smoke` 已完成。

P2 任务表当前全部 `DONE`。P2 收口后已执行真实库候选池/quote 审计数据灌入、合成 ticker 过滤修复和真实 quote provider 限时封装。本文件不自动新增 P3 任务；后续继续时应先按代码事实确认新范围，且仍不得跳到真实下单。

已完成文件范围（P2-1/P2-8）：

1. `skills/deep-analysis/scripts/papertrade/schema_v2.py`
2. `skills/deep-analysis/scripts/papertrade/candidate_pool.py`（新增）
3. `skills/deep-analysis/scripts/papertrade/candidate_generator.py`（新增）
4. `skills/deep-analysis/scripts/papertrade/decision_policy.py`
5. `skills/deep-analysis/scripts/papertrade/quote_snapshots.py`（新增）
6. `skills/deep-analysis/scripts/papertrade/session_scheduler.py`（新增）
7. `skills/deep-analysis/scripts/papertrade/market_calendar_service.py`
8. `skills/deep-analysis/scripts/papertrade/command_service.py`
9. `skills/deep-analysis/scripts/papertrade/watcher_persona.py`
10. `skills/deep-analysis/scripts/papertrade/dashboard_renderer.py`
11. `skills/deep-analysis/scripts/papertrade/notifier.py`
12. `skills/deep-analysis/scripts/papertrade/read_models.py`
13. `skills/deep-analysis/scripts/papertrade/api.py`
14. `skills/deep-analysis/scripts/papertrade/run_realtime.py`
15. `skills/deep-analysis/scripts/papertrade/p2_smoke.py`（新增）
16. `skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py`（新增）
17. `skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py`（新增）
18. `skills/deep-analysis/scripts/tests/test_papertrade_decision_actions.py`（新增）
19. `skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py`（新增）
20. `skills/deep-analysis/scripts/tests/test_papertrade_realtime_universe.py`（新增）
21. `skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py`（新增）
22. `skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`
23. `skills/deep-analysis/scripts/tests/test_papertrade_p2_smoke.py`（新增）
24. 本文件执行记录

P2-POST-2 已完成文件范围：

1. `skills/deep-analysis/scripts/papertrade/market_snapshot.py`
2. `skills/deep-analysis/scripts/papertrade/quote_snapshots.py`
3. `skills/deep-analysis/scripts/papertrade/api.py`
4. `skills/deep-analysis/scripts/papertrade/config.py`
5. `skills/deep-analysis/scripts/papertrade/config.example.json`
6. `skills/deep-analysis/scripts/papertrade/run_realtime.py`
7. `skills/deep-analysis/scripts/papertrade/run_cycle.py`
8. `skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py`
9. 本文件执行记录

P2-1/P2-8 已执行验收命令：

```bash
python3 -m py_compile \
  skills/deep-analysis/scripts/papertrade/schema_v2.py \
  skills/deep-analysis/scripts/papertrade/candidate_pool.py \
  skills/deep-analysis/scripts/papertrade/candidate_generator.py \
  skills/deep-analysis/scripts/papertrade/decision_policy.py \
  skills/deep-analysis/scripts/papertrade/quote_snapshots.py \
  skills/deep-analysis/scripts/papertrade/session_scheduler.py \
  skills/deep-analysis/scripts/papertrade/market_calendar_service.py \
  skills/deep-analysis/scripts/papertrade/command_service.py \
  skills/deep-analysis/scripts/papertrade/watcher_persona.py \
  skills/deep-analysis/scripts/papertrade/dashboard_renderer.py \
  skills/deep-analysis/scripts/papertrade/notifier.py \
  skills/deep-analysis/scripts/papertrade/read_models.py \
  skills/deep-analysis/scripts/papertrade/api.py \
  skills/deep-analysis/scripts/papertrade/run_realtime.py \
  skills/deep-analysis/scripts/papertrade/p2_smoke.py \
  skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py \
  skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py \
  skills/deep-analysis/scripts/tests/test_papertrade_decision_actions.py \
  skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py \
  skills/deep-analysis/scripts/tests/test_papertrade_realtime_universe.py \
  skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py \
  skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py \
  skills/deep-analysis/scripts/tests/test_papertrade_p2_smoke.py

python3 -m pytest \
  skills/deep-analysis/scripts/tests/test_papertrade_decision_actions.py \
  skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py \
  skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py \
  skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py \
  skills/deep-analysis/scripts/tests/test_papertrade_realtime_universe.py \
  skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py \
  skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py \
  skills/deep-analysis/scripts/tests/test_papertrade_p2_smoke.py \
  -q

python3 skills/deep-analysis/scripts/papertrade/p2_smoke.py \
  --db .cache/paper_trade/paper.db \
  --log .cache/paper_trade/run_logs.jsonl \
  --candidate-limit 20 \
  --pretty

python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q
```

## 7. 执行记录

### 2026-04-26

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-DOC-INIT`
- 变更文件：`docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：基于当前代码事实重建 P2 任务文档，明确 P0/P1 已完成但系统仍缺少候选池、真实实时快照、开收盘编排和非买入机会状态；新增 P2-1 到 P2-8 任务表，并指定下一步只做 `P2-1`
- 验证命令：文档生成，无代码验证
- 验证结果：待下一窗口从 `P2-1` 开始执行
- 风险/待办：P2 是新阶段，不应修改 P0/P1 的完成状态；每完成一个 P2 子任务必须即时追加本执行记录

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-1`
- 变更文件：`skills/deep-analysis/scripts/papertrade/schema_v2.py`, `skills/deep-analysis/scripts/papertrade/candidate_pool.py`, `skills/deep-analysis/scripts/papertrade/read_models.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增 `candidate_pool` 批次表与 `candidate_pool_items` 明细表；新增候选池 Python 原语，支持写入候选批次、读取最新 Top N、列出批次、归档批次；read model/API 增加候选池查询、写入和归档入口（`GET/POST /api/v1/candidate-pool`, `POST /api/v1/candidate-pool/archive`）；单测覆盖 ticker 规范化、Top N、归档后 active 不可读、API envelope 与 route 分发
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/schema_v2.py skills/deep-analysis/scripts/papertrade/candidate_pool.py skills/deep-analysis/scripts/papertrade/read_models.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；新增候选池单测 `2 passed, 1 warning`；papertrade 聚焦回归 `40 passed, 1 warning`
- 风险/待办：P2-1 只提供候选池存储与 API 原语，不做自动候选生成、全市场扫描、quote 重算、动作分层、dashboard 展示或真实下单；下一步按任务表进入 `P2-2`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-2`
- 变更文件：`skills/deep-analysis/scripts/papertrade/candidate_generator.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增候选池生成器 MVP，离线合并三类本地事实源：SQLite 最新 `signals`、`watchlists` 手工列表、本地 cache roots；为每个候选输出 `candidate_score`、`reasons`、`source_tags`、`staleness`、`analysis_age_ms`、`action_state`，并复用 P2-1 的 `create_candidate_pool_batch()` 写入 SQLite；API 增加 `POST /api/v1/candidate-pool/generate`，默认生成新 active 批次并归档同 pool 旧批次
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/candidate_generator.py skills/deep-analysis/scripts/papertrade/candidate_pool.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_pool.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；P2 候选池相关单测 `4 passed, 1 warning`；papertrade 聚焦回归 `42 passed, 1 warning`
- 风险/待办：P2-2 只生成候选池，不接入 `run_cycle.py/run_realtime.py` 的默认 ticker universe；不做全市场真实扫描、实时 quote 快照、决策阈值/动作分层、dashboard 候选池展示或真实下单；下一步按任务表进入 `P2-3`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-3`
- 变更文件：`skills/deep-analysis/scripts/papertrade/decision_policy.py`, `skills/deep-analysis/scripts/papertrade/watcher_persona.py`, `skills/deep-analysis/scripts/papertrade/candidate_generator.py`, `skills/deep-analysis/scripts/papertrade/dashboard_renderer.py`, `skills/deep-analysis/scripts/papertrade/notifier.py`, `skills/deep-analysis/scripts/tests/test_papertrade_decision_actions.py`, `skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增动作分层函数 `classify_action_layer()`，把非买入结果拆成 `CANDIDATE_A`、`BREAKOUT_WAIT`、`PULLBACK_WAIT`、`NO_TRADE_AVOID`，并保留 `AVOID` 只给硬性熊向；修正旧逻辑中“策略中性 + 评委偏多”也被当作方向冲突扣分的问题；watcher overlay、候选池生成器、dashboard badge 与 alert payload 同步支持新动作层；模拟成交链仍只对 `PAPER_BUY_A` 自动买入，对持仓强退出/硬性回避卖出
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/decision_policy.py skills/deep-analysis/scripts/papertrade/watcher_persona.py skills/deep-analysis/scripts/papertrade/candidate_generator.py skills/deep-analysis/scripts/papertrade/dashboard_renderer.py skills/deep-analysis/scripts/papertrade/notifier.py skills/deep-analysis/scripts/tests/test_papertrade_decision_actions.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_decision_actions.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；P2-3 相关单测 `6 passed, 1 warning`；papertrade 聚焦回归 `46 passed, 1 warning`
- 风险/待办：P2-3 只改变决策动作表达，不接批量 quote snapshot、不把候选池接入 runtime 默认 universe、不做 dashboard 候选池专区、不做真实下单；下一步按任务表进入 `P2-4`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-4`
- 变更文件：`skills/deep-analysis/scripts/papertrade/schema_v2.py`, `skills/deep-analysis/scripts/papertrade/quote_snapshots.py`, `skills/deep-analysis/scripts/papertrade/read_models.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增 `quote_snapshots` 批量行情快照表；新增 `quote_snapshots.py`，可合并显式 tickers、候选池 Top N 与持仓 ticker universe 后批量刷新行情快照，逐 ticker 记录 `source`、`quote_ts_ms`、`quote_age_ms`、状态、失败代码/原因、原始 payload 与请求上下文；read model/API 增加 `POST /api/v1/quotes/refresh`、`GET /api/v1/quotes/latest`、`GET /api/v1/quotes/batch`，并提供 `quote-snapshots` 别名路径；单测使用 fake provider 覆盖成功与失败快照、候选池/持仓/显式 ticker 合并、API envelope 与路由分发
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/schema_v2.py skills/deep-analysis/scripts/papertrade/quote_snapshots.py skills/deep-analysis/scripts/papertrade/read_models.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；P2-4 新增单测 `2 passed, 1 warning`；papertrade 聚焦回归 `48 passed, 1 warning`
- 风险/待办：P2-4 只落地批量 quote snapshot 事实层，不做全市场扫描、不重算 K 线/策略/评委/synthesis、不接真实下单；`run_cycle.py/run_realtime.py` 尚未自动消费候选池与 `quote_snapshots`，下一步按任务表进入 `P2-5`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-5`
- 变更文件：`skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/tests/test_papertrade_realtime_universe.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：`run_realtime.py --tickers` 改为可选；新增 realtime ticker universe 解析原语，可按“显式 tickers → active candidate pool/default pool → watchlist/default watchlist → 持仓”合并去重，并把来源明细写入 session config 与每轮 JSON 输出；新增 `--candidate-pool-name`、`--candidate-batch-id`、`--candidate-limit`、`--watchlist-id`、`--watchlist-limit`、`--include-positions`；新增 `--quote-only-between-full`，非 full-refresh 轮次可只写入 `quote_snapshots` 批量行情快照，不跑缓存决策循环；修正 `refresh_every_loops=1` 时应每轮 full-refresh 的边界判断
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/papertrade/quote_snapshots.py skills/deep-analysis/scripts/papertrade/candidate_pool.py skills/deep-analysis/scripts/papertrade/read_models.py skills/deep-analysis/scripts/tests/test_papertrade_realtime_universe.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_realtime_universe.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；P2-5 新增单测 `4 passed, 1 warning`；papertrade 聚焦回归 `52 passed, 1 warning`
- 风险/待办：P2-5 只接入 realtime ticker universe 与 quote-only/full-refresh 分流；不做全市场真实扫描、不自动重建候选池、不重算完整 K 线/策略/评委/synthesis、不引入 Web 框架、不做真实下单；下一步按任务表进入 `P2-6`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-6`
- 变更文件：`skills/deep-analysis/scripts/papertrade/market_calendar_service.py`, `skills/deep-analysis/scripts/papertrade/command_service.py`, `skills/deep-analysis/scripts/papertrade/session_scheduler.py`, `skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增 `session_scheduler.py`，把 market clock 映射为 `pre_open/open_morning/lunch/open_afternoon/closing/after_close` 调度相位，并输出每相位动作清单；`run_realtime.py` 每轮写出 `session_plan`，只在开盘/尾盘相位触发 queued DAY 撮合，收盘后执行到期 DAY 订单过期与盘后复盘 outbox 记录；`command_service.py` 为盘外 queued DAY intent 写入下一交易日或当日收盘 `expires_at_ms`；新增可选 `--after-close-rebuild-candidate-pool`，收盘后可用 `candidate_generator.py` 的本地事实源重建候选池，不做全市场扫描
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/market_calendar_service.py skills/deep-analysis/scripts/papertrade/command_service.py skills/deep-analysis/scripts/papertrade/session_scheduler.py skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_opening_matcher.py skills/deep-analysis/scripts/tests/test_papertrade_session_scheduler.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；P2-6 新增单测 `4 passed, 1 warning`；开盘撮合 + 调度测试 `7 passed, 1 warning`；papertrade 聚焦回归 `56 passed, 1 warning`
- 风险/待办：P2-6 仍使用 MVP 工作日/时段日历，不接真实节假日库；盘后候选池重建需显式打开 `--after-close-rebuild-candidate-pool`；不做复杂尾盘风控、dashboard 候选池专区、Web 框架或真实下单；下一步按任务表进入 `P2-7`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-7`
- 变更文件：`skills/deep-analysis/scripts/papertrade/read_models.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/papertrade/dashboard_renderer.py`, `skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增 dashboard candidate read model，把 active candidate pool、当前持仓和最新 `quote_snapshots` 合成为 enriched candidate rows；每行输出 `bucket`（POSITION/WAIT/AVOID/OBSERVE/BUY_READY）、`action_state`、candidate score、trigger price、quote status、quote price/change、analysis/quote age、reasons 与 freshness flags；API 增加 `GET /api/v1/dashboard/candidates`；dashboard payload/HTML 增加 `Candidate Pool` 区块与候选/等待/回避/行情风险摘要
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/read_models.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/papertrade/dashboard_renderer.py skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；dashboard renderer 单测 `5 passed, 1 warning`；papertrade 聚焦回归 `57 passed, 1 warning`
- 风险/待办：P2-7 只做 dashboard/read_model 视图，不做触发价编辑、交互筛选、Web 框架、真实下单或全市场扫描；下一步按任务表进入 `P2-8`

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-8`
- 变更文件：`skills/deep-analysis/scripts/papertrade/p2_smoke.py`, `skills/deep-analysis/scripts/tests/test_papertrade_p2_smoke.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`
- 关键变更：新增 P2 dry-run smoke 入口，串联 dashboard summary、candidate pool、dashboard candidates、quote latest、runtime status、metrics、health 与 P2 schema/table count；smoke 不提交 intent、不撮合订单、不刷新真实网络行情、不重建候选池，只读取当前 SQLite/read model 并按需向 `run_logs.jsonl` 追加 `kind=p2_smoke` 审计记录；新增单测覆盖 candidate/quote/session/action 四类 P2 表面，并覆盖空 candidate pool 在 smoke 中作为 warning 而非失败
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/p2_smoke.py skills/deep-analysis/scripts/tests/test_papertrade_p2_smoke.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_p2_smoke.py -q`；`python3 skills/deep-analysis/scripts/papertrade/p2_smoke.py --db .cache/paper_trade/paper.db --log .cache/paper_trade/run_logs.jsonl --candidate-limit 20 --pretty`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`
- 验证结果：编译通过；P2 smoke 单测 `2 passed, 1 warning`；真实 DB smoke `ok=true`，P2 schema/table 齐全并已写入 `.cache/paper_trade/run_logs.jsonl`，同时记录当时真实库 `candidate_pool=0`、`candidate_pool_items=0`、`quote_snapshots=0`、`watchlists=0`、`signals=19` 且仅观察到历史 `AVOID`；papertrade 聚焦回归 `59 passed, 1 warning`
- 风险/待办：P2-8 是回归与 smoke 收口，不新增真实候选扫描、不改变交易阈值、不引入 Web 框架、不做真实下单；真实库没有 active candidate pool/quote snapshots 是当前事实，后续若要看到候选池 dashboard，需要先执行候选池生成/quote refresh 或打开既有盘后候选池重建

### 2026-04-29

- 时间：2026-04-29
- 完成项（对应 ID）：`P2-POST-1`
- 变更文件：`skills/deep-analysis/scripts/papertrade/candidate_generator.py`, `skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`, `.cache/paper_trade/paper.db`, `.cache/paper_trade/run_logs.jsonl`, `.cache/paper_trade/dashboard.html`
- 关键变更：按真实库 smoke 结果继续收口，修正候选池生成器会把 `.cache/MOCK.SZ` 合成缓存带入默认候选池的问题；新增窄过滤，只排除 `MOCK/TEST/DUMMY` 这类合成 ticker；重建项目根真实 `.cache/paper_trade/paper.db` 的 active `default` candidate pool，当前 dashboard candidate read model 只返回 5 个真实 ticker：`002273.SZ`, `002361.SZ`, `002730.SZ`, `600519.SH`, `000533.SZ`；真实 quote provider 本次阻塞，因此未写伪造成功价格，改写 `controlled_dry_run_no_network` 失败 quote snapshots 作为可审计的新鲜度风险事实，并重新导出 dashboard HTML
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/candidate_generator.py skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_candidate_generator.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`；`python3 skills/deep-analysis/scripts/papertrade/p2_smoke.py --db .cache/paper_trade/paper.db --log .cache/paper_trade/run_logs.jsonl --candidate-limit 20 --pretty`；`python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py --db .cache/paper_trade/paper.db --output .cache/paper_trade/dashboard.html --title "Papertrade Realtime Desk"`；dashboard candidate read model 复核命令
- 验证结果：编译通过；候选生成器单测 `3 passed, 1 warning`；papertrade 聚焦回归 `60 passed, 1 warning`；真实 DB smoke `ok=true`，最新 smoke id `p2smoke_52761fd6a0674dd6ae2f8cfcb6048f5a`，active candidate pool `item_count=5`，dashboard buckets 为 `OBSERVE=1`、`AVOID=2`、`WAIT=2`；dashboard candidate read model 复核确认 active pool 不含 `MOCK.SZ`
- 风险/待办：quote snapshot 当前是受控失败快照，不是实时成功行情；`quote_latest` 仍会看到早前历史批次里的旧合成 ticker 快照，但 active dashboard candidate list 已不会展示 `MOCK.SZ`；后续若要真实价格，需要修复/限时封装真实 quote provider，避免再次被外部行情源卡住

- 时间：2026-04-29
- 完成项（对应 ID）：`P2-POST-2`
- 变更文件：`skills/deep-analysis/scripts/papertrade/market_snapshot.py`, `skills/deep-analysis/scripts/papertrade/quote_snapshots.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/papertrade/config.py`, `skills/deep-analysis/scripts/papertrade/config.example.json`, `skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py`, `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`, `.cache/paper_trade/paper.db`, `.cache/paper_trade/run_logs.jsonl`, `.cache/paper_trade/dashboard.html`
- 关键变更：给真实 quote provider 增加单票限时保护，默认 `fetch_realtime_snapshot()` 通过子进程调用 `fetch_basic()`，超时后返回 `QUOTE_PROVIDER_TIMEOUT` 失败快照，避免单只股票阻塞整个 `quote_snapshots` 批次；`refresh_quote_snapshots()`、`POST /api/v1/quotes/refresh`、`run_realtime --quote-only-between-full` 与 `run_cycle --realtime-quote-overlay` 均可透传或读取 `quote_timeout_seconds`；配置示例新增 `realtime.quote_timeout_seconds`
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/quote_snapshots.py skills/deep-analysis/scripts/papertrade/market_snapshot.py skills/deep-analysis/scripts/papertrade/p2_smoke.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/papertrade/config.py skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/papertrade/run_cycle.py skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_quote_snapshots.py -q`；`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`；真实库 `POST /api/v1/quotes/refresh` 等价调用（`pool_name=default`, `candidate_limit=5`, `provider_timeout_seconds=2.0`）；`python3 skills/deep-analysis/scripts/papertrade/p2_smoke.py --db .cache/paper_trade/paper.db --log .cache/paper_trade/run_logs.jsonl --candidate-limit 20 --pretty`；`python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py --db .cache/paper_trade/paper.db --output .cache/paper_trade/dashboard.html --title "Papertrade Realtime Desk"`
- 验证结果：编译通过；quote snapshot 单测 `3 passed, 1 warning`；papertrade 聚焦回归 `61 passed, 1 warning`；真实库限时 quote refresh `ok=true`，批次 `qbatch_timeout_smoke_c1d795bd907c4cb89573e549f7985d73`，`ticker_count=5`、`ok_count=0`、`failed_count=5`，5 个 active 候选均落 `QUOTE_PROVIDER_TIMEOUT`，未写入伪造价格；真实 DB smoke `ok=true`，最新 smoke id `p2smoke_5fc9236e7bd44e81b0e62d1cdd657920`，active candidate pool `item_count=5`，dashboard buckets 为 `OBSERVE=1`、`AVOID=2`、`WAIT=2`；dashboard HTML 已重新导出
- 风险/待办：当前真实行情源仍未返回成功价格，只是被限时失败快照稳定收敛；`quote_latest` 仍会看到早前历史批次里的旧合成 ticker 快照，但 active dashboard candidate list 不展示 `MOCK.SZ`；后续若要提高成功率，应在不做全市场扫描、不引入 Web 框架、不接真实下单的前提下，增加更轻量的 A 股 quote-only provider 或配置 `MX_APIKEY`
