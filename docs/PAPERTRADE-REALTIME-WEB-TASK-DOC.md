# Papertrade 实时盯盘 Web 系统 · 技术设计与执行文档 v2

更新时间：2026-04-23  
状态：`APPROVED`（用户已确认）

---

## 0. 使用约定（本文件即唯一任务文档）

1. 本文档是本项目的执行基线，后续以此为准。
2. 每完成一个任务项，必须先更新本文件的“执行状态表 + 实时执行记录”，再对外汇报。
3. 不推翻 MVP，只做渐进式改造，优先正确性、一致性、可审计、可解释。

---

## 1. 项目边界

### 1.1 项目定位

- 单用户
- 本地版
- A 股模拟盘
- MVP 优先

### 1.2 强约束

1. 不引入真实券商 API。
2. 不做多用户权限系统。
3. 不做高频撮合与完整 OMS。
4. 不引入 Kafka/Redis/MQ 等重型中间件。
5. 优先复用现有 `papertrade` 结构。

---

## 2. v2 修订目标（在现有 MVP 上补齐工程约束）

### 2.1 需要解决的核心风险

1. 错账：自动与手工路径并发改账、缺少统一命令状态机。
2. 重复执行：UI 重复点击或请求重试造成重复落单。
3. 解释不一致：历史决策回放时解释漂移。
4. 状态漂移：日志与数据库双事实源导致不一致。
5. 运行冲突：无单活锁时可能双 loop 同时改账。

### 2.2 v2 总体设计原则

1. SQLite 是唯一事实源（SoT）。
2. JSONL 仅用于导出、调试、回放辅助。
3. auto/manual 统一进入 `intent` 命令层。
4. 规则服务统一语义：API/loop/UI 共用同一套校验与拒单码。
5. 决策与规则命中必须快照化、版本化。
6. 事件采用 Outbox，确保状态变更与事件发布可对齐。
7. 实时 loop 单活写入，杜绝并发改账。

---

## 3. 修订后的架构设计 v2

```text
行情快照(fetch_basic) + 分析输出(stage1/stage2/cache)
                │
                ▼
      runtime_coordinator (run_realtime)
                │
    decision_engine (evaluate + watcher)
                │
                ▼
     intent_command_service (auto/manual统一)
                │
      rule_engine + market_calendar_service
                │
                ▼
        execution_simulator + lot allocator
                │
                ▼
SQLite(source of truth): intents/orders/fills/lots/nav/snapshots/outbox
                │
      outbox_dispatcher -> SSE(前端实时)
                │
                └── JSONL(调试/导出/回放辅助)
```

---

## 4. P0 / P1 范围

## 4.1 P0（必须先做）

1. 统一 `intent/order` 状态机 + 幂等键。
2. 中心化 A 股规则服务（时钟+元数据+规则引擎）。
3. realtime loop 事务边界与失败恢复语义。
4. SQLite/JSONL 角色收敛 + `event_outbox`。
5. 决策/规则/watcher 版本化快照。
6. cached + overlay 一致性与 staleness 暴露。
7. runtime 单活锁（lease + heartbeat）。
8. lot 级持仓（支持 T+1、分批买卖、可卖数量）。
9. API 读写分层。
10. 可观测性核心指标落地。

## 4.2 P1（延后）

1. 盘外挂单队列与开盘撮合（P0 可先拒单）。
2. 更复杂部分成交模型。
3. replay/rebuild 工具化增强。
4. 更高级可视化与策略参数面板。

---

## 5. 统一状态机设计（P0）

## 5.1 核心实体

1. `signal`：建议层，不直接改账本。
2. `order_intent`：命令层，统一 auto/manual/system_replay。
3. `order/fill`：执行层，模拟成交事实。

## 5.2 状态转移

| From | To | 触发 | 说明 |
|---|---|---|---|
| `signal` | `intent` | auto/manual 创建命令 | 进入统一命令层 |
| `intent` | `validated` | 规则通过 | 记录 `rule_check_snapshot_id` |
| `intent` | `rejected` | 规则拒绝 | 记录稳定拒单码/原因 |
| `validated` | `queued` | 进入执行队列 | 等待模拟执行 |
| `queued` | `filled` | 全量成交 | 终态 |
| `queued` | `partial` | 部分成交 | 可转 filled/cancelled/expired |
| `queued` | `cancelled` | 人工取消/系统取消 | 终态 |
| `queued` | `expired` | 超时 | 终态 |
| `partial` | `filled` | 余量成交完成 | 终态 |
| `partial` | `cancelled` | 取消余量 | 终态 |
| `partial` | `expired` | 余量超时 | 终态 |

## 5.3 幂等键

1. manual：前端必须传 `idempotency_key`。
2. auto：`auto:{loop_id}:{ticker}:{action}:{decision_snapshot_id}`。
3. replay：`replay:{replay_id}:{original_intent_id}`。
4. DB 约束：`UNIQUE(source, idempotency_key)`。

---

## 6. A 股规则服务（P0）

## 6.1 组件拆分

1. `market_calendar_service`：交易日、节假日、时段（上午/午休/下午）。
2. `instrument_metadata_provider`：主板/创业板/科创板/ST 属性与涨跌幅档。
3. `rule_engine`：统一执行所有规则并输出稳定结果。

