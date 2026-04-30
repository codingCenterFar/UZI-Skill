# Papertrade 模块介绍与规划报告

更新时间：2026-04-25

## 1. 背景与目标

`papertrade` 模块是对现有 UZI 分析链路的交易化胶水层，目标是：

1. 复用现有 `stage1/stage2` 深度分析与可解释输出能力。
2. 自动形成模拟盘建议与虚拟成交。
3. 维护独立模拟账本（仓位、现金、净值、回撤）。
4. 给人工发送决策提醒，并支持后续人工实盘回填与偏差分析。

该模块定位为：`模拟盘自动化 + 人工确认实盘`，不是自动实盘下单引擎。

## 2. 模块位置与入口

目录：`skills/deep-analysis/scripts/papertrade/`

核心入口：`run_cycle.py`

实时入口：`run_realtime.py`

API v2 原语入口：`api.py`

命令行参数：

1. `--tickers`：逗号分隔标的，必填。
2. `--depth`：`lite/medium/deep`，控制分析深度。
3. `--config`：JSON 配置文件路径。
4. `--mode`：`short/swing`，覆盖策略模式。
5. `--no-resume`：强制重跑 stage1 抓数。
6. `--from-cache-only`：只使用现有 `.cache` 结果，不重新分析。
7. `--dry-run`：只生成建议，不写模拟订单/成交。
8. `--live-return-pct`：人工实盘当日收益率（用于偏差统计占位）。

`run_realtime.py` 额外参数：

1. `--poll-seconds`：轮询间隔秒数。
2. `--max-loops`：最多循环轮数（`0` 表示无限）。
3. `--session-seconds`：最长运行秒数（`0` 表示无限）。
4. `--refresh-every-loops`：每 N 轮触发一次完整 `stage1/stage2`，其余轮次优先用缓存。
5. `--quote-overlay/--no-quote-overlay`：是否注入实时行情快照覆盖当前价。
6. `--watcher-overlay/--no-watcher-overlay`：是否启用模拟人物盯盘修正动作。

## 3. 核心能力

### 3.1 分析接入能力

由 `analysis_adapter.py` 承担：

1. 正常模式：调用 `stage1()` + `stage2()`，读取 `raw_data/dimensions/panel/strategy_signals/synthesis`。
2. 缓存模式：`--from-cache-only` 直接读取缓存文件，不触发耗时分析。
3. 失败兼容：识别 `name_not_resolved/non_stock_security/cache_missing` 并返回结构化状态。

### 3.2 决策能力（S_final）

由 `decision_policy.py` 承担：

1. 多源打分融合：`strategy + panel + tactical(short_trading) + core(overall_score)`。
2. 51 评委分组加权：`D/F/G`、`C`、`A/B/E` 分组评分用于 `short/swing` 两套模式。
3. 惩罚项：策略与评委方向冲突、策略看空占优、评委看空占优、短线偏空。
4. 动作分级：`PAPER_BUY_A / PAPER_WATCH_B / OBSERVE / AVOID`。
5. 门控降级：
   - 数据缺口 gate
   - agent_review gate（可配置）
   - intraday 质量 gate
   - basket 模式 gate

### 3.3 模拟执行与账本能力

由 `ledger.py` + `run_cycle.py` 承担：

1. 订单与成交：生成 `paper_orders`，即时模拟 `paper_fills`。
2. 持仓与现金：自动更新 `paper_positions` 与 `account_state`。
3. A 股约束：最小交易单位（手数）、`T+1` 卖出可用日。
4. 每日净值：落地 `paper_nav_daily`（权益、累计收益、回撤）。
5. 运行审计：`job_runs` 记录每次批次状态。

### 3.4 通知与偏差能力

1. 通知：`notifier.py` 写入 `alerts` 表与 `alerts.jsonl`，可选 webhook（`PAPERTRADE_WEBHOOK_URL`）。
2. 人工回填：`human_gateway.py` 支持人工动作与镜像实盘成交入库。
3. 偏差分析：`drift_analytics.py` 生成 `deviation_daily`（收益偏差、滑点偏差、样本数量）。

### 3.5 实时模拟盯盘能力（新增）

由 `run_realtime.py` + `watcher_persona.py` + `market_snapshot.py` 承担：

