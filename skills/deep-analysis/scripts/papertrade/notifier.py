from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from typing import Any

from papertrade.config import PaperTradeConfig
from papertrade.ledger import append_jsonl, record_alert


def _post_webhook(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            code = getattr(resp, "status", 200)
        return True, f"http_{code}"
    except Exception as e:
        return False, str(e)


def make_alert_payload(
    *,
    ticker: str,
    action: str,
    score_final: float,
    summary: dict[str, Any],
    order: dict[str, Any] | None,
    as_of_date: str,
) -> dict[str, Any]:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "as_of_date": as_of_date,
        "ticker": ticker,
        "action": action,
        "score_final": round(score_final, 3),
        "verdict": summary.get("verdict_label"),
        "panel_mode": summary.get("panel_mode"),
        "panel_consensus": summary.get("panel_consensus"),
        "short_bias": summary.get("short_bias"),
        "agent_reviewed": summary.get("agent_reviewed"),
        "action_layer": summary.get("action_layer"),
        "panel_insights": summary.get("panel_insights", ""),
        "order": order or {},
    }


def emit_alert(
    conn,
    cfg: PaperTradeConfig,
    *,
    run_id: str,
    as_of_date: str,
    ticker: str,
    level: str,
    title: str,
    body: str,
    payload: dict[str, Any],
) -> None:
    record_alert(
        conn,
        run_id=run_id,
        as_of_date=as_of_date,
        ticker=ticker,
        level=level,
        channel="jsonl",
        title=title,
        body=body,
        payload=payload,
    )
    append_jsonl(cfg.alerts_jsonl, payload)

    webhook = os.environ.get("PAPERTRADE_WEBHOOK_URL", "").strip()
    if webhook:
        ok, msg = _post_webhook(webhook, payload)
        record_alert(
            conn,
            run_id=run_id,
            as_of_date=as_of_date,
            ticker=ticker,
            level="info" if ok else "warning",
            channel="webhook",
            title=f"webhook_{'ok' if ok else 'fail'}",
            body=msg,
            payload={"url": webhook, "ok": ok, "msg": msg},
        )
