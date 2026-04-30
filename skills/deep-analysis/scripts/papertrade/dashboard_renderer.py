from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from papertrade import api  # noqa: E402
from papertrade.config import load_config  # noqa: E402
from papertrade.ledger import connect, init_db  # noqa: E402


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "-"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def _api_data(response: dict[str, Any], default: Any) -> Any:
    if response.get("ok"):
        return response.get("data", default)
    return default


def _api_error(response: dict[str, Any]) -> dict[str, Any] | None:
    if response.get("ok"):
        return None
    err = response.get("error")
    return err if isinstance(err, dict) else {"message": str(err)}


def build_dashboard_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    dashboard = api.get_dashboard_summary(conn)
    candidates = api.get_dashboard_candidates(conn, limit=20)
    health = api.get_health(conn)
    runtime = api.get_runtime_status(conn)
    return {
        "dashboard": _api_data(dashboard, {}),
        "dashboard_error": _api_error(dashboard),
        "candidates": _api_data(candidates, {"items": [], "buckets": {}, "freshness": {}}),
        "candidates_error": _api_error(candidates),
        "watchlist": _api_data(api.get_watchlist(conn, limit=100), {"items": []}),
        "positions": _api_data(api.get_positions(conn), {"items": []}),
        "orders": _api_data(api.get_orders(conn, status="all", limit=50), {"items": []}),
        "events": _api_data(api.get_events(conn, limit=80, include_pending=True), {"events": []}),
        "metrics": _api_data(api.get_metrics(conn, limit=100), {}),
        "health": _api_data(health, {}),
        "health_error": _api_error(health),
        "runtime": _api_data(runtime, {}),
        "runtime_error": _api_error(runtime),
    }


def _state_class(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"ok", "running", "completed", "filled", "published", "paper_buy_a", "candidate_a", "breakout_wait", "position", "buy_ready"}:
        return "good"
    if text in {"warning", "queued", "partial", "paper_watch_b", "observe", "pullback_wait", "wait"}:
        return "warn"
    if text in {"error", "failed", "rejected", "dead", "avoid", "no_trade_avoid"}:
        return "bad"
    return "neutral"


def _badge(value: Any) -> str:
    text = _esc(value or "-")
    return f'<span class="badge {_state_class(value)}">{text}</span>'


def _kv(label: str, value: str, tone: str = "neutral") -> str:
    return f"""
        <div class="metric {tone}">
          <span>{_esc(label)}</span>
          <strong>{value}</strong>
        </div>
    """


def _empty_row(colspan: int, text: str) -> str:
    return f'<tr class="empty"><td colspan="{colspan}">{_esc(text)}</td></tr>'


def _render_watchlist(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td>{_badge(item.get("action"))}</td>
              <td class="num">{_num(item.get("score_final"), 1)}</td>
              <td class="num">{_num(item.get("last_price"), 2)}</td>
              <td class="num">{_pct(item.get("change_pct"))}</td>
              <td>{_esc(item.get("decision_basis"))}</td>
              <td class="num">{_esc(item.get("analysis_age_ms"))}</td>
              <td>{_esc(",".join(item.get("staleness_flags") or []))}</td>
            </tr>
            """
        )
    if not rows:
        rows.append(_empty_row(8, "暂无 watchlist 标的"))
    return "\n".join(rows)


def _render_candidates(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        reasons = item.get("reasons") or []
        flags = item.get("freshness_flags") or []
        rows.append(
            f"""
            <tr>
              <td class="num">{_esc(item.get("rank"))}</td>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td>{_badge(item.get("bucket"))}</td>
              <td>{_badge(item.get("action_state"))}</td>
              <td class="num">{_num(item.get("candidate_score"), 1)}</td>
              <td class="num">{_num(item.get("trigger_price"), 3)}</td>
              <td>{_badge(item.get("quote_status"))}</td>
              <td class="num">{_num(item.get("quote_price"), 3)}</td>
              <td class="num">{_pct(item.get("quote_change_pct"))}</td>
              <td class="num">{_esc(item.get("analysis_age_ms"))}</td>
              <td class="num">{_esc(item.get("quote_age_ms"))}</td>
              <td>{_esc(", ".join(str(x) for x in reasons[:3]))}</td>
              <td>{_esc(", ".join(str(x) for x in flags))}</td>
            </tr>
            """
        )
    if not rows:
        rows.append(_empty_row(13, "暂无候选池"))
    return "\n".join(rows)


def _render_positions(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td class="num">{_esc(item.get("qty"))}</td>
              <td class="num">{_esc(item.get("sellable_qty"))}</td>
              <td class="num">{_num(item.get("avg_cost"), 3)}</td>
              <td class="num">{_num(item.get("last_price"), 3)}</td>
              <td class="num">{_num(item.get("market_value"), 2)}</td>
              <td class="num">{_num(item.get("unrealized_pnl"), 2)}</td>
              <td class="num">{_esc(item.get("lot_count_open"))}</td>
            </tr>
            """
        )
    if not rows:
        rows.append(_empty_row(8, "暂无持仓"))
    return "\n".join(rows)