## 6.2 统一校验项

1. 交易日与交易时段。
2. 是否允许盘外挂单（MVP：先拒单）。
3. 涨跌停（主板±10%、创业板/科创板±20%、ST±5%）。
4. T+1 可卖数量。
5. 买入手数（100 股整数倍）。
6. 禁做空。
7. 现金与持仓约束。

## 6.3 输出规范

- `allow: bool`
- `reject_code: string`（稳定枚举）
- `reject_reason: string`（可展示）
- `checks_json`（命中明细，可审计）

---

## 7. realtime loop 事务边界（P0）

单轮拆分为 7 个阶段：

1. 读取行情快照（事务外）。
2. 读取/生成分析快照（事务外）。
3. 产出 decision set（事务外）。
4. 产出 execution intents（事务内开始）。
5. 执行模拟成交（事务内）。
6. 写数据库（事务内，含 nav/positions/lots/outbox）。
7. 发事件流（事务后，由 dispatcher 异步）。

关键关联键：

- `loop_id`
- `snapshot_ts`
- `decision_version`
- `event_batch_id`
- `correlation_id`

失败语义：

1. 事务内失败：整单回滚，不产生半状态。
2. 事务后事件失败：由 outbox 重试，不影响账本事实。
3. 单票失败可跳过，整轮状态记 `partial_failed`。

---

## 8. 数据一致性与事件流（P0）

## 8.1 事实源角色

1. `SQLite`：唯一事实源。
2. `JSONL`：调试/导出/回放辅助，不参与业务判定。

## 8.2 Outbox 设计

- 所有状态变更同事务插入 `event_outbox`。
- dispatcher 轮询 `pending` 事件发布 SSE。
- 发布成功置 `published`，失败记录 `last_error + attempts`。

统一事件字段：

1. `event_id`
2. `event_type`
3. `entity_type`
4. `entity_id`
5. `causation_id`
6. `correlation_id`
7. `event_batch_id`
8. `payload_version`
9. `payload`
10. `created_at/published_at`

---

## 9. 快照版本化（P0）

新增快照层：

1. `analysis_snapshot`
2. `decision_snapshot`
3. `watcher_overlay_snapshot`
4. `rule_check_snapshot`

要求：

1. 每笔 intent 必须指向 `decision_snapshot_id`。
2. 每次校验必须产出 `rule_check_snapshot_id`。
3. 历史回放只读快照，不重算解释。

---

## 10. cached + overlay 一致性规范（P0）

## 10.1 允许 overlay 字段

1. `0_basic.price`
2. `0_basic.change_pct`
3. `quote_ts/source`

## 10.2 禁止 overlay 字段

1. panel 评分与投票
2. strategy_signals 结果
3. synthesis 解释字段
4. watcher 输入中除实时价外的历史特征

## 10.3 必须暴露的时效信息

1. `analysis_age_ms`
2. `quote_age_ms`
3. `decision_basis = full | cached_overlay`
4. `staleness_flags[]`

---

## 11. runtime 单活锁（P0）

锁模型字段：

1. `lease_name`（固定 `papertrade_runtime`）
2. `owner_session_id`
3. `owner_pid`
4. `owner_host`
5. `heartbeat_at`
6. `lease_expires_at`

规则：

1. 仅 lease 持有者可写账本。
2. 心跳过期可被新会话接管。
3. `start/stop` 都必须经过 lease 校验。

---

## 12. lot 级持仓（P0）

最小模型：

1. 买入生成 `position_lot`（含 `open_date/t1_sellable_date/qty_remaining/cost`）。
2. 卖出时按 lot 分配，扣减 `qty_remaining`。
3. `sellable_qty_today` 由 lot 聚合（只算已过 T+1 的数量）。
4. `paper_positions` 作为投影表，不再直接作为主事实。

---

## 13. API 修订版（P0）

## 13.1 Read Model

1. `GET /api/v2/dashboard/summary`
2. `GET /api/v2/watchlist`
3. `GET /api/v2/ticker/{code}`
4. `GET /api/v2/events`
5. `GET /api/v2/positions`
6. `GET /api/v2/orders`
7. `GET /api/v2/runtime/status`

## 13.2 Write Model

1. `POST /api/v2/sim/intents`
2. `POST /api/v2/sim/intents/{intent_id}/cancel`
3. `POST /api/v2/runtime/start`
4. `POST /api/v2/runtime/stop`
5. `POST /api/v2/runtime/replay-loop`（P1）
6. `POST /api/v2/runtime/rebuild-readmodels`（P1）

---

## 14. 可观测性指标（P0）

1. `quote_freshness_ms`
2. `analysis_freshness_ms`
3. `loop_duration_p50/p95`
4. `db_write_duration`
5. `event_queue_lag`
6. `rule_reject_rate`
7. `manual_vs_auto_order_count`
8. `cash_drift_check`
9. `equity_recompute_diff`

---

## 15. 数据库增量清单（P0）

新增/扩展目标表：

1. `runtime_sessions`
2. `runtime_loops`
3. `runtime_leases`
4. `analysis_snapshots`
5. `decision_snapshots`
6. `watcher_overlay_snapshots`
7. `order_intents`
8. `order_rule_checks`
9. `event_outbox`
10. `position_lots`
11. `lot_allocations`
12. `watchlists`
13. 对 `paper_orders/paper_fills/signals/paper_positions` 做关联字段扩展

