# Papertrade: Sim Auto + Human Confirm

## Goal

Use existing UZI analysis strengths for a production-friendly loop:

1. system auto-runs analysis and paper decisions,
2. system maintains virtual orders/positions/NAV,
3. human decides whether to mirror in real account,
4. system tracks paper-vs-live deviation.

## Entry Command

```bash
python3 skills/deep-analysis/scripts/papertrade/run_cycle.py \
  --tickers 600519.SH,002273.SZ \
  --depth medium \
  --config skills/deep-analysis/scripts/papertrade/config.example.json
```

Dry run (only signals + alerts, no paper fills):

```bash
python3 skills/deep-analysis/scripts/papertrade/run_cycle.py \
  --tickers 600519.SH \
  --depth lite \
  --dry-run
```

Cache-only run (no stage1/stage2 refresh):

```bash
python3 skills/deep-analysis/scripts/papertrade/run_cycle.py \
  --tickers 600519.SH \
  --from-cache-only \
  --dry-run
```

Realtime simulated loop (watchers + quote overlay):

```bash
python3 skills/deep-analysis/scripts/papertrade/run_realtime.py \
  --tickers 600519.SH,002273.SZ \
  --depth medium \
  --poll-seconds 60 \
  --refresh-every-loops 5 \
  --watcher-overlay \
  --quote-overlay
```

## Decision Inputs

- `panel.json`: consensus, signal distribution, investor-group opinions.
- `strategy_signals.json`: 12-family signal strengths/confidence/regime fit.
- `synthesis.json`: overall score/verdict, short-trading module, exit triggers.
- `_review_issues.json`: data-quality gate.

## Decision Outputs

- `PAPER_BUY_A`
- `PAPER_WATCH_B`
- `OBSERVE`
- `AVOID`

For open positions, score drops below `force_exit` can trigger simulated sell (T+1 aware).

## Storage

SQLite: `.cache/paper_trade/paper.db`

Core tables:

- `signals`
- `paper_orders`
- `paper_fills`
- `paper_positions`
- `paper_nav_daily`
- `alerts`
- `human_actions`
- `live_mirror_trades`
- `deviation_daily`

Alerts JSONL: `.cache/paper_trade/alerts.jsonl`

## Notes

- This layer does not place real broker orders.
- It is designed for "auto paper + human confirm real execution".
- Intraday quality and data-gap gates are enabled before A-level actions.
- Realtime mode adds simulated watcher personas (trend / risk / mean-revert) and logs each loop to `.cache/paper_trade/realtime_logs.jsonl`.