def _render_orders(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td class="mono">{_esc(item.get("intent_id"))}</td>
              <td>{_badge(item.get("status"))}</td>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td>{_esc(item.get("side"))}</td>
              <td class="num">{_esc(item.get("qty"))}</td>
              <td class="num">{_esc(item.get("filled_qty"))}</td>
              <td class="num">{_num(item.get("avg_fill_price"), 3)}</td>
              <td>{_esc(item.get("source"))}</td>
            </tr>
            """
        )
    if not rows:
        rows.append(_empty_row(8, "暂无订单"))
    return "\n".join(rows)


def _render_events(events: list[dict[str, Any]]) -> str:
    rows = []
    for event in events[:40]:
        payload = event.get("payload")
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True) if isinstance(payload, dict) else str(payload or "")
        rows.append(
            f"""
            <li>
              <span>{_badge(event.get("event_type"))}</span>
              <code>{_esc(event.get("entity_type"))}:{_esc(event.get("entity_id"))}</code>
              <em>{_esc(event.get("status"))}</em>
              <small>{_esc(payload_text[:180])}</small>
            </li>
            """
        )
    if not rows:
        rows.append("<li class=\"empty-line\">暂无事件</li>")
    return "\n".join(rows)


def render_dashboard_html(
    conn: sqlite3.Connection,
    *,
    title: str = "Papertrade Realtime Desk",
    generated_at: str | None = None,
    refresh_seconds: int | None = None,
) -> str:
    payload = build_dashboard_payload(conn)
    dashboard = payload["dashboard"]
    metrics = payload["metrics"]
    health = payload["health"]
    runtime = payload["runtime"]
    positions = payload["positions"].get("items", [])
    candidates = payload["candidates"].get("items", [])
    candidate_buckets = payload["candidates"].get("buckets", {})
    candidate_freshness = payload["candidates"].get("freshness", {})
    watchlist = payload["watchlist"].get("items", [])
    orders = payload["orders"].get("items", [])
    events = payload["events"].get("events", [])
    generated = generated_at or datetime.now().isoformat(timespec="seconds")

    runtime_status = ((runtime.get("runtime") or {}).get("status")) or "stopped"
    health_status = health.get("status") or ("unknown" if payload.get("health_error") else "ok")
    freshness = dashboard.get("freshness") or {}
    runtime_summary = dashboard.get("runtime") or {}
    refresh_s = int(refresh_seconds or 0)
    refresh_meta = f'\n  <meta http-equiv="refresh" content="{refresh_s}" />' if refresh_s > 0 else ""
    refresh_note = f" · auto refresh {refresh_s}s" if refresh_s > 0 else ""

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  {refresh_meta}
  <title>{_esc(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #121210;
      --panel: #191917;
      --panel-soft: #22211d;
      --line: #343229;
      --text: #f1efe8;
      --muted: #a8a095;
      --good: #43c48b;
      --warn: #e1b14d;
      --bad: #e26363;
      --accent: #76a9d8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 22px 28px;
      border-bottom: 1px solid var(--line);
      background: #151511;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
    h2 {{ margin: 0 0 12px; font-size: 15px; font-weight: 700; }}
    .sub {{ color: var(--muted); margin-top: 4px; }}
    .actions {{ display: flex; gap: 8px; }}
    button {{
      border: 1px solid var(--line);
      background: var(--panel-soft);
      color: var(--text);
      border-radius: 6px;
      padding: 8px 11px;
      cursor: pointer;
    }}
    button:hover {{ border-color: var(--accent); }}
    main {{ padding: 24px 28px 36px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 22px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 12px 13px;
      min-height: 78px;
    }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 7px; font-size: 22px; line-height: 1.1; }}
    .metric.good strong {{ color: var(--good); }}
    .metric.warn strong {{ color: var(--warn); }}
    .metric.bad strong {{ color: var(--bad); }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.55fr) minmax(320px, .9fr);
      gap: 18px;
      align-items: start;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 18px;
      overflow: hidden;
    }}
    section > h2 {{ padding: 14px 16px; border-bottom: 1px solid var(--line); background: #151511; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 650; background: #151511; }}
    tr:hover td {{ background: #201f1a; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      white-space: nowrap;
      font-size: 12px;
    }}
    .badge.good {{ color: var(--good); border-color: rgba(67, 196, 139, .35); }}
    .badge.warn {{ color: var(--warn); border-color: rgba(225, 177, 77, .35); }}
    .badge.bad {{ color: var(--bad); border-color: rgba(226, 99, 99, .35); }}
    .events {{ list-style: none; margin: 0; padding: 6px 0; }}
    .events li {{
      display: grid;
      grid-template-columns: 150px 1fr 88px;
      gap: 10px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
    }}
    .events small {{ grid-column: 1 / -1; color: var(--muted); overflow-wrap: anywhere; }}
    .events em {{ color: var(--muted); font-style: normal; }}
    .empty td, .empty-line {{ color: var(--muted); padding: 18px; }}
    @media (max-width: 980px) {{
      header {{ position: static; align-items: flex-start; flex-direction: column; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .layout {{ grid-template-columns: 1fr; }}
      section {{ overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{_esc(title)}</h1>
      <div class="sub">generated {_esc(generated)} · runtime {_badge(runtime_status)} · health {_badge(health_status)}{_esc(refresh_note)}</div>
    </div>
    <div class="actions">
      <button type="button" onclick="location.reload()">Refresh</button>
      <button type="button" onclick="window.print()">Print</button>
    </div>
  </header>
  <main>
    <div class="metrics">
      {_kv("Equity", _num(dashboard.get("equity"), 2), "good")}
      {_kv("Cash", _num(dashboard.get("cash"), 2))}
      {_kv("Market Value", _num(dashboard.get("position_market_value"), 2))}
      {_kv("Return", _pct(dashboard.get("cumulative_return_pct")), "good" if _num(dashboard.get("cumulative_return_pct")) != "-" else "neutral")}
      {_kv("Drawdown", _pct(dashboard.get("drawdown_pct")), "bad" if _f(dashboard.get("drawdown_pct")) < 0 else "neutral")}
      {_kv("Event Lag", _esc(freshness.get("event_queue_lag", 0)), "warn" if _f(freshness.get("event_queue_lag")) > 0 else "neutral")}
    </div>
    <div class="metrics">
      {_kv("Quote Freshness", _esc(metrics.get("quote_freshness_ms", freshness.get("quote_freshness_ms", 0))) + " ms")}
      {_kv("Analysis Freshness", _esc(metrics.get("analysis_freshness_ms", freshness.get("analysis_freshness_ms", 0))) + " ms")}
      {_kv("Candidates", _esc(payload["candidates"].get("item_count", 0)))}
      {_kv("Wait / Avoid", _esc(candidate_buckets.get("WAIT", 0)) + " / " + _esc(candidate_buckets.get("AVOID", 0)))}
      {_kv("Quote Risk", _esc(candidate_freshness.get("quote_missing_count", 0)) + " / " + _esc(candidate_freshness.get("quote_failed_count", 0)))}
      {_kv("Last Loop", _esc(runtime_summary.get("last_loop_id") or "-"))}
    </div>
    <div class="layout">
      <div>
        <section>
          <h2>Candidate Pool</h2>
          <table>
            <thead><tr><th class="num">Rank</th><th>Ticker</th><th>Bucket</th><th>Action</th><th class="num">Score</th><th class="num">Trigger</th><th>Quote</th><th class="num">Price</th><th class="num">Chg</th><th class="num">Analysis Age</th><th class="num">Quote Age</th><th>Reasons</th><th>Flags</th></tr></thead>
            <tbody>{_render_candidates(candidates)}</tbody>
          </table>
        </section>
        <section>
          <h2>Watchlist</h2>
          <table>
            <thead><tr><th>Ticker</th><th>Action</th><th class="num">Score</th><th class="num">Price</th><th class="num">Chg</th><th>Basis</th><th class="num">Analysis Age</th><th>Flags</th></tr></thead>
            <tbody>{_render_watchlist(watchlist)}</tbody>
          </table>
        </section>
        <section>
          <h2>Positions</h2>
          <table>
            <thead><tr><th>Ticker</th><th class="num">Qty</th><th class="num">Sellable</th><th class="num">Avg</th><th class="num">Last</th><th class="num">MV</th><th class="num">PnL</th><th class="num">Lots</th></tr></thead>
            <tbody>{_render_positions(positions)}</tbody>
          </table>
        </section>
        <section>
          <h2>Orders</h2>
          <table>
            <thead><tr><th>Intent</th><th>Status</th><th>Ticker</th><th>Side</th><th class="num">Qty</th><th class="num">Filled</th><th class="num">Avg Px</th><th>Source</th></tr></thead>
            <tbody>{_render_orders(orders)}</tbody>
          </table>
        </section>
      </div>
      <aside>
        <section>
          <h2>Runtime</h2>
          <table>
            <tbody>
              <tr><th>Status</th><td>{_badge(runtime_status)}</td></tr>
              <tr><th>Session</th><td class="mono">{_esc(runtime_summary.get("session_id") or "-")}</td></tr>
              <tr><th>Last Loop</th><td class="mono">{_esc(runtime_summary.get("last_loop_id") or "-")}</td></tr>
              <tr><th>Health</th><td>{_badge(health_status)}</td></tr>
              <tr><th>Health Error</th><td>{_esc((payload.get("health_error") or {}).get("message") or "-")}</td></tr>
              <tr><th>Runtime Error</th><td>{_esc((payload.get("runtime_error") or {}).get("message") or "-")}</td></tr>
            </tbody>
          </table>
        </section>
        <section>
          <h2>Events</h2>
          <ul class="events">{_render_events(events)}</ul>
        </section>
      </aside>
    </div>
  </main>
</body>
</html>
"""


def write_dashboard_html(
    conn: sqlite3.Connection,
    output_path: str | Path,
    *,
    title: str = "Papertrade Realtime Desk",
    refresh_seconds: int | None = None,
) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_dashboard_html(conn, title=title, refresh_seconds=refresh_seconds), encoding="utf-8")
    return target


