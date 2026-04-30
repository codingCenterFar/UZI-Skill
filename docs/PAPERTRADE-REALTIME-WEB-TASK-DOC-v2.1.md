# Papertrade 实时盯盘 Web 系统 · v2.1 补充设计稿

更新时间：2026-04-23  
定位：仅补 v2 缺失的实现细节，不重写 v2 总体原则

---

## A. 数据库字段级设计

## A0. 对 v2 的修正点（仅实现层）

1. v2 中 `order_rule_checks` 与 `rule_check_snapshots` 语义有重叠，v2.1 明确分工：
   - `rule_check_snapshots`：不可变快照，存完整规则命中细节。
   - `order_rule_checks`：操作日志，记录 intent 的第几次校验结果与耗时。
2. v2 中未单列 `runtime_leases`，v2.1 新增该表支撑单活锁。
3. v2 中 `positions` 语义偏事实，v2.1 明确其为 lot 聚合读模型，不作为主事实。

---

## A1. 关系总览（signal/intents/orders/fills/lots）

1. `signal` 与 `order_intents`：`1:N`。一个 signal 可派生多个 intent（自动重试、人工跟单、系统回放）。
2. `order_intents` 与 `orders`：`1:N`。一个 intent 可产生多个 order（例如 partial 后余量继续执行）。
3. `orders` 与 `fills`：`1:N`。一个 order 允许 0..N 条 fill，支持 partial。
4. `fills` 与 `position_lots`：
   - BUY fill：创建一个或多个 lot。
   - SELL fill：通过 `lot_allocations` 分配到若干 OPEN lot。
5. `positions`：由 `position_lots + lot_allocations + latest_quote` 派生聚合，不直接手工写。
6. T+1：在 `position_lots.t1_sellable_date` 体现，`sellable_qty` 为 `remaining_qty` 在当日可卖 lot 的聚合值。

---

## A2. 表级说明书（字段级）

## A2.1 runtime_sessions

用途：runtime 启停会话事实，记录一段 loop 生命周期。  
类型：事实表（control fact）

字段示例：
- `session_id: TEXT, PK`
- `status: TEXT, enum(starting|running|stopping|stopped|failed)`
- `start_request_id: TEXT, nullable`（幂等启动请求 ID）
- `owner_pid: INTEGER`
- `owner_host: TEXT`
- `config_hash: TEXT`
- `config_json: TEXT(JSON-TEXT)`
- `started_at_ms: INTEGER`
- `heartbeat_at_ms: INTEGER`
- `ended_at_ms: INTEGER, nullable`
- `last_loop_id: TEXT, nullable`
- `stop_reason: TEXT, nullable`
- `created_at_ms: INTEGER`
- `updated_at_ms: INTEGER`

约束与索引：
- PK：`session_id`
- Unique：`start_request_id`（可空）
- 逻辑关联：`last_loop_id -> runtime_loops.loop_id`
- 索引：
  - `idx_runtime_sessions_status_updated_at(status, updated_at_ms DESC)`：查当前会话与最近异常。
  - `idx_runtime_sessions_heartbeat(heartbeat_at_ms DESC)`：健康检查。

一致性字段：`start_request_id`、`config_hash`

---

## A2.2 runtime_leases（新增，v2.1）

用途：单活锁 lease。  
类型：事实表（lock/control）

字段示例：
- `lease_name: TEXT, PK`（固定 `papertrade_runtime`）
- `owner_session_id: TEXT`
- `owner_pid: INTEGER`
- `owner_host: TEXT`
- `heartbeat_at_ms: INTEGER`
- `lease_expires_at_ms: INTEGER`
- `version: INTEGER`（CAS 续租）
- `updated_at_ms: INTEGER`

约束与索引：
- PK：`lease_name`
- 外键逻辑关联：`owner_session_id -> runtime_sessions.session_id`
- 索引：
  - `idx_runtime_leases_expiry(lease_expires_at_ms)`：接管判断。

幂等字段：`version`

---

## A2.3 runtime_loops

用途：每轮 loop 执行事实。  
类型：事实表

字段示例：
- `loop_id: TEXT, PK`
- `session_id: TEXT`
- `loop_no: INTEGER`
- `status: TEXT, enum(started|completed|partial_failed|failed|skipped)`
- `snapshot_ts_ms: INTEGER`
- `decision_version: TEXT`
- `event_batch_id: TEXT`
- `decision_basis_summary_json: TEXT(JSON-TEXT)`
- `staleness_summary_json: TEXT(JSON-TEXT)`
- `ticker_count: INTEGER`
- `success_count: INTEGER`
- `failed_count: INTEGER`
- `started_at_ms: INTEGER`
- `ended_at_ms: INTEGER, nullable`
- `duration_ms: INTEGER, nullable`
- `db_write_duration_ms: INTEGER, nullable`
- `error_json: TEXT(JSON-TEXT), nullable`
- `retry_of_loop_id: TEXT, nullable`

约束与索引：
- PK：`loop_id`
- Unique：`(session_id, loop_no)`
- 逻辑关联：`session_id -> runtime_sessions.session_id`
- 索引：
  - `idx_runtime_loops_session_no(session_id, loop_no DESC)`：会话内翻页。
  - `idx_runtime_loops_status_started(status, started_at_ms DESC)`：找失败轮次。
  - `idx_runtime_loops_event_batch(event_batch_id)`：事件对账。

一致性字段：`event_batch_id`

---

## A2.4 analysis_snapshots

用途：分析输入快照，固定 full/cached 的分析语境。  
类型：快照表

字段示例：
- `analysis_snapshot_id: TEXT, PK`
- `ticker: TEXT`
- `basis: TEXT, enum(full|cached)`
- `analysis_version: TEXT`
- `source_cache_key: TEXT, nullable`
- `raw_ref_path: TEXT, nullable`
- `features_json: TEXT(JSON-TEXT)`
- `synthesis_json: TEXT(JSON-TEXT)`
- `source_data_ts_ms: INTEGER, nullable`
- `generated_at_ms: INTEGER`
- `analysis_age_ms: INTEGER`
- `staleness_flags_json: TEXT(JSON-TEXT)`
- `snapshot_hash: TEXT`

