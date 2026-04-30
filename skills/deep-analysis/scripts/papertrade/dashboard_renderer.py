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
from papertrade.i18n import display_label, normalize_lang, t  # noqa: E402
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


def _badge(value: Any, *, lang: str = "zh-CN") -> str:
    text = _esc(display_label(value, lang=lang))
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


def _join_labels(values: list[Any], *, lang: str, limit: int | None = None) -> str:
    selected = values[:limit] if limit is not None else values
    return ", ".join(display_label(x, lang=lang) for x in selected)


def _format_event_payload(payload: Any, *, lang: str) -> str:
    if not isinstance(payload, dict):
        return str(payload or "")
    if lang == "en":
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    fields = [
        ("ticker", "代码"),
        ("action", "动作"),
        ("score_final", "分数"),
        ("signal_id", "信号ID"),
        ("as_of_date", "日期"),
        ("loop_no", "循环"),
        ("ticker_count", "标的数"),
        ("failed_count", "失败数"),
        ("success_count", "成功数"),
        ("created_count", "创建数"),
        ("expired_day_count", "过期天数"),
    ]
    parts: list[str] = []
    for key, label in fields:
        if key not in payload:
            continue
        value = payload.get(key)
        if key == "action":
            value = display_label(value, lang=lang)
        elif isinstance(value, float):
            value = f"{value:.3f}".rstrip("0").rstrip(".")
        parts.append(f"{label} {value}")

    latest_nav = payload.get("latest_nav")
    if isinstance(latest_nav, dict):
        nav_bits = []
        for key, label in (("as_of_date", "日期"), ("cash", "现金"), ("equity", "权益")):
            if key in latest_nav:
                value = latest_nav.get(key)
                if isinstance(value, (int, float)) and key in {"cash", "equity"}:
                    value = _num(value, 2)
                nav_bits.append(f"{label} {value}")
        if nav_bits:
            parts.append("最新净值：" + "，".join(nav_bits))

    if not parts:
        return f"详情已记录（{len(payload)} 项）"
    return " · ".join(parts)


def _render_watchlist(items: list[dict[str, Any]], *, lang: str) -> str:
    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td>{_badge(item.get("action"), lang=lang)}</td>
              <td class="num">{_num(item.get("score_final"), 1)}</td>
              <td class="num">{_num(item.get("last_price"), 2)}</td>
              <td class="num">{_pct(item.get("change_pct"))}</td>
              <td>{_esc(display_label(item.get("decision_basis"), lang=lang))}</td>
              <td class="num">{_esc(item.get("analysis_age_ms"))}</td>
              <td>{_esc(_join_labels(item.get("staleness_flags") or [], lang=lang))}</td>
            </tr>
            """
        )
    if not rows:
        rows.append(_empty_row(8, "暂无观察标的"))
    return "\n".join(rows)


def _render_candidates(items: list[dict[str, Any]], *, lang: str) -> str:
    rows = []
    for item in items:
        reasons = item.get("reasons") or []
        flags = item.get("freshness_flags") or []
        rows.append(
            f"""
            <tr>
              <td class="num">{_esc(item.get("rank"))}</td>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td>{_badge(item.get("bucket"), lang=lang)}</td>
              <td>{_badge(item.get("action_state"), lang=lang)}</td>
              <td class="num">{_num(item.get("candidate_score"), 1)}</td>
              <td class="num">{_num(item.get("trigger_price"), 3)}</td>
              <td>{_badge(item.get("quote_status"), lang=lang)}</td>
              <td class="num">{_num(item.get("quote_price"), 3)}</td>
              <td class="num">{_pct(item.get("quote_change_pct"))}</td>
              <td class="num">{_esc(item.get("analysis_age_ms"))}</td>
              <td class="num">{_esc(item.get("quote_age_ms"))}</td>
              <td>{_esc(_join_labels(reasons, lang=lang, limit=3))}</td>
              <td>{_esc(_join_labels(flags, lang=lang))}</td>
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


def _render_orders(items: list[dict[str, Any]], *, lang: str) -> str:
    rows = []
    for item in items:
        rows.append(
            f"""
            <tr>
              <td class="mono">{_esc(item.get("intent_id"))}</td>
              <td>{_badge(item.get("status"), lang=lang)}</td>
              <td class="mono">{_esc(item.get("ticker"))}</td>
              <td>{_esc(display_label(item.get("side"), lang=lang))}</td>
              <td class="num">{_esc(item.get("qty"))}</td>
              <td class="num">{_esc(item.get("filled_qty"))}</td>
              <td class="num">{_num(item.get("avg_fill_price"), 3)}</td>
              <td>{_esc(display_label(item.get("source"), lang=lang))}</td>
            </tr>
            """
        )
    if not rows:
        rows.append(_empty_row(8, "暂无订单"))
    return "\n".join(rows)