---

## 16. 执行状态表（唯一进度看板）

| ID | 优先级 | 任务 | 状态 | 验收标准 |
|---|---|---|---|---|
| P0-1 | P0 | 统一 intent 状态机 + 幂等键 | DONE | 重复请求不重复落单，auto/manual 共用命令链 |
| P0-2 | P0 | 规则服务中心化（calendar+metadata+engine） | DONE | 同输入拒单码稳定且可复现 |
| P0-3 | P0 | loop 事务边界 + outbox | DONE | DB 与事件不再双写漂移 |
| P0-4 | P0 | runtime 单活锁 | DONE | 任意时刻仅一个 loop 写账本 |
| P0-5 | P0 | lot 持仓与 T+1 可卖 | DONE | 分批买卖与可卖数量正确 |
| P0-6 | P0 | API v2 读写分层 | DONE | 前端不再拼原始表 |
| P0-7 | P0 | 指标与健康监控 | DONE | 核心指标可查询并可告警 |
| P1-1 | P1 | 盘外挂单/开盘撮合 | DONE | 盘外命令可排队并受时钟驱动 |
| P1-2 | P1 | replay/rebuild 工具 | DONE | 可回放 loop 与重建读模型 |
| P1-3 | P1 | 高级前端体验增强 | DONE | 实战盯盘可读性提升 |
| P1-4 | P1 | dashboard 自动刷新/只读 HTTP 包装 | DONE | 页面可自动刷新，本地 HTTP 仅读取 API 读模型 |

---

## 17. 测试策略（最小完整覆盖）

1. 规则引擎单测：时段、涨跌停、手数、T+1、禁做空、现金持仓。
2. 状态机单测：合法/非法转移、终态保护。
3. 账本一致性：`cash + mv = equity`，lot 与 realized/unrealized 对齐。
4. realtime loop 集成：full/cached_overlay 混跑与失败恢复。
5. 幂等与并发：重复点击、并发请求、冲突处理。
6. 进程重启恢复：lease 过期接管、outbox 补发。

---

## 18. 实时执行记录（每完成一步必须追加）

记录模板：

- 时间：
- 完成项（对应 ID）：
- 变更文件：
- 关键变更：
- 验证命令：
- 验证结果：
- 风险/待办：

### 2026-04-23

- 时间：2026-04-23
- 完成项（对应 ID）：`DOC-BASELINE`
- 变更文件：`docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`
- 关键变更：将方案升级为 `v2`，固化 P0/P1、状态机、规则服务、事务边界、outbox、快照、单活锁、lot、API 分层与测试策略
- 验证命令：`wc -l docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md && sed -n '1,220p' docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`
- 验证结果：文档生成成功，结构与内容完整
- 风险/待办：进入 `P0-1` 开发前需先出首批 migration 与命令服务接口定义