约束与索引：
- PK：`analysis_snapshot_id`
- 索引：
  - `idx_analysis_snapshots_ticker_time(ticker, generated_at_ms DESC)`：单票最新分析。
  - `idx_analysis_snapshots_hash(snapshot_hash)`：快照去重/排查。

一致性字段：`snapshot_hash`

---

## A2.5 watcher_overlay_snapshots

用途：watcher 修正前后快照。  
类型：快照表

字段示例：
- `watcher_overlay_snapshot_id: TEXT, PK`
- `loop_id: TEXT`
- `ticker: TEXT`
- `watcher_version: TEXT`
- `score_before: REAL`
- `score_delta: REAL`
- `score_after: REAL`
- `action_before: TEXT`
- `action_after: TEXT`
- `votes_json: TEXT(JSON-TEXT)`
- `created_at_ms: INTEGER`

约束与索引：
- PK：`watcher_overlay_snapshot_id`
- 索引：
  - `idx_watcher_overlay_loop_ticker(loop_id, ticker)`：按轮展示。
  - `idx_watcher_overlay_ticker_time(ticker, created_at_ms DESC)`：单票历史。

---

## A2.6 decision_snapshots

用途：最终决策快照（含 staleness 与解释锚点）。  
类型：快照表

字段示例：
- `decision_snapshot_id: TEXT, PK`
- `loop_id: TEXT`
- `ticker: TEXT`
- `analysis_snapshot_id: TEXT`
- `watcher_overlay_snapshot_id: TEXT, nullable`
- `decision_basis: TEXT, enum(full|cached_overlay)`
- `decision_version: TEXT`
- `policy_version: TEXT`
- `analysis_age_ms: INTEGER`
- `quote_age_ms: INTEGER`
- `quote_snapshot_ts_ms: INTEGER, nullable`
- `staleness_flags_json: TEXT(JSON-TEXT)`
- `score_strategy: REAL`
- `score_panel: REAL`
- `score_tactical: REAL`
- `score_core: REAL`
- `score_final_before_watcher: REAL`
- `score_final: REAL`
- `action_before_watcher: TEXT`
- `action: TEXT`
- `penalties_json: TEXT(JSON-TEXT)`
- `gates_json: TEXT(JSON-TEXT)`
- `summary_json: TEXT(JSON-TEXT)`
- `created_at_ms: INTEGER`

约束与索引：
- PK：`decision_snapshot_id`
- Unique：`(loop_id, ticker)`
- 逻辑关联：
  - `analysis_snapshot_id -> analysis_snapshots.analysis_snapshot_id`
  - `watcher_overlay_snapshot_id -> watcher_overlay_snapshots.watcher_overlay_snapshot_id`
- 索引：
  - `idx_decision_snapshots_ticker_time(ticker, created_at_ms DESC)`
  - `idx_decision_snapshots_basis_time(decision_basis, created_at_ms DESC)`

一致性字段：`decision_version`, `policy_version`

---

## A2.7 rule_check_snapshots

用途：不可变规则快照，复现拒单或放行。  
类型：快照表

字段示例：
- `rule_check_snapshot_id: TEXT, PK`
- `ticker: TEXT`
- `side: TEXT, enum(BUY|SELL)`
- `qty: INTEGER`
- `order_type: TEXT, enum(MARKET|LIMIT)`
- `limit_price: REAL, nullable`
- `market_phase: TEXT`
- `trade_date: TEXT`
- `calendar_version: TEXT`
- `instrument_version: TEXT`
- `rule_engine_version: TEXT`
- `decision_basis: TEXT, enum(full|cached_overlay)`
- `analysis_age_ms: INTEGER`
- `quote_age_ms: INTEGER`
- `allow: INTEGER, enum(0|1)`
- `reject_code: TEXT, nullable`
- `reject_reason: TEXT, nullable`
- `checks_json: TEXT(JSON-TEXT)`
- `created_at_ms: INTEGER`

约束与索引：
- PK：`rule_check_snapshot_id`
- 索引：
  - `idx_rule_check_snapshots_ticker_time(ticker, created_at_ms DESC)`
  - `idx_rule_check_snapshots_allow_time(allow, created_at_ms DESC)`
  - `idx_rule_check_snapshots_reject_code(reject_code, created_at_ms DESC)`

一致性字段：`calendar_version`, `instrument_version`, `rule_engine_version`

---

## A2.8 order_rule_checks

用途：intent 规则执行日志（可多次尝试）。  
类型：事实表（operation log）

字段示例：
- `check_id: TEXT, PK`
- `intent_id: TEXT`
- `attempt_no: INTEGER`
- `rule_check_snapshot_id: TEXT`
- `result: TEXT, enum(pass|reject)`
- `reject_code: TEXT, nullable`
- `reject_reason: TEXT, nullable`
- `latency_ms: INTEGER`
- `checked_at_ms: INTEGER`

约束与索引：
- PK：`check_id`
- Unique：`(intent_id, attempt_no)`
- 逻辑关联：
  - `intent_id -> order_intents.intent_id`
  - `rule_check_snapshot_id -> rule_check_snapshots.rule_check_snapshot_id`
- 索引：
  - `idx_order_rule_checks_intent(intent_id, attempt_no DESC)`
  - `idx_order_rule_checks_result_time(result, checked_at_ms DESC)`

幂等字段：`attempt_no`

---

## A2.9 order_intents

用途：统一命令状态机主表。  
类型：事实表（command fact）

字段示例：
- `intent_id: TEXT, PK`
- `signal_id: INTEGER, nullable`（映射旧 `signals.id`）
- `loop_id: TEXT, nullable`（auto 场景）
- `source: TEXT, enum(auto|manual|system_replay)`
- `operator_id: TEXT, nullable`
- `operator_channel: TEXT, nullable`（web|cli|loop）
- `idempotency_key: TEXT`
- `request_hash: TEXT`
- `ticker: TEXT`
- `side: TEXT, enum(BUY|SELL)`
- `qty: INTEGER`
- `order_type: TEXT, enum(MARKET|LIMIT)`
- `limit_price: REAL, nullable`
- `time_in_force: TEXT, enum(IOC|DAY)`
- `status: TEXT, enum(intent|validated|queued|rejected|filled|partial|cancelled|expired)`
- `decision_snapshot_id: TEXT, nullable`
- `rule_check_snapshot_id: TEXT, nullable`
- `latest_order_id: TEXT, nullable`
- `reject_code: TEXT, nullable`
- `reject_reason: TEXT, nullable`
- `expires_at_ms: INTEGER, nullable`
- `correlation_id: TEXT`
- `causation_id: TEXT, nullable`
- `created_at_ms: INTEGER`
- `updated_at_ms: INTEGER`