1. 持续循环：按秒级/分钟级轮询运行 `run_cycle`。
2. 混合刷新：`refresh_every_loops` 控制“完整分析刷新”与“缓存快跑”节奏。
3. 行情覆盖：每轮可注入最新 `fetch_basic` 快照价格，减少缓存价格滞后。
4. 模拟盯盘人物：趋势交易员、风控官、均值回归员会对 `S_final` 做实时修正。
5. 实时审计：输出 `.cache/paper_trade/realtime_logs.jsonl` 记录每轮状态。

### 3.6 API v2 读写分层能力（新增）

由 `api.py` + `read_models.py` + `command_handlers.py` 承担：

1. 读模型：`dashboard/watchlist/ticker/events/positions/orders/runtime status` 七类聚合数据，前端无需直接拼底表。
2. 写模型：`sim/order` 接入 manual intent -> rule check -> order/fill/lot 链，保留幂等与审计。
3. 运行控制：`runtime/start` 与 `runtime/stop` 维护 session/lease 控制面，沿用单活锁约束。
4. 统一响应：所有 API 原语返回 `ok/data/meta` 或 `ok/error`，规则拒绝包含 `intent_id` 与 `rule_check_snapshot_id`。

### 3.7 指标、健康检查与恢复能力（新增）

由 `metrics.py` + `health.py` + `recovery.py` 承担：

1. 指标落库：`runtime_loops.metrics_json` 记录 quote/analysis freshness、DB 写耗时、事件队列滞后、规则拒绝率、manual/auto 命令数。
2. 指标查询：`api.get_metrics()` / `/api/v1/metrics` 返回 `loop_duration_p50/p95` 等聚合指标。
3. 健康检查：`api.get_health()` / `/api/v1/health` 汇总 runtime、lease、outbox、账本一致性和核心指标。
4. 恢复语义：过期 lease 可被新 session 接管，旧 session 会被标记为 `failed/lease_expired_takeover`。
5. 一致性告警：`equity_recompute_diff` 或 cash drift 超阈值时写入 `alerts` 并发出 `alert.raised` 事件。

### 3.8 盘外挂单与开盘撮合能力（新增）

由 `opening_matcher.py` + `command_service.py` + `run_realtime.py` 承担：

1. `IOC` 盘外命令仍按 P0 规则拒绝为 `RULE_MARKET_CLOSED`。
2. 显式 `DAY` 命令可在盘外进入 `order_intents(status=queued)`，不会提前创建 order/fill。
3. 开盘撮合器只处理 `queued + DAY + latest_order_id IS NULL` 的 intent，并按最新市场时钟、现金、持仓、T+1、手数规则重新校验。
4. 撮合通过后复用原有 intent/order/fill/lot 链即时成交；规则失败则转为 rejected。
5. `run_realtime.py` 每轮先触发一次撮合，API 可通过 `post_match_opening()` / `/api/v1/runtime/match-opening` 手动触发。

### 3.9 回放、读模型重建与配置审计能力（新增）

由 `replay.py` + `rebuild.py` + `system_config.py` 承担：

1. `replay_loop()` / `/api/v1/runtime/replay-loop` 只读回放指定 `loop_id`，汇总 loop、session、signals、intents、orders、fills、outbox events 与 nav，并从 loop fills 重建关键仓位视图。
2. `rebuild_read_models()` / `/api/v1/runtime/rebuild-readmodels` 从 durable lots 重建 `positions`、`paper_positions`、`portfolio_nav_snapshots`，用于修复投影表漂移。
3. rebuild 会返回事实表前后计数和 `facts_unchanged`，默认不写 outbox，避免修复动作改变审计事实链。
4. `record_config_change()` / `/api/v1/runtime/config` 写 `system_config_history`，并发 `config.changed` outbox 事件，便于追踪 runtime 或策略配置变更。

### 3.10 静态盯盘仪表盘能力（新增）

由 `dashboard_renderer.py` 承担：