- 时间：2026-04-23
- 完成项（对应 ID）：`DOC-v2.1-SUPPLEMENT`
- 变更文件：`docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`
- 关键变更：补齐 v2.1 四大块实现细节：数据库字段级设计、API request/response+错误语义、事件 catalog+payload 规范、P0/P1 分步实施与验收标准
- 验证命令：`wc -l docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md && sed -n '1,220p' docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`
- 验证结果：v2.1 补充稿生成成功，可直接指导建表/API/事件流/排期实现
- 风险/待办：下一步进入 `P0-1`，先出 migration 草案与 intent 命令服务代码骨架

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-1 (phase-a)`
- 变更文件：`skills/deep-analysis/scripts/papertrade/schema_v2.py`, `skills/deep-analysis/scripts/papertrade/ledger.py`, `skills/deep-analysis/scripts/papertrade/intent_service.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py`
- 关键变更：新增 v2.1 首批 schema+migration（runtime/intents/snapshots/outbox/lots 等表与索引）；新增 intent 幂等服务与状态迁移；`run_cycle` 自动路径接入 auto-intent（ORDER/SIM_ONLY 时落 `order_intents` 并迁移状态）
- 验证命令：`python3 -m py_compile ...`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：py_compile 通过；新增单测 3/3 通过；实时链路 smoke 通过；新表已成功创建（`missing=[]`）
- 风险/待办：manual API 还未接入 intent 命令链（将在 P0-6 写模型 API 时接通）；rule_check_snapshots 与 order_rule_checks 尚未由规则引擎写入（P0-2）

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-1 (phase-b, close)`
- 变更文件：`skills/deep-analysis/scripts/papertrade/command_service.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/papertrade/human_gateway.py`, `skills/deep-analysis/scripts/tests/test_papertrade_command_service.py`
- 关键变更：新增统一命令提交服务 `submit_intent_order`（intent 幂等 + 状态迁移 + 预校验 + 模拟成交），并将 auto 路径 (`run_cycle`) 与 manual 路径 (`human_gateway.submit_manual_order`) 同时接入该服务；重复请求按 `source + idempotency_key` 直接复用 intent 且不重复落单
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/command_service.py skills/deep-analysis/scripts/papertrade/run_cycle.py skills/deep-analysis/scripts/papertrade/human_gateway.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；单测 6/6 通过；realtime smoke 通过；验收点“同幂等键不重复落单 + auto/manual 共用命令链”满足
- 风险/待办：manual 的 HTTP 写接口尚未落地（P0-6）；A 股规则码当前仍为命令层最小校验，完整规则服务与 `rule_check_snapshots/order_rule_checks` 在 P0-2 完成

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-2`
- 变更文件：`skills/deep-analysis/scripts/papertrade/market_calendar_service.py`, `skills/deep-analysis/scripts/papertrade/instrument_metadata_provider.py`, `skills/deep-analysis/scripts/papertrade/rule_engine.py`, `skills/deep-analysis/scripts/papertrade/command_service.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/papertrade/human_gateway.py`, `skills/deep-analysis/scripts/tests/test_papertrade_command_service.py`, `skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py`
- 关键变更：新增中心化规则服务三件套（市场时钟 + 标的信息 + 规则引擎），并在 `submit_intent_order` 统一接入 `validate_and_record_rule_check`，将校验结果写入 `rule_check_snapshots` 与 `order_rule_checks`；拒单码统一为稳定语义（含 `RULE_MARKET_CLOSED` / `RULE_ODD_LOT_BUY` / `RULE_TPLUS1_VIOLATION` 等）
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/market_calendar_service.py skills/deep-analysis/scripts/papertrade/instrument_metadata_provider.py skills/deep-analysis/scripts/papertrade/rule_engine.py skills/deep-analysis/scripts/papertrade/command_service.py skills/deep-analysis/scripts/papertrade/human_gateway.py skills/deep-analysis/scripts/papertrade/run_cycle.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；相关单测 9/9 通过；realtime smoke 通过；验收点 “T+1/odd lot/盘外拒单” 全部满足
- 风险/待办：当前 market calendar 仍为 MVP 版（工作日+时段，未接节假日库）；涨跌停目前使用基础板块推断（主板/创业板/科创板/ST），更精细板块元数据在后续迭代补强

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-3 (phase-a)`
- 变更文件：`skills/deep-analysis/scripts/papertrade/outbox.py`, `skills/deep-analysis/scripts/papertrade/runtime_store.py`, `skills/deep-analysis/scripts/papertrade/command_service.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/tests/test_papertrade_command_service.py`, `skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py`
- 关键变更：引入 `event_outbox` 写入工具与 runtime loop/session 存储层；`run_cycle` 新增 `loop.started/signal.generated/nav.updated/loop.finished/loop.failed` 事件写入与 `runtime_loops` 状态更新；`run_cycle` 异常路径新增 `conn.rollback()` 防止当前轮半状态提交；`run_realtime` 接入 `runtime_sessions` 生命周期（start/heartbeat/stop）
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/outbox.py skills/deep-analysis/scripts/papertrade/runtime_store.py skills/deep-analysis/scripts/papertrade/command_service.py skills/deep-analysis/scripts/papertrade/run_cycle.py skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；相关单测 10/10 通过；realtime smoke 通过；实库可见 `runtime_loops` 与 `event_outbox` 新记录（`loop.started/loop.finished/nav.updated/signal.generated`）
- 风险/待办：目前仅完成 outbox 落库，尚未实现 dispatcher/SSE 发布与发布重试（P0-4）；`runtime_loops` 仍按单轮整体记录，单票级事务统计与更细粒度失败恢复后续补齐

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-3 (phase-b, close)`
- 变更文件：`skills/deep-analysis/scripts/papertrade/outbox.py`, `skills/deep-analysis/scripts/papertrade/event_dispatcher.py`, `skills/deep-analysis/scripts/papertrade/event_stream.py`, `skills/deep-analysis/scripts/papertrade/api_events.py`, `skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py`, `skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py`
- 关键变更：补齐 outbox dispatcher（`pending -> published/retry/dead`）与指数退避重试；新增 SSE 事件总线与 `Last-Event-ID` 语义（命中缓冲可重放，未命中发 `stream.gap` 提示补拉）；新增 `/events` 对应查询能力（`limit/cursor/order` + `query_events_since`）；`run_realtime` 每轮自动触发 outbox dispatch 并记录 `event_dispatch` 统计
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/outbox.py skills/deep-analysis/scripts/papertrade/event_dispatcher.py skills/deep-analysis/scripts/papertrade/event_stream.py skills/deep-analysis/scripts/papertrade/api_events.py skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；相关单测 16/16 通过；realtime smoke 通过且日志含 `event_dispatch`（示例：`scanned=8, published=8, retried=0, dead=0`）；验证“DB 成功、SSE 失败时 `/events` 仍可查 pending 事件”用例通过
- 风险/待办：HTTP 路由层尚未接入真实 Web 框架（当前已提供 `api_events.py` 查询/流式原语，路由封装放在 P0-6 统一 API 分层时接入）；`P0-4 runtime 单活锁` 仍待实现

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-4`
- 变更文件：`skills/deep-analysis/scripts/papertrade/runtime_store.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/papertrade/config.py`, `skills/deep-analysis/scripts/papertrade/config.example.json`, `skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py`
- 关键变更：实现 runtime lease 全链路（`acquire/renew/release/owner-check`）并接入 realtime 启停流程；`run_realtime` 启动时抢占 `papertrade_runtime`，冲突时返回 `RUNTIME_ALREADY_RUNNING`，循环前续租、退出释放；`run_cycle` 写账前强制 lease 校验，独立 run_cycle 场景自动申请并在退出时释放临时 lease，避免并发改账
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/runtime_store.py skills/deep-analysis/scripts/papertrade/run_cycle.py skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/papertrade/config.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --config /tmp/papertrade_lock_cfg.json --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；相关单测 17/17 通过；realtime smoke 通过；锁冲突场景返回 `RUNTIME_ALREADY_RUNNING`（验证“任意时刻仅一个 loop 写账本”）
- 风险/待办：`run_cycle` 单次执行期间 lease 续租当前按“轮级续租”策略（TTL 默认 900s）；若未来出现单轮超长执行（>TTL）需补后台续租线程或细粒度心跳