约束与索引：
- PK：`intent_id`
- Unique：`(source, idempotency_key)`
- 逻辑关联：
  - `decision_snapshot_id -> decision_snapshots.decision_snapshot_id`
  - `rule_check_snapshot_id -> rule_check_snapshots.rule_check_snapshot_id`
- 索引：
  - `idx_order_intents_status_created(status, created_at_ms DESC)`：命令队列。
  - `idx_order_intents_ticker_status(ticker, status, created_at_ms DESC)`：单票调试。
  - `idx_order_intents_loop(loop_id, created_at_ms)`：loop 追踪。
  - `idx_order_intents_correlation(correlation_id)`：链路追踪。

幂等字段：`idempotency_key`, `request_hash`

---

## A2.10 orders

用途：执行层订单记录。  
类型：事实表

字段示例：
- `order_id: TEXT, PK`
- `intent_id: TEXT`
- `order_seq: INTEGER`
- `ticker: TEXT`
- `side: TEXT`
- `qty: INTEGER`
- `order_type: TEXT`
- `limit_price: REAL, nullable`
- `status: TEXT, enum(queued|open|partially_filled|filled|cancelled|expired|rejected)`
- `submitted_at_ms: INTEGER`
- `last_update_at_ms: INTEGER`
- `filled_qty: INTEGER`
- `avg_fill_price: REAL, nullable`
- `reject_code: TEXT, nullable`
- `reject_reason: TEXT, nullable`
- `loop_id: TEXT, nullable`
- `correlation_id: TEXT`
- `causation_id: TEXT, nullable`

约束与索引：
- PK：`order_id`
- Unique：`(intent_id, order_seq)`
- 逻辑关联：`intent_id -> order_intents.intent_id`
- 索引：
  - `idx_orders_status_submitted(status, submitted_at_ms DESC)`：执行队列与历史。
  - `idx_orders_ticker_time(ticker, submitted_at_ms DESC)`：单票历史。
  - `idx_orders_intent(intent_id)`：命令追踪。

一致性字段：`correlation_id`

---

## A2.11 fills

用途：成交事实。  
类型：事实表

字段示例：
- `fill_id: TEXT, PK`
- `order_id: TEXT`
- `intent_id: TEXT`
- `fill_seq: INTEGER`
- `ticker: TEXT`
- `side: TEXT`
- `qty: INTEGER`
- `price: REAL`
- `fee: REAL`
- `tax: REAL`
- `slippage_bps: REAL`
- `fill_value: REAL`
- `fill_ts_ms: INTEGER`
- `loop_id: TEXT, nullable`
- `correlation_id: TEXT`
- `causation_id: TEXT, nullable`

约束与索引：
- PK：`fill_id`
- Unique：`(order_id, fill_seq)`
- 逻辑关联：
  - `order_id -> orders.order_id`
  - `intent_id -> order_intents.intent_id`
- 索引：
  - `idx_fills_ticker_time(ticker, fill_ts_ms DESC)`
  - `idx_fills_intent(intent_id, fill_seq)`
  - `idx_fills_order(order_id, fill_seq)`

---

## A2.12 position_lots

用途：lot 级仓位事实，T+1 与成本核算主依据。  
类型：事实表

字段示例：
- `lot_id: TEXT, PK`
- `ticker: TEXT`
- `buy_fill_id: TEXT`
- `buy_intent_id: TEXT`
- `open_qty: INTEGER`
- `remaining_qty: INTEGER`
- `open_price: REAL`
- `open_fee_alloc: REAL`
- `open_tax_alloc: REAL`
- `open_trade_date: TEXT`（YYYY-MM-DD）
- `t1_sellable_date: TEXT`
- `status: TEXT, enum(OPEN|CLOSED)`
- `opened_at_ms: INTEGER`
- `closed_at_ms: INTEGER, nullable`
- `created_at_ms: INTEGER`
- `updated_at_ms: INTEGER`

约束与索引：
- PK：`lot_id`
- 逻辑关联：
  - `buy_fill_id -> fills.fill_id`
  - `buy_intent_id -> order_intents.intent_id`
- 索引：
  - `idx_position_lots_ticker_status(ticker, status, t1_sellable_date)`：计算可卖量。
  - `idx_position_lots_buy_fill(buy_fill_id)`：成交追溯。

一致性字段：`remaining_qty`（不可负）

---

## A2.13 lot_allocations（建议新增）

用途：SELL fill 对应哪些 BUY lots。  
类型：事实表

字段示例：
- `allocation_id: TEXT, PK`
- `sell_fill_id: TEXT`
- `lot_id: TEXT`
- `qty: INTEGER`
- `open_price: REAL`
- `close_price: REAL`
- `realized_pnl: REAL`
- `created_at_ms: INTEGER`

约束与索引：
- PK：`allocation_id`
- Unique：`(sell_fill_id, lot_id)`
- 逻辑关联：
  - `sell_fill_id -> fills.fill_id`
  - `lot_id -> position_lots.lot_id`
- 索引：
  - `idx_lot_allocations_sell_fill(sell_fill_id)`
  - `idx_lot_allocations_lot(lot_id)`

原因：不加该表时，卖出如何分配 lot 无法完整审计。

---

## A2.14 positions（聚合表，保留）

用途：前端快速读取聚合持仓。  
类型：读模型表（projection）

字段示例：
- `ticker: TEXT, PK`
- `qty: INTEGER`
- `sellable_qty: INTEGER`
- `avg_cost: REAL`
- `last_price: REAL`
- `market_value: REAL`
- `unrealized_pnl: REAL`
- `realized_pnl_cum: REAL`
- `lot_count_open: INTEGER`
- `source_lot_watermark: INTEGER`（可选，投影版本）
- `updated_at_ms: INTEGER`