1. 复用 API 读模型聚合 dashboard、watchlist、positions、orders、events、metrics、health 与 runtime 状态。
2. 生成自包含 HTML 文件，默认输出 `.cache/paper_trade/dashboard.html`。
3. 支持 `--refresh-seconds` 写入浏览器自动刷新标记，适合盘中放在旁屏持续观察。
4. 支持 `--serve` 启动本地只读 HTTP 包装，提供 `/`, `/dashboard.html`, `/api/dashboard/payload`, `/healthz`。
5. 页面突出权益、现金、仓位市值、回撤、事件滞后、watchlist 动作、持仓可卖、订单状态与最近事件。
6. 不引入前端框架和构建链，可直接本地打开、只读浏览或归档，适合作为真实 Web UI 前的低风险工作台。

## 4. 当前能力边界（重要）

模块当前不包含以下能力：

1. 不直接对接券商 API，不执行真实下单。
2. 不提供毫秒级实时行情与撮合。
3. 不提供生产级 HTTP server；当前仅有本机只读 dashboard 包装，写模型仍是 Web 框架无关的 Python 原语与轻量路由分发器。
4. dashboard 不是实时推送 Web 前端；刷新依赖浏览器定时刷新或重新请求只读 payload，不提供 WebSocket/SSE UI。
5. 健康检查为主动查询/脚本调用模型，尚未提供后台常驻告警守护。
6. 开盘撮合仍为简化即时全量成交，不模拟盘口深度、排队优先级和复杂部分成交。
7. `replay_loop` 是审计复原视图，不会重新执行历史订单或写入新的事实表。
8. 不做完整 OMS 状态机（撤单/部分成交链路仍未产品化，风控拒单链路已接入规则快照）。
9. 不做交易时段调度守护进程（需外部 cron/任务编排）。
10. 偏差分析中的 `live_return_pct` 未接实盘系统时默认为占位值。

因此它是“研究/决策自动化 + 模拟执行”层，不是“自动实盘执行系统”。

## 5. 典型使用方式

### 5.1 快速验证（推荐先跑）

```bash
python3 skills/deep-analysis/scripts/papertrade/run_cycle.py \
  --tickers 600519.SH \
  --from-cache-only \
  --dry-run
```

用途：快速检查策略评分与动作，不触发重抓，不落模拟订单。

### 5.2 标准日常跑批

```bash
python3 skills/deep-analysis/scripts/papertrade/run_cycle.py \
  --tickers 600519.SH,002273.SZ \
  --depth medium \
  --config skills/deep-analysis/scripts/papertrade/config.example.json
```

用途：自动分析 + 模拟决策 + 模拟成交 + 净值落账 + 通知。

### 5.3 短线/波段模式切换

```bash
python3 skills/deep-analysis/scripts/papertrade/run_cycle.py \
  --tickers 600519.SH \
  --mode swing
```

用途：切换 51 评委分组加权方式与决策风格。

### 5.4 实时模拟盯盘（新增）

```bash
python3 skills/deep-analysis/scripts/papertrade/run_realtime.py \
  --tickers 600519.SH,002273.SZ \
  --depth medium \
  --config skills/deep-analysis/scripts/papertrade/config.example.json \
  --poll-seconds 60 \
  --refresh-every-loops 5
```

用途：持续模拟“盯盘-判断-下单/减仓”，全程只写模拟账本，不触发真实下单。

### 5.5 回放指定 loop（新增）

```python
from papertrade import api
from papertrade.config import load_config
from papertrade.ledger import connect

cfg = load_config()
conn = connect(cfg.db_path)
res = api.post_replay_loop(conn, payload={"loop_id": "<loop_id>"})
```

用途：排查某一轮 runtime 为什么产生某个信号、订单、成交或事件，并核对 loop fills 重建出的关键仓位结果。

### 5.6 重建读模型与记录配置变更（新增）

```python
from papertrade import api
from papertrade.config import load_config
from papertrade.ledger import connect

cfg = load_config()
conn = connect(cfg.db_path)

api.post_rebuild_readmodels(
    conn,
    cfg=cfg,
    payload={"trade_date": "2026-04-25", "tickers": ["600519.SH"]},
)

api.post_runtime_config(
    conn,
    cfg=cfg,
    payload={
        "scope": "runtime",
        "config": {"poll_seconds": 30},
        "changed_by": "operator",
        "change_reason": "盘中缩短轮询间隔",
    },
)
```