- 时间：2026-04-23
- 完成项（对应 ID）：`P0-5`
- 变更文件：`skills/deep-analysis/scripts/papertrade/lot_allocator.py`, `skills/deep-analysis/scripts/papertrade/ledger.py`, `skills/deep-analysis/scripts/papertrade/command_service.py`, `skills/deep-analysis/scripts/papertrade/rule_engine.py`, `skills/deep-analysis/scripts/tests/test_papertrade_lots.py`, `skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py`
- 关键变更：新增 lot 分配器并接入成交链路：BUY 成交创建 `position_lots`，SELL 成交按 FIFO 分配并写 `lot_allocations`，`positions + paper_positions` 统一改为 lot 投影结果；规则引擎 SELL 校验改为基于 `sellable_qty`，并保留当日新买仓位优先命中 `RULE_TPLUS1_VIOLATION` 的语义；`mark_to_market` 对存在 lots 的标的改为重算 lot 投影
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/lot_allocator.py skills/deep-analysis/scripts/papertrade/ledger.py skills/deep-analysis/scripts/papertrade/command_service.py skills/deep-analysis/scripts/papertrade/rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_lots.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py skills/deep-analysis/scripts/tests/test_papertrade_lots.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；相关单测 20/20 通过（新增 lots 用例覆盖“当日买入 sellable=0、分批买卖 realized/unrealized 一致、cash+mv=equity”）；realtime smoke 通过
- 风险/待办：当前 SELL lot 分配策略固定 FIFO（未参数化为 avg-cost/LIFO）；历史无 lot 的老仓位在首次 SELL 时走一次性 bootstrap（用于兼容迁移），后续如需严格审计需补 replay/rebuild 工具（P1-2）

### 2026-04-24

- 时间：2026-04-24
- 完成项（对应 ID）：`P0-6`
- 变更文件：`skills/deep-analysis/scripts/papertrade/api_common.py`, `skills/deep-analysis/scripts/papertrade/read_models.py`, `skills/deep-analysis/scripts/papertrade/command_handlers.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/tests/test_papertrade_api_v2.py`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`, `docs/PAPERTRADE-MODULE-REPORT.md`
- 关键变更：新增 API v2 分层原语：7 个读模型聚合接口（dashboard/watchlist/ticker/events/positions/orders/runtime status）、3 个写模型命令（`/sim/order`, `/runtime/start`, `/runtime/stop`）、统一 `ok/error/meta` envelope 与错误码；`POST /sim/order` 接入现有 manual intent/rule/order/fill/lot 链，规则拒绝会返回统一错误结构并保留 `intent_id/rule_check_snapshot_id` 审计信息
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/api_common.py skills/deep-analysis/scripts/papertrade/read_models.py skills/deep-analysis/scripts/papertrade/command_handlers.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/tests/test_papertrade_api_v2.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_api_v2.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py skills/deep-analysis/scripts/tests/test_papertrade_lots.py skills/deep-analysis/scripts/tests/test_papertrade_api_v2.py`
- 验证结果：编译通过；新增 API v2 单测 4/4 通过；papertrade 聚焦回归 24/24 通过
- 风险/待办：当前实现为 Web 框架无关的 Python API 原语与轻量路由分发器，尚未绑定真实 HTTP server；`/runtime/start` 目前创建 runtime session/lease 作为控制面原语，不在同进程内派生常驻 loop，真实常驻运行仍由 `run_realtime.py` 负责