约束与索引：
- PK：`ticker`
- 索引：
  - `idx_positions_market_value(market_value DESC)`：持仓排序。
  - `idx_positions_updated(updated_at_ms DESC)`：刷新状态。

说明：事实来自 `position_lots/lot_allocations/fills`，本表可重建。

---

## A2.15 portfolio_nav_snapshots

用途：净值快照历史。  
类型：事实快照表

字段示例：
- `nav_snapshot_id: TEXT, PK`
- `as_of_date: TEXT`
- `as_of_ts_ms: INTEGER`
- `session_id: TEXT, nullable`
- `loop_id: TEXT, nullable`
- `cash: REAL`
- `position_market_value: REAL`
- `equity: REAL`
- `cumulative_return_pct: REAL`
- `drawdown_pct: REAL`
- `cash_drift_check: REAL`
- `equity_recompute_diff: REAL`
- `created_at_ms: INTEGER`

约束与索引：
- PK：`nav_snapshot_id`
- Unique：`(loop_id)`（每轮最多一条）
- 索引：
  - `idx_nav_snapshots_date_ts(as_of_date DESC, as_of_ts_ms DESC)`
  - `idx_nav_snapshots_session(session_id, as_of_ts_ms DESC)`

---

## A2.16 event_outbox

用途：状态变更事件待发布队列。  
类型：事实表（integration fact）

字段示例：
- `event_id: TEXT, PK`
- `event_batch_id: TEXT`
- `event_type: TEXT`
- `entity_type: TEXT`
- `entity_id: TEXT`
- `source: TEXT`
- `severity: TEXT, enum(info|warn|error)`
- `correlation_id: TEXT`
- `causation_id: TEXT, nullable`
- `payload_version: INTEGER`
- `payload_json: TEXT(JSON-TEXT)`
- `status: TEXT, enum(pending|publishing|published|failed|dead)`
- `publish_attempts: INTEGER`
- `next_retry_at_ms: INTEGER, nullable`
- `last_error: TEXT, nullable`
- `created_at_ms: INTEGER`
- `published_at_ms: INTEGER, nullable`

约束与索引：
- PK：`event_id`
- 索引：
  - `idx_event_outbox_status_retry(status, next_retry_at_ms, created_at_ms)`：dispatcher 主扫描索引。
  - `idx_event_outbox_correlation(correlation_id)`：链路查询。
  - `idx_event_outbox_entity(entity_type, entity_id, created_at_ms DESC)`：实体时间线。

一致性字段：`correlation_id`, `causation_id`, `event_batch_id`

---

## A2.17 watchlists

用途：盯盘列表配置。  
类型：基础配置表

字段示例：
- `watchlist_id: TEXT, PK`
- `name: TEXT`
- `is_default: INTEGER, enum(0|1)`
- `symbols_json: TEXT(JSON-TEXT)`
- `status: TEXT, enum(active|archived)`
- `created_at_ms: INTEGER`
- `updated_at_ms: INTEGER`

约束与索引：
- PK：`watchlist_id`
- Unique：`name`
- Partial Unique：`is_default=1`（最多一个默认）
- 索引：
  - `idx_watchlists_status_updated(status, updated_at_ms DESC)`

---

## A2.18 system_config_history（建议新增）

用途：规则/runtime 配置变更审计与回放。  
类型：基础配置历史表

字段示例：
- `config_change_id: TEXT, PK`
- `scope: TEXT, enum(runtime|rules|risk|watchlist|api)`
- `config_hash: TEXT`
- `config_json: TEXT(JSON-TEXT)`
- `changed_by: TEXT`
- `change_reason: TEXT`
- `effective_from_ms: INTEGER`
- `correlation_id: TEXT`
- `created_at_ms: INTEGER`

约束与索引：
- PK：`config_change_id`
- Unique：`(scope, config_hash, effective_from_ms)`
- 索引：
  - `idx_system_config_scope_time(scope, effective_from_ms DESC)`

原因：`config.changed` 事件需可追溯，回放时需知道规则版本。

---

## B. API 详细设计

## B0. 通用约定

统一成功响应：
```json
{
  "ok": true,
  "data": {},
  "meta": {
    "correlation_id": "crr_01J...",
    "ts_ms": 1770000000000
  }
}
```

统一错误响应：
```json
{
  "ok": false,
  "error": {
    "error_code": "RULE_MARKET_CLOSED",
    "message": "A股当前为午休时段，暂不允许成交",
    "details": {
      "market_phase": "lunch_break",
      "trade_date": "2026-04-23",
      "intent_id": "it_01J..."
    },
    "retryable": true,
    "correlation_id": "crr_01J..."
  }
}
```

错误码（最小集）：
1. `RULE_MARKET_CLOSED`
2. `RULE_PRICE_LIMIT_EXCEEDED`
3. `RULE_TPLUS1_VIOLATION`
4. `RULE_INSUFFICIENT_CASH`
5. `RULE_INSUFFICIENT_SELLABLE`
6. `RULE_ODD_LOT_BUY`
7. `RULE_NO_SHORT_ALLOWED`
8. `IDEMPOTENCY_CONFLICT`
9. `RUNTIME_ALREADY_RUNNING`
10. `RUNTIME_LOCK_NOT_OWNED`
11. `RUNTIME_NOT_RUNNING`
12. `INVALID_REQUEST`
13. `INTERNAL_ERROR`

---

## B1. 读模型 API

## B1.1 GET /api/v1/dashboard/summary

用途：仪表盘聚合总览。  
类型：读模型

request 示例：
```json
{}
```

response 示例：
```json
{
  "ok": true,
  "data": {
    "equity": 1002350.12,
    "cash": 602350.12,
    "position_market_value": 400000.0,
    "drawdown_pct": -1.23,
    "cumulative_return_pct": 0.24,
    "runtime": {
      "status": "running",
      "session_id": "rs_01J...",
      "last_loop_id": "lp_01J..."
    },
    "freshness": {
      "quote_freshness_ms": 1200,
      "analysis_freshness_ms": 62000,
      "event_queue_lag": 3
    }
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000000000}
}
```

失败示例：`INTERNAL_ERROR`