def _render_events(events: list[dict[str, Any]], *, lang: str) -> str:
    rows = []
    for event in events[:40]:
        payload_text = _format_event_payload(event.get("payload"), lang=lang)
        rows.append(
            f"""
            <li>
              <span>{_badge(event.get("event_type"), lang=lang)}</span>
              <code>{_esc(display_label(event.get("entity_type"), lang=lang))}:{_esc(event.get("entity_id"))}</code>
              <em>{_esc(display_label(event.get("status"), lang=lang))}</em>
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
    title: str = "模拟实盘看板",
    generated_at: str | None = None,
    refresh_seconds: int | None = None,
    lang: str = "zh-CN",
) -> str:
    lang = normalize_lang(lang)
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
    refresh_note = f" · {t('auto_refresh', lang=lang)} {refresh_s}s" if refresh_s > 0 else ""

    return f"""<!doctype html>
<html lang="{_esc(lang)}">
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
      <div class="sub">{_esc(t("generated", lang=lang))} {_esc(generated)} · {t("runtime", lang=lang)} {_badge(runtime_status, lang=lang)} · {t("health", lang=lang)} {_badge(health_status, lang=lang)}{_esc(refresh_note)}</div>
    </div>
    <div class="actions">
      <button type="button" onclick="location.reload()">{_esc(t("refresh", lang=lang))}</button>
      <button type="button" onclick="window.print()">{_esc(t("print", lang=lang))}</button>
    </div>
  </header>
  <main>
    <div class="metrics">
      {_kv(t("equity", lang=lang), _num(dashboard.get("equity"), 2), "good")}
      {_kv(t("cash", lang=lang), _num(dashboard.get("cash"), 2))}
      {_kv(t("market_value", lang=lang), _num(dashboard.get("position_market_value"), 2))}
      {_kv(t("return", lang=lang), _pct(dashboard.get("cumulative_return_pct")), "good" if _num(dashboard.get("cumulative_return_pct")) != "-" else "neutral")}
      {_kv("回撤" if lang == "zh-CN" else "Drawdown", _pct(dashboard.get("drawdown_pct")), "bad" if _f(dashboard.get("drawdown_pct")) < 0 else "neutral")}
      {_kv(t("event_lag", lang=lang), _esc(freshness.get("event_queue_lag", 0)), "warn" if _f(freshness.get("event_queue_lag")) > 0 else "neutral")}
    </div>
    <div class="metrics">
      {_kv(t("quote_freshness", lang=lang), _esc(metrics.get("quote_freshness_ms", freshness.get("quote_freshness_ms", 0))) + " ms")}
      {_kv(t("analysis_freshness", lang=lang), _esc(metrics.get("analysis_freshness_ms", freshness.get("analysis_freshness_ms", 0))) + " ms")}
      {_kv(t("candidates", lang=lang), _esc(payload["candidates"].get("item_count", 0)))}
      {_kv(t("wait_avoid", lang=lang), _esc(candidate_buckets.get("WAIT", 0)) + " / " + _esc(candidate_buckets.get("AVOID", 0)))}
      {_kv(t("quote_risk", lang=lang), _esc(candidate_freshness.get("quote_missing_count", 0)) + " / " + _esc(candidate_freshness.get("quote_failed_count", 0)))}
      {_kv(t("last_loop", lang=lang), _esc(runtime_summary.get("last_loop_id") or "-"))}
    </div>
    <div class="layout">
      <div>
        <section>
          <h2>{_esc(t("candidate_pool", lang=lang))}</h2>
          <table>
            <thead><tr><th class="num">{_esc(t("rank", lang=lang))}</th><th>{_esc(t("ticker", lang=lang))}</th><th>{_esc(t("bucket", lang=lang))}</th><th>{_esc("动作" if lang == "zh-CN" else "Action")}</th><th class="num">{_esc(t("score", lang=lang))}</th><th class="num">{_esc(t("trigger", lang=lang))}</th><th>{_esc(t("quote", lang=lang))}</th><th class="num">{_esc(t("price", lang=lang))}</th><th class="num">{_esc(t("change", lang=lang))}</th><th class="num">{_esc(t("analysis_age", lang=lang))}</th><th class="num">{_esc(t("quote_age", lang=lang))}</th><th>{_esc(t("reasons", lang=lang))}</th><th>{_esc(t("flags", lang=lang))}</th></tr></thead>
            <tbody>{_render_candidates(candidates, lang=lang)}</tbody>
          </table>
        </section>
        <section>
          <h2>{_esc(t("watchlist", lang=lang))}</h2>
          <table>
            <thead><tr><th>{_esc(t("ticker", lang=lang))}</th><th>{_esc("动作" if lang == "zh-CN" else "Action")}</th><th class="num">{_esc(t("score", lang=lang))}</th><th class="num">{_esc(t("price", lang=lang))}</th><th class="num">{_esc(t("change", lang=lang))}</th><th>{_esc(t("basis", lang=lang))}</th><th class="num">{_esc(t("analysis_age", lang=lang))}</th><th>{_esc(t("flags", lang=lang))}</th></tr></thead>
            <tbody>{_render_watchlist(watchlist, lang=lang)}</tbody>
          </table>
        </section>
        <section>
          <h2>{_esc(t("positions", lang=lang))}</h2>
          <table>
            <thead><tr><th>{_esc(t("ticker", lang=lang))}</th><th class="num">{_esc(t("qty", lang=lang))}</th><th class="num">{_esc(t("sellable", lang=lang))}</th><th class="num">{_esc(t("avg", lang=lang))}</th><th class="num">{_esc(t("last", lang=lang))}</th><th class="num">{_esc("市值" if lang == "zh-CN" else "MV")}</th><th class="num">PnL</th><th class="num">{_esc(t("lots", lang=lang))}</th></tr></thead>
            <tbody>{_render_positions(positions)}</tbody>
          </table>
        </section>
        <section>
          <h2>{_esc(t("orders", lang=lang))}</h2>
          <table>
            <thead><tr><th>{_esc(t("intent", lang=lang))}</th><th>{_esc(t("status", lang=lang))}</th><th>{_esc(t("ticker", lang=lang))}</th><th>{_esc(t("side", lang=lang))}</th><th class="num">{_esc(t("qty", lang=lang))}</th><th class="num">{_esc(t("filled", lang=lang))}</th><th class="num">{_esc(t("avg_px", lang=lang))}</th><th>{_esc(t("source", lang=lang))}</th></tr></thead>
            <tbody>{_render_orders(orders, lang=lang)}</tbody>
          </table>
        </section>
      </div>
      <aside>
        <section>
          <h2>{_esc(t("runtime", lang=lang))}</h2>
          <table>
            <tbody>
              <tr><th>{_esc(t("status", lang=lang))}</th><td>{_badge(runtime_status, lang=lang)}</td></tr>
              <tr><th>{_esc(t("session", lang=lang))}</th><td class="mono">{_esc(runtime_summary.get("session_id") or "-")}</td></tr>
              <tr><th>{_esc(t("last_loop", lang=lang))}</th><td class="mono">{_esc(runtime_summary.get("last_loop_id") or "-")}</td></tr>
              <tr><th>{_esc(t("health", lang=lang))}</th><td>{_badge(health_status, lang=lang)}</td></tr>
              <tr><th>{_esc(t("health_error", lang=lang))}</th><td>{_esc((payload.get("health_error") or {}).get("message") or "-")}</td></tr>
              <tr><th>{_esc(t("runtime_error", lang=lang))}</th><td>{_esc((payload.get("runtime_error") or {}).get("message") or "-")}</td></tr>
            </tbody>
          </table>
        </section>
        <section>
          <h2>{_esc(t("events", lang=lang))}</h2>
          <ul class="events">{_render_events(events, lang=lang)}</ul>
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
    title: str = "模拟实盘看板",
    refresh_seconds: int | None = None,
    lang: str = "zh-CN",
) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_dashboard_html(conn, title=title, refresh_seconds=refresh_seconds, lang=lang), encoding="utf-8")
    return target