- 时间：2026-04-24
- 完成项（对应 ID）：`P0-7`
- 变更文件：`skills/deep-analysis/scripts/papertrade/schema_v2.py`, `skills/deep-analysis/scripts/papertrade/runtime_store.py`, `skills/deep-analysis/scripts/papertrade/ledger.py`, `skills/deep-analysis/scripts/papertrade/metrics.py`, `skills/deep-analysis/scripts/papertrade/health.py`, `skills/deep-analysis/scripts/papertrade/recovery.py`, `skills/deep-analysis/scripts/papertrade/read_models.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/papertrade/run_cycle.py`, `skills/deep-analysis/scripts/tests/test_papertrade_health_metrics.py`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`, `docs/PAPERTRADE-MODULE-REPORT.md`
- 关键变更：新增 `runtime_loops.metrics_json` 增量字段并在 `run_cycle` 完成时写入核心指标；新增 `metrics.py` 查询 `quote_freshness_ms/analysis_freshness_ms/loop_duration_p50/p95/event_queue_lag/rule_reject_rate/manual_vs_auto_order_count`；新增 `health.py` 做 runtime/outbox/账本一致性健康检查，`equity_recompute_diff` 超阈值会写 `alerts` 并发 `alert.raised`；新增 `recovery.py` 支持过期 lease 接管并标记旧 session；API 增加 `/api/v1/metrics` 与 `/api/v1/health` 原语
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/schema_v2.py skills/deep-analysis/scripts/papertrade/runtime_store.py skills/deep-analysis/scripts/papertrade/ledger.py skills/deep-analysis/scripts/papertrade/metrics.py skills/deep-analysis/scripts/papertrade/health.py skills/deep-analysis/scripts/papertrade/recovery.py skills/deep-analysis/scripts/papertrade/read_models.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/papertrade/run_cycle.py skills/deep-analysis/scripts/tests/test_papertrade_health_metrics.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_health_metrics.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py skills/deep-analysis/scripts/tests/test_papertrade_lots.py skills/deep-analysis/scripts/tests/test_papertrade_api_v2.py skills/deep-analysis/scripts/tests/test_papertrade_health_metrics.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`、`sqlite3 .cache/paper_trade/paper.db "SELECT status, json_extract(metrics_json, '$.quote_freshness_ms'), json_extract(metrics_json, '$.analysis_freshness_ms'), json_extract(metrics_json, '$.event_queue_lag') FROM runtime_loops ORDER BY started_at_ms DESC LIMIT 1;"`
- 验证结果：编译通过；新增 P0-7 单测 4/4 通过；papertrade 聚焦回归 28/28 通过；realtime smoke 通过并写入 nav drift 字段与 event dispatch；最新 SQLite `runtime_loops.metrics_json` 可查（示例：`completed|824|159476971|2`）；实际 `api.get_health()` 返回 `status=ok`
- 风险/待办：健康检查当前为主动查询/脚本调用模型，尚未做后台定时告警守护；`analysis_freshness_ms` 对缺少生成时间戳的旧缓存使用保守推断，后续 P1 可在 analysis snapshot 落库时改为强事实时间

### 2026-04-25