关联表：`portfolio_nav_snapshots`, `positions`, `runtime_sessions`, `runtime_loops`, `event_outbox`

---

## B1.2 GET /api/v1/watchlist

用途：盯盘列表与最新建议。  
类型：读模型

request 示例：
```json
{
  "watchlist_id": "wl_default",
  "limit": 100
}
```

response 示例：
```json
{
  "ok": true,
  "data": {
    "watchlist_id": "wl_default",
    "items": [
      {
        "ticker": "600519.SH",
        "last_price": 1409.5,
        "change_pct": -0.18,
        "action": "OBSERVE",
        "score_final": 63.2,
        "decision_basis": "cached_overlay",
        "analysis_age_ms": 186000,
        "quote_age_ms": 900,
        "staleness_flags": ["ANALYSIS_STALE"]
      }
    ]
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000000000}
}
```

失败示例：`INVALID_REQUEST`（watchlist 不存在）

关联表：`watchlists`, `decision_snapshots`, `analysis_snapshots`, `positions`

---

## B1.3 GET /api/v1/ticker/{code}

用途：单票深度详情（解释+执行+仓位）。  
类型：读模型

response 示例：
```json
{
  "ok": true,
  "data": {
    "ticker": "600519.SH",
    "latest_decision": {
      "decision_snapshot_id": "ds_01J...",
      "action": "PAPER_WATCH_B",
      "score_final": 68.1,
      "penalties": [],
      "gates": [],
      "watcher_overlay": {
        "score_before": 66.1,
        "score_after": 68.1,
        "votes": []
      }
    },
    "position": {
      "qty": 800,
      "sellable_qty": 500,
      "avg_cost": 1388.2,
      "unrealized_pnl": 17040.0
    },
    "open_intents": [],
    "recent_orders": [],
    "recent_fills": []
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000000000}
}
```

失败示例：`INVALID_REQUEST`（ticker 格式不合法）

关联表：`decision_snapshots`, `watcher_overlay_snapshots`, `order_intents`, `orders`, `fills`, `positions`, `position_lots`

---

## B1.4 GET /api/v1/events

用途：历史事件查询（SSE 兜底与补拉）。  
类型：读模型

request 参数：
- `limit`（默认 100，最大 1000）
- `cursor`（使用上次返回的 `next_cursor`，值为最旧 `event_id`）
- `order`（默认 `desc`）

response 示例：
```json
{
  "ok": true,
  "data": {
    "events": [
      {
        "event_id": "evt_01J...",
        "event_type": "loop.finished",
        "entity_type": "runtime_loop",
        "entity_id": "lp_01J...",
        "ts_ms": 1770000000123,
        "payload": {}
      }
    ],
    "next_cursor": "evt_01J..."
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

失败示例：`INVALID_REQUEST`（limit 超范围）

关联表：`event_outbox`

SSE 关系：REST 用于补历史；SSE 用于实时推送。

---

## B1.5 GET /api/v1/positions

用途：组合持仓列表。  
类型：读模型

response 示例：
```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "ticker": "600519.SH",
        "qty": 800,
        "sellable_qty": 500,
        "avg_cost": 1388.2,
        "last_price": 1409.5,
        "market_value": 1127600.0,
        "unrealized_pnl": 17040.0,
        "lot_count_open": 3
      }
    ]
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000000000}
}
```

关联表：`positions`

---

## B1.6 GET /api/v1/orders

用途：订单/intent 查询（前端订单页）。  
类型：读模型

request 示例：
```json
{
  "ticker": "600519.SH",
  "status": "open",
  "limit": 50
}
```

response 示例：
```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "intent_id": "it_01J...",
        "order_id": "od_01J...",
        "source": "manual",
        "status": "partially_filled",
        "qty": 1000,
        "filled_qty": 600,
        "avg_fill_price": 1401.3,
        "decision_snapshot_id": "ds_01J...",
        "rule_check_snapshot_id": "rcs_01J...",
        "created_at_ms": 1770000000000
      }
    ]
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

关联表：`order_intents`, `orders`, `order_rule_checks`

---

## B1.7 GET /api/v1/runtime/status

用途：runtime 会话、锁、最近 loop 状态。  
类型：读模型