def create_dashboard_server(
    *,
    db_path: str | Path,
    initial_cash: float,
    host: str = "127.0.0.1",
    port: int = 8765,
    title: str = "Papertrade Realtime Desk",
    refresh_seconds: int | None = 15,
) -> ThreadingHTTPServer:
    db_file = Path(db_path)
    title_s = str(title)
    refresh_s = int(refresh_seconds or 0)

    # Prepare the schema once at startup; request handlers only read models.
    setup_conn = connect(db_file)
    init_db(setup_conn, initial_cash)
    setup_conn.close()

    class DashboardRequestHandler(BaseHTTPRequestHandler):
        server_version = "PapertradeDashboard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path in {"/", "/dashboard.html"}:
                conn = connect(db_file)
                try:
                    html_doc = render_dashboard_html(conn, title=title_s, refresh_seconds=refresh_s)
                finally:
                    conn.close()
                self._send(200, "text/html; charset=utf-8", html_doc.encode("utf-8"))
                return

            if path == "/api/dashboard/payload":
                conn = connect(db_file)
                try:
                    data = {
                        "ok": True,
                        "data": build_dashboard_payload(conn),
                        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds")},
                    }
                finally:
                    conn.close()
                self._send_json(200, data)
                return

            if path == "/healthz":
                self._send_json(200, {"ok": True, "service": "papertrade-dashboard"})
                return

            self._send_json(404, {"ok": False, "error": {"error_code": "NOT_FOUND", "path": path}})

    return ThreadingHTTPServer((host, int(port)), DashboardRequestHandler)