- 时间：2026-04-25
- 完成项（对应 ID）：`P1-1`
- 变更文件：`skills/deep-analysis/scripts/papertrade/command_service.py`, `skills/deep-analysis/scripts/papertrade/human_gateway.py`, `skills/deep-analysis/scripts/papertrade/command_handlers.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/papertrade/opening_matcher.py`, `skills/deep-analysis/scripts/papertrade/run_realtime.py`, `skills/deep-analysis/scripts/tests/test_papertrade_opening_matcher.py`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`, `docs/PAPERTRADE-MODULE-REPORT.md`
- 关键变更：保留 P0 `IOC` 盘外拒单语义，同时新增 `DAY + queue_when_market_closed` 的盘外排队语义；新增 `opening_matcher.py` 扫描 `queued + DAY + latest_order_id IS NULL` 的 intent，并在交易时钟进入开盘连续竞价后强制重跑规则、复用原命令链撮合成交或拒绝；`run_realtime.py` 每轮先按时钟触发队列撮合；API 新增 `POST /api/v1/runtime/match-opening`
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/command_service.py skills/deep-analysis/scripts/papertrade/human_gateway.py skills/deep-analysis/scripts/papertrade/command_handlers.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/papertrade/opening_matcher.py skills/deep-analysis/scripts/papertrade/run_realtime.py skills/deep-analysis/scripts/tests/test_papertrade_opening_matcher.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_opening_matcher.py`、`pytest -q skills/deep-analysis/scripts/tests/test_papertrade_intent_service.py skills/deep-analysis/scripts/tests/test_papertrade_command_service.py skills/deep-analysis/scripts/tests/test_papertrade_rule_engine.py skills/deep-analysis/scripts/tests/test_papertrade_runtime_store.py skills/deep-analysis/scripts/tests/test_papertrade_event_dispatcher.py skills/deep-analysis/scripts/tests/test_papertrade_api_events_stream.py skills/deep-analysis/scripts/tests/test_papertrade_lots.py skills/deep-analysis/scripts/tests/test_papertrade_api_v2.py skills/deep-analysis/scripts/tests/test_papertrade_health_metrics.py skills/deep-analysis/scripts/tests/test_papertrade_opening_matcher.py`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`
- 验证结果：编译通过；新增 P1-1 单测 3/3 通过；papertrade 聚焦回归 31/31 通过；realtime smoke 通过，2026-04-25 周六时 `opening_match` 正确返回 `market_phase=holiday, matched=0`
- 风险/待办：当前开盘撮合仍是简化即时全量成交模型，不做复杂部分成交和盘口深度滑点；`DAY` 队列使用当前 MVP 市场日历（工作日+时段），节假日精细日历仍待后续接入

- 时间：2026-04-25
- 完成项（对应 ID）：`P1-2`
- 变更文件：`skills/deep-analysis/scripts/papertrade/replay.py`, `skills/deep-analysis/scripts/papertrade/rebuild.py`, `skills/deep-analysis/scripts/papertrade/system_config.py`, `skills/deep-analysis/scripts/papertrade/command_handlers.py`, `skills/deep-analysis/scripts/papertrade/api.py`, `skills/deep-analysis/scripts/tests/test_papertrade_replay_rebuild.py`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`, `docs/PAPERTRADE-MODULE-REPORT.md`
- 关键变更：新增只读 `replay_loop`，按 `runtime_loops.event_batch_id/loop_id` 汇总 signals/intents/orders/fills/outbox/nav，并从 loop fills 重建关键仓位视图用于核对；新增 `rebuild_read_models`，从 `position_lots/lot_allocations` 重建 `positions + paper_positions + portfolio_nav_snapshots`，并返回事实表前后计数；新增 `system_config_history` 写入工具并发 `config.changed` 事件；API 增加 `POST /api/v1/runtime/replay-loop`, `POST /api/v1/runtime/rebuild-readmodels`, `POST /api/v1/runtime/config`
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/replay.py skills/deep-analysis/scripts/papertrade/rebuild.py skills/deep-analysis/scripts/papertrade/system_config.py skills/deep-analysis/scripts/papertrade/command_handlers.py skills/deep-analysis/scripts/papertrade/api.py skills/deep-analysis/scripts/tests/test_papertrade_replay_rebuild.py`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_replay_rebuild.py -q`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`、`python3 skills/deep-analysis/scripts/papertrade/run_realtime.py --tickers 600519.SH --from-cache-only --dry-run --max-loops 1 --poll-seconds 1 --watcher-overlay --quote-overlay`、`python3 -c "...api.post_replay_loop(...) + api.post_rebuild_readmodels(...)..."`
- 验证结果：编译通过；新增 P1-2 单测 3/3 通过；papertrade 聚焦回归 34/34 通过；realtime smoke 通过；真实 DB 最新 loop 可 replay（示例：`signals=1, events=4`），rebuild 在无持仓 smoke 下 `facts_unchanged=true`
- 风险/待办：`replay_loop` 当前是审计/复原视图，不重新执行历史订单写入，避免制造第二条事实链；`rebuild_read_models` 默认不发 outbox 事件，以确保“重建读模型不改写事实表”，如后续需要管理员操作审计，可在外层单独调用 `/runtime/config` 或新增独立 admin audit 事件

- 时间：2026-04-25
- 完成项（对应 ID）：`P1-3`
- 变更文件：`skills/deep-analysis/scripts/papertrade/dashboard_renderer.py`, `skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`, `docs/PAPERTRADE-MODULE-REPORT.md`
- 关键变更：新增无依赖静态盯盘仪表盘导出器，复用 `api.py` 读模型聚合 dashboard/watchlist/positions/orders/events/metrics/health/runtime 状态，生成 `.cache/paper_trade/dashboard.html`；页面以操作台布局展示权益、现金、仓位市值、回撤、事件滞后、watchlist 动作、持仓 sellable、订单状态与最近 outbox 事件，适合本地打开或打印归档
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/dashboard_renderer.py skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py -q`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`、`python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py --output .cache/paper_trade/dashboard.html --title "Papertrade Realtime Desk"`、`rg -n "Papertrade Realtime Desk|Watchlist|Positions|Orders|Events|Runtime" .cache/paper_trade/dashboard.html`
- 验证结果：编译通过；新增 P1-3 单测 2/2 通过；papertrade 聚焦回归 36/36 通过；静态导出成功，生成 `.cache/paper_trade/dashboard.html`（约 24KB），核心区块均可检索
- 风险/待办：当前是静态 HTML 导出，不是常驻 HTTP 前端；刷新依赖重新运行导出命令或浏览器手动刷新已生成文件，后续如接真实 Web server 可直接复用 `build_dashboard_payload()` 作为数据层

- 时间：2026-04-25
- 完成项（对应 ID）：`VERIFY-CURRENT`
- 变更文件：`docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`
- 关键变更：按当前代码事实复核 P0-1 到 P1-3 状态；未新增实现；确认任务表仍为全 DONE，`papertrade/`、`test_papertrade_*.py`、dashboard/replay/rebuild/health/runtime API 与文档描述一致
- 验证命令：`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`、`python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py --output .cache/paper_trade/dashboard.html --title "Papertrade Realtime Desk"`、`rg -n "Papertrade Realtime Desk|Watchlist|Positions|Orders|Events|Runtime|Health|Event Lag" .cache/paper_trade/dashboard.html`、`python3 -c "...api.handle_request(...health/runtime/replay/rebuild...)..."`
- 验证结果：papertrade 聚焦回归 `36 passed`；dashboard 静态导出成功且核心区块均可检索；真实 DB 上 `health_status=ok`；runtime API `start/status/stop` 可用；最新 completed loop 可 replay（`signals=1, events=4`）；rebuild 返回 `facts_unchanged=true` 并写入读模型投影
- 风险/待办：未发现文档与代码不一致；下一阶段只能从现有风险/延后项中挑最小增量，优先候选为静态 dashboard 的只读自动刷新/轻量 HTTP 包装、health 主动查询的后台/CLI 守护化、或市场日历精细化

- 时间：2026-04-25
- 完成项（对应 ID）：`P1-4`
- 变更文件：`skills/deep-analysis/scripts/papertrade/dashboard_renderer.py`, `skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC-v2.1.md`, `docs/PAPERTRADE-MODULE-REPORT.md`
- 关键变更：在不改变默认静态导出行为的前提下，为 dashboard 增加 `--refresh-seconds` 自动刷新标记与 `--serve` 本地只读 HTTP 包装；HTTP 包装仅提供 `GET /`, `GET /dashboard.html`, `GET /api/dashboard/payload`, `GET /healthz`，请求处理只读取现有 API 读模型，不接入写命令、不新增事实源、不引入 Web 框架
- 验证命令：`python3 -m py_compile skills/deep-analysis/scripts/papertrade/dashboard_renderer.py skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_dashboard_renderer.py -q`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`、`python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py --output .cache/paper_trade/dashboard.html --title "Papertrade Realtime Desk" --refresh-seconds 15`、`python3 -c "...create_dashboard_server(...)+urllib smoke..."`、`rg -n "Papertrade Realtime Desk|http-equiv=\"refresh\" content=\"15\"|auto refresh 15s|Watchlist|Positions|Orders|Events|Runtime" .cache/paper_trade/dashboard.html`
- 验证结果：编译通过；dashboard renderer 单测 `4 passed`；papertrade 聚焦回归 `38 passed`；静态 dashboard 成功写入自动刷新标记；本地只读 HTTP smoke 返回 `html_ok=true`, `payload_ok=true`, `health_ok=true`，页面区块 `Watchlist/Positions/Orders/Runtime/Events` 可见
- 风险/待办：当前 HTTP 包装定位为本机只读工作台，不是生产 Web server；未加入认证、TLS、远程暴露、写模型命令或 SSE 推送，后续如需公网/团队共享必须先补访问控制和部署边界