response 示例：
```json
{
  "ok": true,
  "data": {
    "runtime": {
      "status": "running",
      "session_id": "rs_01J...",
      "owner_pid": 93211,
      "heartbeat_at_ms": 1770000000900
    },
    "lease": {
      "lease_name": "papertrade_runtime",
      "lease_expires_at_ms": 1770000005900
    },
    "last_loop": {
      "loop_id": "lp_01J...",
      "status": "completed",
      "duration_ms": 843
    }
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

失败示例：`RUNTIME_NOT_RUNNING`

关联表：`runtime_sessions`, `runtime_leases`, `runtime_loops`

---

## B2. 写模型 API

## B2.1 POST /api/v1/sim/order（核心）

用途：创建模拟交易命令（manual）。  
类型：写模型

幂等：
1. Header `Idempotency-Key` 必填。
2. body 可传 `client_order_id`，若存在则要求与 header 一致。
3. 同 key 同 payload：返回首次结果。
4. 同 key 不同 payload：返回 `409 IDEMPOTENCY_CONFLICT`。

request 示例：
```json
{
  "ticker": "600519.SH",
  "side": "BUY",
  "qty": 200,
  "order_type": "MARKET",
  "limit_price": null,
  "time_in_force": "IOC",
  "source": "manual",
  "operator": {
    "operator_id": "local_user",
    "channel": "web"
  },
  "client_order_id": "cli_20260423_0001",
  "note": "web manual buy"
}
```

成功（已接收，待执行）示例：
```json
{
  "ok": true,
  "data": {
    "intent": {
      "intent_id": "it_01J...",
      "status": "queued",
      "source": "manual",
      "idempotency_key": "idem_...",
      "decision_snapshot_id": "ds_01J...",
      "rule_check_snapshot_id": "rcs_01J..."
    },
    "order": {
      "order_id": "od_01J...",
      "status": "queued"
    },
    "rule_check": {
      "allow": true,
      "reject_code": null
    }
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

成功（立即成交）示例：
```json
{
  "ok": true,
  "data": {
    "intent": {"intent_id": "it_01J...", "status": "filled"},
    "order": {"order_id": "od_01J...", "status": "filled"},
    "fills": [{"fill_id": "fl_01J...", "qty": 200, "price": 1409.5}],
    "rule_check": {"allow": true}
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

规则拒绝示例（已落审计）：
```json
{
  "ok": false,
  "error": {
    "error_code": "RULE_TPLUS1_VIOLATION",
    "message": "当日买入仓位不可卖出",
    "details": {
      "intent_id": "it_01J...",
      "rule_check_snapshot_id": "rcs_01J...",
      "sellable_qty": 0,
      "requested_qty": 200
    },
    "retryable": false,
    "correlation_id": "crr_..."
  }
}
```

失败响应示例：
1. `IDEMPOTENCY_CONFLICT`（409）
2. `INVALID_REQUEST`（400）
3. `INTERNAL_ERROR`（500）

关联表：`order_intents`, `rule_check_snapshots`, `order_rule_checks`, `orders`, `fills`, `event_outbox`

---

## B2.2 POST /api/v1/runtime/start

用途：启动 realtime loop。  
类型：写模型

request 示例：
```json
{
  "poll_seconds": 60,
  "refresh_every_loops": 5,
  "watchlist_id": "wl_default",
  "quote_overlay": true,
  "watcher_overlay": true,
  "dry_run": false,
  "operator": {"operator_id": "local_user", "channel": "web"}
}
```

response 示例：
```json
{
  "ok": true,
  "data": {
    "session_id": "rs_01J...",
    "status": "running",
    "lease": {
      "lease_name": "papertrade_runtime",
      "owner_pid": 93211,
      "lease_expires_at_ms": 1770000005900
    }
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

失败示例：
- `RUNTIME_ALREADY_RUNNING`（409）

幂等：可选 `Idempotency-Key`，推荐支持。

关联表：`runtime_sessions`, `runtime_leases`, `system_config_history`, `event_outbox`

---

## B2.3 POST /api/v1/runtime/stop

用途：停止 realtime loop。  
类型：写模型

request 示例：
```json
{
  "session_id": "rs_01J...",
  "reason": "operator_stop",
  "operator": {"operator_id": "local_user", "channel": "web"}
}
```

response 示例：
```json
{
  "ok": true,
  "data": {
    "session_id": "rs_01J...",
    "status": "stopping"
  },
  "meta": {"correlation_id": "crr_...", "ts_ms": 1770000001000}
}
```

失败示例：
1. `RUNTIME_NOT_RUNNING`（409）
2. `RUNTIME_LOCK_NOT_OWNED`（409）

关联表：`runtime_sessions`, `runtime_leases`, `event_outbox`

---

## B3. 建议新增 API（可选，但推荐）

1. `GET /api/v1/intents/{id}`：查单个 intent 全链路。
2. `GET /api/v1/orders/{id}`：查单个 order + fills。
3. `POST /api/v1/runtime/replay-loop`：P1。
4. `POST /api/v1/runtime/rebuild-readmodels`：P1。
5. `POST /api/v1/runtime/config`：记录配置变更并发 `config.changed`。

---

## C. 事件流详细设计

## C1. 事件类型 catalog

| event_type | 触发点 | entity_type | 关键 payload |
|---|---|---|---|
| runtime.started | start 成功 | runtime_session | session_id, config_hash |
| runtime.stopped | stop 完成 | runtime_session | session_id, reason |
| runtime.heartbeat | 心跳续租 | runtime_lease | lease_name, expires_at |
| loop.started | 单轮开始 | runtime_loop | loop_id, loop_no, session_id |
| loop.finished | 单轮完成 | runtime_loop | metrics, counts, decision_basis_summary |
| loop.failed | 单轮失败 | runtime_loop | error_code, error_message |
| signal.generated | 产出 signal | signal | ticker, action, score |
| intent.created | 命令创建 | intent | intent_id, source, idempotency_key |
| intent.validated | 规则通过 | intent | intent_id, rule_check_snapshot_id |
| intent.rejected | 规则拒绝 | intent | reject_code, reject_reason, checks |
| order.created | 订单创建 | order | order_id, intent_id, qty |
| order.filled | 全量成交 | order | order_id, fills, avg_fill_price |
| order.partially_filled | 部分成交 | order | order_id, filled_qty, remaining_qty |
| order.cancelled | 订单取消 | order | order_id, reason |
| position.updated | 持仓投影更新 | position | ticker, qty, sellable_qty |
| nav.updated | 净值更新 | portfolio_nav | equity, drawdown_pct |
| alert.raised | 告警触发 | alert | level, title, body |
| config.changed | 配置变更 | system_config | scope, config_hash |

---

## C2. 统一事件 envelope

```json
{
  "event_id": "evt_01J...",
  "event_type": "intent.rejected",
  "entity_type": "intent",
  "entity_id": "it_01J...",
  "ts_ms": 1770000001000,
  "source": "manual",
  "severity": "warn",
  "correlation_id": "crr_01J...",
  "causation_id": "cmd_01J...",
  "event_batch_id": "eb_01J...",
  "payload_version": 1,
  "payload": {}
}
```

字段约束：
1. `event_id` 全局唯一，且可用于 SSE `id`。
2. `correlation_id` 同一命令链固定。
3. `causation_id` 指向直接触发动作（命令或上游事件）。

---

## C3. 关键事件 payload 示例

## C3.1 intent.rejected

```json
{
  "intent_id": "it_01J...",
  "ticker": "600519.SH",
  "side": "SELL",
  "qty": 200,
  "reject_code": "RULE_TPLUS1_VIOLATION",
  "reject_reason": "当日买入仓位不可卖",
  "rule_check_snapshot_id": "rcs_01J...",
  "market_phase": "continuous_auction",
  "trade_date": "2026-04-23"
}
```

## C3.2 order.filled

```json
{
  "order_id": "od_01J...",
  "intent_id": "it_01J...",
  "ticker": "600519.SH",
  "side": "BUY",
  "filled_qty": 200,
  "avg_fill_price": 1409.5,
  "fill_ids": ["fl_01J..."],
  "fee": 56.38,
  "tax": 0,
  "position_effect": {
    "qty_before": 600,
    "qty_after": 800,
    "sellable_qty_after": 500
  }
}
```

## C3.3 loop.finished

```json
{
  "loop_id": "lp_01J...",
  "session_id": "rs_01J...",
  "loop_no": 18,
  "status": "completed",
  "duration_ms": 843,
  "ticker_count": 6,
  "success_count": 6,
  "failed_count": 0,
  "decision_basis_summary": {
    "full": 1,
    "cached_overlay": 5
  },
  "freshness": {
    "quote_freshness_ms_p95": 1400,
    "analysis_freshness_ms_p95": 188000
  }
}
```

## C3.4 nav.updated

```json
{
  "nav_snapshot_id": "nav_01J...",
  "as_of_ts_ms": 1770000001000,
  "cash": 602350.12,
  "position_market_value": 400000.0,
  "equity": 1002350.12,
  "cumulative_return_pct": 0.24,
  "drawdown_pct": -1.23,
  "cash_drift_check": 0.0,
  "equity_recompute_diff": 0.0
}
```

---

## C4. DB 一致性策略（outbox + SSE）

1. outbox 记录写入时机：
   - 与业务状态变更同一 DB 事务内写入 `event_outbox(status=pending)`。
2. 发布成功标记：
   - dispatcher 成功推送 SSE 后更新 `status=published, published_at_ms`。
3. 发布失败重试：
   - `publish_attempts += 1`，写 `last_error`，设置 `next_retry_at_ms`。
   - 超过阈值后标记 `dead`，并触发 `alert.raised`。
4. SSE 断线补拉：
   - 客户端断线重连带 `Last-Event-ID`。
   - 服务端若检测 gap，客户端调用 `GET /api/v1/events?cursor=...` 补历史。
5. REST 兜底：
   - `/events` 永远可查已发布与待发布历史（按策略可仅返回 published）。

---

## C5. SSE 协议建议

1. SSE endpoint：`GET /api/v1/events/stream`。
2. 事件名：直接使用 `event_type`。
3. SSE 帧：
   - `id: <event_id>`
   - `event: <event_type>`
   - `data: <json envelope>`
4. 支持 `Last-Event-ID`。
5. 服务端每 15 秒发送注释心跳：`: heartbeat`。
6. 前端重连：
   - 初始重连间隔 1s；指数退避到 10s；加随机抖动。
   - 重连成功后先补拉 `/events` 防丢事件。

---

## D. P0 / P1 分步实施计划（可直接执行）

## D1. P0-1（先落状态机和表结构骨架）

目标：建立统一命令链与基础 schema。  
具体改动：
1. 新增表：`order_intents/orders/fills/analysis_snapshots/decision_snapshots/watcher_overlay_snapshots/rule_check_snapshots/order_rule_checks/runtime_sessions/runtime_loops/runtime_leases/event_outbox/position_lots/lot_allocations/portfolio_nav_snapshots/system_config_history/watchlists`。
2. `papertrade` 原有下单路径改为“先 intent 后 order/fill”。
3. 加入 `idempotency_key` 唯一约束与冲突处理。

涉及模块/文件（建议）：
1. `skills/deep-analysis/scripts/papertrade/schema_v2.py`（或 migrations SQL）
2. `skills/deep-analysis/scripts/papertrade/intent_service.py`（新增）
3. `skills/deep-analysis/scripts/papertrade/ledger.py`（改造）

产出物：
1. schema migration v2.1
2. intent 创建与状态迁移服务
3. 幂等处理单元测试

验收标准：
1. 同 `source + idempotency_key` 重复提交不生成第二条 intent。
2. 同 key 不同 payload 返回 `IDEMPOTENCY_CONFLICT`。
3. auto/manual 都落到 `order_intents`。

---

## D2. P0-2（规则引擎统一出口）

目标：统一规则语义，稳定拒单码。  
具体改动：
1. 实现 `market_calendar_service`。
2. 实现 `instrument_metadata_provider`。
3. 实现 `rule_engine.validate(intent, context)`，输出 `rule_check_snapshot`。
4. 命令链与 loop 全部接入 rule_engine。

涉及模块/文件（建议）：
1. `papertrade/market_calendar_service.py`
2. `papertrade/instrument_metadata_provider.py`
3. `papertrade/rule_engine.py`
4. `papertrade/run_cycle.py`, `papertrade/run_realtime.py`, `papertrade/api.py`

产出物：
1. 统一 reject_code 枚举
2. rule_check 快照落库
3. 规则单测

验收标准：
1. 人工卖出当天新买仓位返回 `RULE_TPLUS1_VIOLATION`。
2. 买入非 100 股整数倍返回 `RULE_ODD_LOT_BUY`。
3. 盘外下单返回 `RULE_MARKET_CLOSED`（MVP 策略）。

---

## D3. P0-3（loop 事务边界）

目标：避免半状态与双写不一致。  
具体改动：
1. loop 分阶段实现（读外部在事务外，写状态在事务内）。
2. 单 ticker 执行事务内完成：intent -> rule -> order/fill -> lots -> positions -> nav -> outbox。
3. loop 层写入 `runtime_loops` 统计与错误信息。

涉及模块/文件（建议）：
1. `papertrade/runtime_coordinator.py`（新增）
2. `papertrade/run_realtime.py`
3. `papertrade/run_cycle.py`

产出物：
1. loop_id/event_batch_id/correlation_id 全链路贯通
2. 单轮失败恢复语义

验收标准：
1. 人为注入 DB 异常后，不出现仅有 order 无 fill 的半状态。
2. `runtime_loops` 可准确显示 `completed/partial_failed/failed`。

---

## D4. P0-4（outbox + SSE）

目标：事件与状态一致，前端实时可用。  
具体改动：
1. 所有业务事件写入 outbox。
2. 增加 dispatcher 推送 SSE。
3. 增加 `/api/v1/events` 历史查询与补拉。

涉及模块/文件（建议）：
1. `papertrade/event_outbox.py`
2. `papertrade/event_dispatcher.py`
3. `papertrade/api_events.py`

产出物：
1. 事件 catalog 实现
2. SSE endpoint
3. outbox 重试机制

验收标准：
1. loop 写 DB 成功但 SSE 推送失败时，`/events` 仍能查到事件。
2. SSE 断线重连后可通过 `Last-Event-ID` + REST 补拉恢复连续性。

---

## D5. P0-5（lots 与聚合持仓）

目标：T+1、分批成本、可卖数量正确。  
具体改动：
1. BUY fill 创建 lot。
2. SELL fill 分配 lot，写 `lot_allocations`。
3. `positions` 改为 lot 投影结果。

涉及模块/文件（建议）：
1. `papertrade/lot_allocator.py`（新增）
2. `papertrade/positions_projector.py`（新增）
3. `papertrade/ledger.py`

产出物：
1. lot 分配逻辑
2. 可卖数量计算器
3. 持仓投影任务

验收标准：
1. 当日新买仓位 `sellable_qty=0`。
2. 分批买入后卖出，realized/unrealized 与 lot 分配一致。
3. `cash + market_value = equity` 持续成立。

---

## D6. P0-6（读模型 API）

目标：前端不拼表，直接消费聚合接口。  
具体改动：
1. 实现 7 个读模型 API。
2. 实现 `/sim/order`, `/runtime/start`, `/runtime/stop` 写接口。
3. 统一错误 envelope 与错误码。

涉及模块/文件（建议）：
1. `papertrade/api.py`
2. `papertrade/read_models.py`
3. `papertrade/command_handlers.py`

产出物：
1. API 文档
2. OpenAPI（可选）
3. 接口集成测试

验收标准：
1. `POST /sim/order` 被规则拒绝时返回统一错误结构并包含 `intent_id`。
2. `GET /watchlist` 返回 `analysis_age_ms/quote_age_ms/decision_basis/staleness_flags`。

---

## D7. P0-7（监控与恢复）

目标：可观测且可恢复。  
具体改动：
1. 在 `runtime_loops.metrics_json` 记录核心指标。
2. 增加健康检查与恢复策略（lease 过期接管、outbox 重试告警）。
3. 增加一致性校验任务（cash/equity drift）。

涉及模块/文件（建议）：
1. `papertrade/metrics.py`
2. `papertrade/health.py`
3. `papertrade/recovery.py`

产出物：
1. 指标采集与查询
2. 恢复流程脚本

验收标准：
1. `quote_freshness_ms`, `analysis_freshness_ms`, `loop_duration_p50/p95` 可查询。
2. 重启后 lease 过期可重新 `runtime/start`。
3. `equity_recompute_diff` 超阈值会触发 `alert.raised`。

---

## D8. P1-1（盘外挂单 / 开盘撮合）

目标：在不破坏 P0 盘外 `IOC` 拒单语义的前提下，让显式 `DAY` 命令可排队，并在交易时钟进入开盘连续竞价后复用规则/成交/lot 链撮合。  
具体改动：
1. `IOC` 盘外仍返回 `RULE_MARKET_CLOSED`。
2. `DAY` 且 `queue_when_market_closed=true` 的盘外命令进入 `order_intents(status=queued)`，不创建 order/fill。
3. 开盘撮合器扫描 `queued + DAY + latest_order_id IS NULL` 的 intent，按最新市场时钟和账户事实重新跑规则，放行后成交，失败则拒绝。
4. `run_realtime.py` 每轮先按时钟触发一次队列撮合；API 暴露 `POST /api/v1/runtime/match-opening` 原语。

涉及模块/文件：
1. `papertrade/command_service.py`
2. `papertrade/opening_matcher.py`
3. `papertrade/run_realtime.py`
4. `papertrade/api.py`

产出物：
1. 盘外 `DAY` 队列语义
2. 开盘撮合命令
3. 队列撮合集成测试

验收标准：
1. `IOC` 盘外命令继续拒绝为 `RULE_MARKET_CLOSED`。
2. `DAY` 盘外命令可进入 `queued`，且没有半成品 order/fill。
3. 开盘后队列只成交一次，重复撮合不重复落单。

---

## D9. P1-2（replay / rebuild 增强）

实现状态：`DONE`（2026-04-25）

目标：增强回放与修复能力，不阻塞 MVP 主线。  
具体改动：
1. `POST /api/v1/runtime/replay-loop`（兼容 `/api/v2/runtime/replay-loop` 与 `/runtime/replay-loop`）。
2. `POST /api/v1/runtime/rebuild-readmodels`（兼容 `/api/v2/runtime/rebuild-readmodels` 与 `/runtime/rebuild-readmodels`）。
3. `POST /api/v1/runtime/config` 记录配置变更审计并联动 `config.changed`。

涉及模块/文件（建议）：
1. `papertrade/replay.py`
2. `papertrade/rebuild.py`
3. `papertrade/system_config.py`

产出物：
1. 回放与重建命令：`replay.py`, `rebuild.py`, `command_handlers.py`, `api.py`
2. 配置审计命令：`system_config.py`
3. 管理员操作文档：`PAPERTRADE-MODULE-REPORT.md` 第 3.9 / 5.6

验收标准：
1. 指定 loop 回放后，读模型可重现关键结果。
2. 重建读模型不改写事实表。

---

## D10. 延后项（明确不在 MVP P0）

1. 历史延后项：盘外挂单进入待执行队列并开盘自动撮合。  
当前状态：已作为 `P1-1` 实现；P0 替代方案仍保留为 `IOC` 盘外统一返回 `RULE_MARKET_CLOSED`。

2. 延后项：复杂部分成交模型（滑点曲线、成交深度模拟）。  
延后原因：模型参数与验证成本高。  
替代方案：P0 用简化 IOC/即时成交模型 + 审计字段保留。

3. 延后项：高级策略参数调参与可视化回测面板。  
延后原因：不影响账本一致性主线。  
替代方案：P0 通过配置文件和日志回放完成调试。  
当前状态：`P1-3` 已先实现静态盯盘仪表盘导出（`dashboard_renderer.py`），用于提升实战扫读体验；高级参数调参与可视化回测面板仍保持延后。

4. 延后项：生产级 Web 前端与远程访问。  
延后原因：认证、部署、权限边界与写模型隔离需要单独设计。  
替代方案：`P1-4` 已实现本机只读 dashboard HTTP 包装与浏览器自动刷新；该入口只读 API 读模型，不暴露下单、runtime 控制或配置写接口。