def serve_dashboard(
    *,
    db_path: str | Path,
    initial_cash: float,
    host: str = "127.0.0.1",
    port: int = 8765,
    title: str = "Papertrade Realtime Desk",
    refresh_seconds: int | None = 15,
) -> None:
    server = create_dashboard_server(
        db_path=db_path,
        initial_cash=initial_cash,
        host=host,
        port=port,
        title=title,
        refresh_seconds=refresh_seconds,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"http://{actual_host}:{actual_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a static papertrade realtime dashboard")
    ap.add_argument("--config", default=None, help="optional papertrade config JSON")
    ap.add_argument("--db", default=None, help="optional SQLite db path override")
    ap.add_argument("--output", default=".cache/paper_trade/dashboard.html", help="HTML output path")
    ap.add_argument("--title", default="Papertrade Realtime Desk", help="dashboard title")
    ap.add_argument("--refresh-seconds", type=int, default=None, help="optional browser auto-refresh interval")
    ap.add_argument("--serve", action="store_true", help="serve a local read-only dashboard")
    ap.add_argument("--host", default="127.0.0.1", help="dashboard server host")
    ap.add_argument("--port", type=int, default=8765, help="dashboard server port")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db_path = Path(args.db) if args.db else cfg.db_path
    if args.serve:
        refresh_seconds = args.refresh_seconds if args.refresh_seconds is not None else 15
        serve_dashboard(
            db_path=db_path,
            initial_cash=cfg.trade.initial_cash,
            host=args.host,
            port=args.port,
            title=args.title,
            refresh_seconds=refresh_seconds,
        )
        return

    conn = connect(db_path)
    init_db(conn, cfg.trade.initial_cash)
    path = write_dashboard_html(conn, args.output, title=args.title, refresh_seconds=args.refresh_seconds)
    conn.close()
    print(path)


if __name__ == "__main__":
    main()
