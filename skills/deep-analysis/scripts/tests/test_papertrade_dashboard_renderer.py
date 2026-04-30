from __future__ import annotations

import json
import sqlite3
import sys
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.api_common import now_ms  # noqa: E402
from papertrade.candidate_pool import create_candidate_pool_batch  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.dashboard_renderer import build_dashboard_payload, create_dashboard_server, render_dashboard_html, write_dashboard_html  # noqa: E402
from papertrade.ledger import init_db  # noqa: E402
from papertrade.quote_snapshots import refresh_quote_snapshots  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ts_ms(dt_s: str) -> int:
    return int(datetime.fromisoformat(dt_s).timestamp() * 1000)


def _seed_position(conn: sqlite3.Connection) -> None:
    cfg = load_config()
    res = api.post_sim_order(
        conn,
        cfg=cfg,
        payload={
            "ticker": "600519.SH",
            "side": "BUY",
            "qty": 100,
            "order_type": "LIMIT",
            "limit_price": 100.0,
            "trade_date": "2026-04-23",
            "request_ts_ms": _ts_ms("2026-04-23T10:00:00"),
            "client_order_id": "dashboard-buy-001",
            "operator": {"operator_id": "tester", "channel": "pytest"},
        },
        idempotency_key="dashboard-buy-001",
    )
    assert res["ok"] is True


def _seed_candidate_view(conn: sqlite3.Connection) -> None:
    create_candidate_pool_batch(
        conn,
        candidate_batch_id="cpb_dashboard_001",
        pool_name="default",
        source="pytest",
        as_of_date="2026-04-26",
        generated_at_ms=1_000,
        items=[
            {
                "ticker": "600519.SH",
                "name": "Moutai",
                "candidate_score": 89.0,
                "action_state": "CANDIDATE_A",
                "reasons": ["position_candidate", "agent_reviewed"],
                "analysis_age_ms": 1_200,
                "metadata": {"trigger_price": 98.5},
            },
            {
                "ticker": "002361.SZ",
                "name": "Shenjian",
                "candidate_score": 61.0,
                "action_state": "PULLBACK_WAIT",
                "reasons": ["wait_pullback"],
                "analysis_age_ms": 2_400,
            },
        ],
    )

    def provider(ticker: str) -> dict:
        if ticker == "002361.SZ":
            return {
                "ok": False,
                "ticker": ticker,
                "source": "pytest_provider",
                "ts_ms": now_ms() - 2_000,
                "error_code": "QUOTE_DOWN",
                "reason": "quote unavailable",
            }
        return {
            "ok": True,
            "ticker": ticker,
            "source": "pytest_provider",
            "price": 101.2,
            "change_pct": 0.8,
            "ts_ms": now_ms() - 1_000,
        }

    refresh_quote_snapshots(
        conn,
        pool_name="default",
        provider=provider,
        source="pytest_provider",
        quote_batch_id="qbatch_dashboard_001",
    )
    conn.commit()


def test_dashboard_candidate_payload_enriches_pool_with_quote_and_position_state():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_position(conn)
    _seed_candidate_view(conn)

    payload = build_dashboard_payload(conn)
    items = {item["ticker"]: item for item in payload["candidates"]["items"]}

    assert payload["candidates"]["item_count"] == 2
    assert items["600519.SH"]["bucket"] == "POSITION"
    assert items["600519.SH"]["trigger_price"] == 98.5
    assert items["600519.SH"]["quote_status"] == "ok"
    assert items["002361.SZ"]["bucket"] == "WAIT"
    assert items["002361.SZ"]["quote_status"] == "failed"
    assert "quote_failed" in items["002361.SZ"]["freshness_flags"]

    routed = api.handle_request(
        conn,
        cfg=cfg,
        method="GET",
        path="/api/v1/dashboard/candidates",
        query={"limit": 2},
    )
    assert routed["ok"] is True
    assert routed["data"]["buckets"]["POSITION"] == 1
    assert routed["data"]["freshness"]["quote_failed_count"] == 1


def test_render_dashboard_html_contains_operator_workbench_sections():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_position(conn)
    _seed_candidate_view(conn)

    html = render_dashboard_html(conn, title="Pytest Papertrade Desk", generated_at="2026-04-25T15:00:00")

    assert "<!doctype html>" in html
    assert "Pytest Papertrade Desk" in html
    assert 'lang="zh-CN"' in html
    assert "观察列表" in html
    assert "持仓" in html
    assert "订单" in html
    assert "事件" in html
    assert "运行时" in html
    assert "候选池" in html
    assert "等回踩" in html
    assert "等待回踩" in html
    assert "行情失败" in html
    assert "PULLBACK_WAIT" not in html
    assert "wait_pullback" not in html
    assert "quote_failed" not in html
    assert "600519.SH" in html
    assert "dashboard-buy-001" not in html
    assert "dashboard-buy" not in html


def test_render_dashboard_html_supports_english_language_option():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_position(conn)
    _seed_candidate_view(conn)

    html = render_dashboard_html(
        conn,
        title="Pytest Papertrade Desk",
        generated_at="2026-04-25T15:00:00",
        lang="en",
    )

    assert 'lang="en"' in html
    assert "Watchlist" in html
    assert "Positions" in html
    assert "Orders" in html
    assert "Events" in html
    assert "Runtime" in html
    assert "Candidate Pool" in html
    assert "PULLBACK_WAIT" in html
    assert "wait_pullback" in html
    assert "quote_failed" in html


def test_render_dashboard_html_supports_auto_refresh_marker():
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_position(conn)
    _seed_candidate_view(conn)

    html = render_dashboard_html(
        conn,
        title="Pytest Papertrade Desk",
        generated_at="2026-04-25T15:00:00",
        refresh_seconds=7,
    )

    assert 'http-equiv="refresh" content="7"' in html
    assert "自动刷新 7s" in html


def test_write_dashboard_html_writes_static_file(tmp_path: Path):
    conn = _conn()
    cfg = load_config()
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_position(conn)
    _seed_candidate_view(conn)

    output = write_dashboard_html(conn, tmp_path / "papertrade-dashboard.html", title="Desk Export")

    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "Desk Export" in content
    assert "候选池" in content
    assert "600519.SH" in content


def test_readonly_dashboard_http_server_serves_html_and_payload(tmp_path: Path):
    cfg = load_config()
    db_path = tmp_path / "paper.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn, initial_cash=cfg.trade.initial_cash)
    _seed_position(conn)
    _seed_candidate_view(conn)
    conn.close()

    server = create_dashboard_server(
        db_path=db_path,
        initial_cash=cfg.trade.initial_cash,
        host="127.0.0.1",
        port=0,
        title="HTTP Desk",
        refresh_seconds=5,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"
    try:
        html_body = urllib.request.urlopen(f"{base_url}/", timeout=2).read().decode("utf-8")
        payload_raw = urllib.request.urlopen(f"{base_url}/api/dashboard/payload", timeout=2).read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    payload = json.loads(payload_raw)
    assert "HTTP Desk" in html_body
    assert 'http-equiv="refresh" content="5"' in html_body
    assert payload["ok"] is True
    assert payload["data"]["positions"]["items"][0]["ticker"] == "600519.SH"
    assert payload["data"]["candidates"]["items"][0]["ticker"] == "600519.SH"
