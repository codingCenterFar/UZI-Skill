from __future__ import annotations

import sqlite3


def init_schema_v2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_sessions (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            start_request_id TEXT,
            owner_pid INTEGER,
            owner_host TEXT,
            config_hash TEXT,
            config_json TEXT,
            started_at_ms INTEGER NOT NULL,
            heartbeat_at_ms INTEGER NOT NULL,
            ended_at_ms INTEGER,
            last_loop_id TEXT,
            stop_reason TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uniq_runtime_sessions_start_request
            ON runtime_sessions(start_request_id)
            WHERE start_request_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_runtime_sessions_status_updated_at
            ON runtime_sessions(status, updated_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_runtime_sessions_heartbeat
            ON runtime_sessions(heartbeat_at_ms DESC);

        CREATE TABLE IF NOT EXISTS runtime_leases (
            lease_name TEXT PRIMARY KEY,
            owner_session_id TEXT NOT NULL,
            owner_pid INTEGER,
            owner_host TEXT,
            heartbeat_at_ms INTEGER NOT NULL,
            lease_expires_at_ms INTEGER NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_leases_expiry
            ON runtime_leases(lease_expires_at_ms);

        CREATE TABLE IF NOT EXISTS runtime_loops (
            loop_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            loop_no INTEGER NOT NULL,
            status TEXT NOT NULL,
            snapshot_ts_ms INTEGER NOT NULL,
            decision_version TEXT,
            event_batch_id TEXT,
            decision_basis_summary_json TEXT,
            staleness_summary_json TEXT,
            ticker_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            started_at_ms INTEGER NOT NULL,
            ended_at_ms INTEGER,
            duration_ms INTEGER,
            db_write_duration_ms INTEGER,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT,
            retry_of_loop_id TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(session_id, loop_no)
        );

        CREATE INDEX IF NOT EXISTS idx_runtime_loops_session_no
            ON runtime_loops(session_id, loop_no DESC);
        CREATE INDEX IF NOT EXISTS idx_runtime_loops_status_started
            ON runtime_loops(status, started_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_runtime_loops_event_batch
            ON runtime_loops(event_batch_id);

        CREATE TABLE IF NOT EXISTS analysis_snapshots (
            analysis_snapshot_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            basis TEXT NOT NULL,
            analysis_version TEXT,
            source_cache_key TEXT,
            raw_ref_path TEXT,
            features_json TEXT NOT NULL,
            synthesis_json TEXT NOT NULL,
            source_data_ts_ms INTEGER,
            generated_at_ms INTEGER NOT NULL,
            analysis_age_ms INTEGER NOT NULL,
            staleness_flags_json TEXT,
            snapshot_hash TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_snapshots_ticker_time
            ON analysis_snapshots(ticker, generated_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_analysis_snapshots_hash
            ON analysis_snapshots(snapshot_hash);

        CREATE TABLE IF NOT EXISTS watcher_overlay_snapshots (
            watcher_overlay_snapshot_id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            watcher_version TEXT,
            score_before REAL NOT NULL,
            score_delta REAL NOT NULL,
            score_after REAL NOT NULL,
            action_before TEXT,
            action_after TEXT,
            votes_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_watcher_overlay_loop_ticker
            ON watcher_overlay_snapshots(loop_id, ticker);
        CREATE INDEX IF NOT EXISTS idx_watcher_overlay_ticker_time
            ON watcher_overlay_snapshots(ticker, created_at_ms DESC);

        CREATE TABLE IF NOT EXISTS decision_snapshots (
            decision_snapshot_id TEXT PRIMARY KEY,
            loop_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            analysis_snapshot_id TEXT,
            watcher_overlay_snapshot_id TEXT,
            decision_basis TEXT NOT NULL,
            decision_version TEXT,
            policy_version TEXT,
            analysis_age_ms INTEGER NOT NULL DEFAULT 0,
            quote_age_ms INTEGER NOT NULL DEFAULT 0,
            quote_snapshot_ts_ms INTEGER,
            staleness_flags_json TEXT,
            score_strategy REAL,
            score_panel REAL,
            score_tactical REAL,
            score_core REAL,
            score_final_before_watcher REAL,
            score_final REAL,
            action_before_watcher TEXT,
            action TEXT,
            penalties_json TEXT,
            gates_json TEXT,
            summary_json TEXT,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(loop_id, ticker)
        );

        CREATE INDEX IF NOT EXISTS idx_decision_snapshots_ticker_time
            ON decision_snapshots(ticker, created_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_snapshots_basis_time
            ON decision_snapshots(decision_basis, created_at_ms DESC);

        CREATE TABLE IF NOT EXISTS rule_check_snapshots (
            rule_check_snapshot_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            order_type TEXT NOT NULL,
            limit_price REAL,
            market_phase TEXT,
            trade_date TEXT,
            calendar_version TEXT,
            instrument_version TEXT,
            rule_engine_version TEXT,
            decision_basis TEXT,
            analysis_age_ms INTEGER NOT NULL DEFAULT 0,
            quote_age_ms INTEGER NOT NULL DEFAULT 0,
            allow INTEGER NOT NULL,
            reject_code TEXT,
            reject_reason TEXT,
            checks_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_rule_check_snapshots_ticker_time
            ON rule_check_snapshots(ticker, created_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_rule_check_snapshots_allow_time
            ON rule_check_snapshots(allow, created_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_rule_check_snapshots_reject_code
            ON rule_check_snapshots(reject_code, created_at_ms DESC);

        CREATE TABLE IF NOT EXISTS order_intents (
            intent_id TEXT PRIMARY KEY,
            signal_id INTEGER,
            loop_id TEXT,
            source TEXT NOT NULL,
            operator_id TEXT,
            operator_channel TEXT,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            order_type TEXT NOT NULL,
            limit_price REAL,
            time_in_force TEXT,
            status TEXT NOT NULL,
            decision_snapshot_id TEXT,
            rule_check_snapshot_id TEXT,
            latest_order_id TEXT,
            reject_code TEXT,
            reject_reason TEXT,
            expires_at_ms INTEGER,
            correlation_id TEXT NOT NULL,
            causation_id TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(source, idempotency_key)
        );

        CREATE INDEX IF NOT EXISTS idx_order_intents_status_created
            ON order_intents(status, created_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_order_intents_ticker_status
            ON order_intents(ticker, status, created_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_order_intents_loop
            ON order_intents(loop_id, created_at_ms);
        CREATE INDEX IF NOT EXISTS idx_order_intents_correlation
            ON order_intents(correlation_id);

        CREATE TABLE IF NOT EXISTS order_rule_checks (
            check_id TEXT PRIMARY KEY,
            intent_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            rule_check_snapshot_id TEXT NOT NULL,
            result TEXT NOT NULL,
            reject_code TEXT,
            reject_reason TEXT,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            checked_at_ms INTEGER NOT NULL,
            UNIQUE(intent_id, attempt_no)
        );

        CREATE INDEX IF NOT EXISTS idx_order_rule_checks_intent
            ON order_rule_checks(intent_id, attempt_no DESC);
        CREATE INDEX IF NOT EXISTS idx_order_rule_checks_result_time
            ON order_rule_checks(result, checked_at_ms DESC);

        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            intent_id TEXT NOT NULL,
            order_seq INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            order_type TEXT NOT NULL,
            limit_price REAL,
            status TEXT NOT NULL,
            submitted_at_ms INTEGER NOT NULL,
            last_update_at_ms INTEGER NOT NULL,
            filled_qty INTEGER NOT NULL DEFAULT 0,
            avg_fill_price REAL,
            reject_code TEXT,
            reject_reason TEXT,
            loop_id TEXT,
            correlation_id TEXT NOT NULL,
            causation_id TEXT,
            UNIQUE(intent_id, order_seq)
        );

        CREATE INDEX IF NOT EXISTS idx_orders_status_submitted
            ON orders(status, submitted_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_ticker_time
            ON orders(ticker, submitted_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_intent
            ON orders(intent_id);

        CREATE TABLE IF NOT EXISTS fills (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            intent_id TEXT NOT NULL,
            fill_seq INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price REAL NOT NULL,
            fee REAL NOT NULL DEFAULT 0,
            tax REAL NOT NULL DEFAULT 0,
            slippage_bps REAL NOT NULL DEFAULT 0,
            fill_value REAL NOT NULL,
            fill_ts_ms INTEGER NOT NULL,
            loop_id TEXT,
            correlation_id TEXT NOT NULL,
            causation_id TEXT,
            UNIQUE(order_id, fill_seq)
        );

        CREATE INDEX IF NOT EXISTS idx_fills_ticker_time
            ON fills(ticker, fill_ts_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_fills_intent
            ON fills(intent_id, fill_seq);
        CREATE INDEX IF NOT EXISTS idx_fills_order
            ON fills(order_id, fill_seq);

        CREATE TABLE IF NOT EXISTS position_lots (
            lot_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            buy_fill_id TEXT NOT NULL,
            buy_intent_id TEXT NOT NULL,
            open_qty INTEGER NOT NULL,
            remaining_qty INTEGER NOT NULL,
            open_price REAL NOT NULL,
            open_fee_alloc REAL NOT NULL DEFAULT 0,
            open_tax_alloc REAL NOT NULL DEFAULT 0,
            open_trade_date TEXT NOT NULL,
            t1_sellable_date TEXT NOT NULL,
            status TEXT NOT NULL,
            opened_at_ms INTEGER NOT NULL,
            closed_at_ms INTEGER,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_position_lots_ticker_status
            ON position_lots(ticker, status, t1_sellable_date);
        CREATE INDEX IF NOT EXISTS idx_position_lots_buy_fill
            ON position_lots(buy_fill_id);

        CREATE TABLE IF NOT EXISTS lot_allocations (
            allocation_id TEXT PRIMARY KEY,
            sell_fill_id TEXT NOT NULL,
            lot_id TEXT NOT NULL,
            qty INTEGER NOT NULL,
            open_price REAL NOT NULL,
            close_price REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(sell_fill_id, lot_id)
        );

        CREATE INDEX IF NOT EXISTS idx_lot_allocations_sell_fill
            ON lot_allocations(sell_fill_id);
        CREATE INDEX IF NOT EXISTS idx_lot_allocations_lot
            ON lot_allocations(lot_id);

        CREATE TABLE IF NOT EXISTS positions (
            ticker TEXT PRIMARY KEY,
            qty INTEGER NOT NULL,
            sellable_qty INTEGER NOT NULL,
            avg_cost REAL NOT NULL,
            last_price REAL NOT NULL,
            market_value REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            realized_pnl_cum REAL NOT NULL,
            lot_count_open INTEGER NOT NULL DEFAULT 0,
            source_lot_watermark INTEGER,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_positions_market_value
            ON positions(market_value DESC);
        CREATE INDEX IF NOT EXISTS idx_positions_updated
            ON positions(updated_at_ms DESC);

        CREATE TABLE IF NOT EXISTS portfolio_nav_snapshots (
            nav_snapshot_id TEXT PRIMARY KEY,
            as_of_date TEXT NOT NULL,
            as_of_ts_ms INTEGER NOT NULL,
            session_id TEXT,
            loop_id TEXT,
            cash REAL NOT NULL,
            position_market_value REAL NOT NULL,
            equity REAL NOT NULL,
            cumulative_return_pct REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            cash_drift_check REAL NOT NULL DEFAULT 0,
            equity_recompute_diff REAL NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(loop_id)
        );

        CREATE INDEX IF NOT EXISTS idx_nav_snapshots_date_ts
            ON portfolio_nav_snapshots(as_of_date DESC, as_of_ts_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_nav_snapshots_session
            ON portfolio_nav_snapshots(session_id, as_of_ts_ms DESC);

        CREATE TABLE IF NOT EXISTS event_outbox (
            event_id TEXT PRIMARY KEY,
            event_batch_id TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            source TEXT NOT NULL,
            severity TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id TEXT,
            payload_version INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            publish_attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at_ms INTEGER,
            last_error TEXT,
            created_at_ms INTEGER NOT NULL,
            published_at_ms INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_event_outbox_status_retry
            ON event_outbox(status, next_retry_at_ms, created_at_ms);
        CREATE INDEX IF NOT EXISTS idx_event_outbox_correlation
            ON event_outbox(correlation_id);
        CREATE INDEX IF NOT EXISTS idx_event_outbox_entity
            ON event_outbox(entity_type, entity_id, created_at_ms DESC);

        CREATE TABLE IF NOT EXISTS watchlists (
            watchlist_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            symbols_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(name)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uniq_watchlists_default
            ON watchlists(is_default)
            WHERE is_default = 1;
        CREATE INDEX IF NOT EXISTS idx_watchlists_status_updated
            ON watchlists(status, updated_at_ms DESC);

        CREATE TABLE IF NOT EXISTS candidate_pool (
            candidate_batch_id TEXT PRIMARY KEY,
            pool_name TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            as_of_date TEXT,
            universe_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            generated_at_ms INTEGER NOT NULL,
            archived_at_ms INTEGER,
            archive_reason TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_pool_name_status_time
            ON candidate_pool(pool_name, status, generated_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_pool_status_time
            ON candidate_pool(status, generated_at_ms DESC);

        CREATE TABLE IF NOT EXISTS candidate_pool_items (
            candidate_item_id TEXT PRIMARY KEY,
            candidate_batch_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT,
            name TEXT,
            candidate_score REAL NOT NULL,
            action_state TEXT NOT NULL,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            source_tags_json TEXT NOT NULL DEFAULT '[]',
            staleness_json TEXT NOT NULL DEFAULT '{}',
            analysis_age_ms INTEGER NOT NULL DEFAULT 0,
            quote_age_ms INTEGER NOT NULL DEFAULT 0,
            quote_ts_ms INTEGER,
            analysis_snapshot_id TEXT,
            decision_snapshot_id TEXT,
            signal_id INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(candidate_batch_id, ticker),
            UNIQUE(candidate_batch_id, rank)
        );

        CREATE INDEX IF NOT EXISTS idx_candidate_pool_items_batch_rank
            ON candidate_pool_items(candidate_batch_id, rank ASC);
        CREATE INDEX IF NOT EXISTS idx_candidate_pool_items_ticker_time
            ON candidate_pool_items(ticker, updated_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_candidate_pool_items_score
            ON candidate_pool_items(candidate_score DESC);

        CREATE TABLE IF NOT EXISTS quote_snapshots (
            quote_snapshot_id TEXT PRIMARY KEY,
            quote_batch_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT,
            name TEXT,
            price REAL,
            change_pct REAL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            quote_ts_ms INTEGER,
            quote_age_ms INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_reason TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            request_context_json TEXT NOT NULL DEFAULT '{}',
            created_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_quote_snapshots_ticker_time
            ON quote_snapshots(ticker, created_at_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_quote_snapshots_batch
            ON quote_snapshots(quote_batch_id, ticker);
        CREATE INDEX IF NOT EXISTS idx_quote_snapshots_status_time
            ON quote_snapshots(status, created_at_ms DESC);

        CREATE TABLE IF NOT EXISTS system_config_history (
            config_change_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            config_json TEXT NOT NULL,
            changed_by TEXT,
            change_reason TEXT,
            effective_from_ms INTEGER NOT NULL,
            correlation_id TEXT,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(scope, config_hash, effective_from_ms)
        );

        CREATE INDEX IF NOT EXISTS idx_system_config_scope_time
            ON system_config_history(scope, effective_from_ms DESC);
        """
    )
    _ensure_column(conn, "runtime_loops", "metrics_json", "TEXT NOT NULL DEFAULT '{}'")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row[1]) for row in rows}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