- 时间：2026-04-25
- 完成项（对应 ID）：`ADHOC-002361-DEEP`
- 变更文件：`skills/deep-analysis/scripts/.cache/002361.SZ/panel.json`, `skills/deep-analysis/scripts/.cache/002361.SZ/agent_analysis.json`, `skills/deep-analysis/scripts/.cache/002361.SZ/synthesis.json`, `skills/deep-analysis/scripts/reports/002361.SZ_20260425/full-report-standalone.html`, `.cache/paper_trade/paper.db`, `.cache/paper_trade/dashboard.html`
- 关键变更：按全量 agent 闭环为神剑股份 `002361.SZ` 补齐人审分析：复核 22 维原始数据、重写 51 评委 `signal/score/headline/reasoning`，写入 `agent_analysis.json`（`agent_reviewed=true`、23 个维度定性评语、6 个 qualitative deep dive、风险/买入区间/多空辩论覆盖）；重跑 stage2 生成 HTML 报告；用 `run_cycle --from-cache-only --dry-run --watcher-overlay` 将该 AI 深度分析接入 papertrade 信号链路，SQLite 最新 `signals.summary_json.agent_reviewed=true`
- 验证命令：`python3 -c "...lib.agent_analysis_validator.validate(...)"`、`UZI_NO_AUTO_OPEN=1 python3 -c "from run_real_test import stage2; stage2('002361.SZ')"`、`python3 skills/deep-analysis/scripts/papertrade/run_cycle.py --tickers 002361.SZ --depth deep --from-cache-only --dry-run --watcher-overlay`、`python3 -m pytest skills/deep-analysis/scripts/tests/test_papertrade_*.py -q`、`python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py --db .cache/paper_trade/paper.db --output .cache/paper_trade/dashboard.html --title "Papertrade Realtime Desk" --refresh-seconds 15`
- 验证结果：`agent_analysis.json` schema 通过；stage2 输出 `agent_reviewed=True`，综合评分 `60.8/100 · 观望优先`，data gap `7_industry.growth` 已 ack；报告生成成功；papertrade 最新信号 `action=AVOID`, `score_final=40.108`, `bonus_agent=4.0`, `panel_signal_dist={bullish:17, neutral:17, bearish:13, skip:4}`，盯盘人物 overlay 再扣 `-3.0`；papertrade 聚焦回归 `38 passed, 1 warning`
- 风险/待办：stage2 自查仍有 2 个非阻断 warning（DCF alias 自查提示、panel formula 版本提示），不影响 `synthesis.institutional_modeling.dcf_intrinsic=0.99` 和最终 HTML；分享卡/战报 PNG 因当前 macOS sandbox 下 Chromium MachPort 权限失败而跳过，HTML 主报告已正常生成

- 时间：2026-04-26
- 完成项（对应 ID）：`P2-DOC-INIT`
- 变更文件：`docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md`, `docs/PAPERTRADE-REALTIME-WEB-TASK-DOC.md`
- 关键变更：新增 P2 继续任务文档，明确下一阶段问题不在 P0/P1 底座，而在候选池、实时行情快照、开收盘调度和非买入机会状态；后续 P2 执行以 `docs/PAPERTRADE-P2-CANDIDATE-REALTIME-TASK-DOC.md` 为任务表基线
- 验证命令：文档更新，无代码验证
- 验证结果：P2 任务表已创建，下一步只做 `P2-1 候选池数据模型与 API 原语`
- 风险/待办：P0/P1 完成状态不变；P2 每完成一项必须同步更新新任务文档执行记录