用途：读模型漂移时从 lots 事实表恢复投影；配置参数变化时留下可查询、可回放的审计记录。

### 5.7 导出静态盯盘仪表盘（新增）

```bash
python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py \
  --output .cache/paper_trade/dashboard.html \
  --title "Papertrade Realtime Desk" \
  --refresh-seconds 15
```

用途：把当前模拟盘状态导出为本地 HTML 工作台，便于盘中扫读、复盘或截图归档。

### 5.8 启动本地只读 dashboard（新增）

```bash
python3 skills/deep-analysis/scripts/papertrade/dashboard_renderer.py \
  --serve \
  --host 127.0.0.1 \
  --port 8765 \
  --refresh-seconds 15
```

用途：在本机浏览器打开 `http://127.0.0.1:8765/`，通过只读 API payload 持续查看当前模拟盘状态；该入口不暴露下单、runtime start/stop 或配置变更写接口。

## 6. 主要产物与数据位置

1. 模拟账本数据库：`.cache/paper_trade/paper.db`
2. 告警日志：`.cache/paper_trade/alerts.jsonl`
3. 运行日志：`.cache/paper_trade/run_logs.jsonl`
4. 实时循环日志：`.cache/paper_trade/realtime_logs.jsonl`
5. 配置示例：`skills/deep-analysis/scripts/papertrade/config.example.json`
6. 静态盯盘仪表盘：`.cache/paper_trade/dashboard.html`
7. 本地只读 dashboard：`http://127.0.0.1:8765/`（仅在 `--serve` 运行时存在）

数据库核心表：

1. `signals`
2. `paper_orders`
3. `paper_fills`
4. `paper_positions`
5. `paper_nav_daily`
6. `alerts`
7. `human_actions`
8. `live_mirror_trades`
9. `deviation_daily`
10. `job_runs`
11. `runtime_sessions`
12. `runtime_loops`
13. `event_outbox`
14. `position_lots`
15. `lot_allocations`
16. `portfolio_nav_snapshots`
17. `system_config_history`

## 7. 与原报告系统的关系

两者是并行增强，不冲突：

1. 原有报告链路 `run.py / stage1 / stage2` 保持可用。
2. `papertrade` 只是额外消费报告链输出，形成模拟交易闭环。
3. 未启用 `papertrade` 时，原分析报告流程不受影响。

## 8. 改进空间与优先级

### P0（建议优先）

1. 增加交易日历与节假日识别，替代当前简单 `+1 day` 的 T+1 日期逻辑。
2. 加入“真实成交回填 CLI”，减少手工写库。
3. 增加订单执行模型（部分成交、开盘滑点场景化、涨跌停不可成交细化）。
4. 增加运行超时与重试策略，避免分析环节长时间挂起。

### P1（中期增强）

1. 新增 `report_daily.py` 生成每日摘要（建议、成交、净值、偏差归因）。
2. 增加组合级风险控制（行业暴露、最大回撤熔断、连亏熔断）。
3. 新增策略参数回放面板，支持阈值敏感性分析。
4. 将 webhook 扩展为飞书/企业微信/钉钉模板消息。

### P2（长期演进）

1. 对接真实券商仿真环境（先仿真通道，后真实通道）。
2. 引入事件驱动调度器，覆盖盘前/盘中/收盘三段自动化。
3. 引入更完整审计链路（谁确认、何时确认、确认后结果）。

## 9. 建议的落地节奏

1. 第 1 周：固定 `from-cache-only + dry-run` 观察决策稳定性。
2. 第 2 周：开启模拟成交，验证净值与仓位账本一致性。
3. 第 3 周：接入人工回填，开始偏差统计。
4. 第 4 周：固化日报模板与风控阈值，形成日常运行 SOP。

## 10. 结论

`papertrade` 已具备“可运行的模拟盘自动化骨架”，能够把 UZI 现有的分析、51 评委、策略融合、可解释输出真正接入交易决策与账本闭环。

当前最合理使用方式是：

1. 系统自动分析与模拟执行。
2. 人工确认是否在真实账户执行。
3. 系统长期统计“模拟 vs 人工”偏差并反哺策略与流程。

这条路径与现有系统能力边界一致，工程风险可控，且可持续演进。