def create_dashboard_server(
    *,
    db_path: str | Path,
    initial_cash: float,
    host: str = "127.0.0.1",
    port: int = 8765,
    title: str = "模拟实盘看板",
    refresh_seconds: int | None = 15,
    lang: str = "zh-CN",
) -> ThreadingHTTPServer:
    db_file = Path(db_path)
    title_s = str(title)
    refresh_s = int(refresh_seconds or 0)
    lang_s = normalize_lang(lang)

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
                    html_doc = render_dashboard_html(conn, title=title_s, refresh_seconds=refresh_s, lang=lang_s)
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
    title: str = "模拟实盘看板",
    refresh_seconds: int | None = 15,
    lang: str = "zh-CN",
) -> None:
    server = create_dashboard_server(
        db_path=db_path,
        initial_cash=initial_cash,
        host=host,
        port=port,
        title=title,
        refresh_seconds=refresh_seconds,
        lang=lang,
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
    ap.add_argument("--title", default="模拟实盘看板", help="dashboard title")
    ap.add_argument("--lang", default="zh-CN", help="dashboard language: zh-CN or en")
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
            lang=args.lang,
        )
        return

    conn = connect(db_path)
    init_db(conn, cfg.trade.initial_cash)
    path = write_dashboard_html(conn, args.output, title=args.title, refresh_seconds=args.refresh_seconds, lang=args.lang)
    conn.close()
    print(path)


if __name__ == "__main__":
    main()
