from __future__ import annotations

import multiprocessing as mp
import os
import queue
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.data_sources import fetch_basic  # noqa: E402
from lib.market_router import parse_ticker  # noqa: E402


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw in (None, ""):
            return default
        return float(raw)
    except Exception:
        return default


DEFAULT_QUOTE_TIMEOUT_SECONDS = _env_float("PAPERTRADE_QUOTE_TIMEOUT_SECONDS", 8.0)


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except Exception:
        return d


def _failure_snapshot(
    ticker: str,
    *,
    error_code: str,
    reason: str,
    source: str = "fetch_basic",
    timeout_seconds: float | None = None,
    elapsed_ms: int | None = None,
) -> dict[str, Any]:
    ti = parse_ticker(ticker)
    payload: dict[str, Any] = {
        "ok": False,
        "ticker": ti.full,
        "market": ti.market,
        "reason": reason,
        "error_code": error_code,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": source,
    }
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    return payload


def _snapshot_from_basic(ticker: str, basic: dict[str, Any]) -> dict[str, Any]:
    ti = parse_ticker(ticker)
    price = _f(basic.get("price"), 0.0)
    return {
        "ok": price > 0,
        "ticker": ti.full,
        "market": ti.market,
        "price": price,
        "change_pct": basic.get("change_pct"),
        "name": basic.get("name"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": basic.get("_fallback_snap") or "fetch_basic",
    }


def _snapshot_from_quote(ticker: str, quote: dict[str, Any], *, source: str = "direct_http") -> dict[str, Any]:
    ti = parse_ticker(ticker)
    price = _f(quote.get("price"), 0.0)
    prev_close = _f(quote.get("prev_close"), 0.0)
    change_pct = quote.get("change_pct")
    if change_pct in (None, "") and price > 0 and prev_close > 0:
        change_pct = round((price / prev_close - 1) * 100, 2)
    return {
        "ok": price > 0,
        "ticker": ti.full,
        "market": ti.market,
        "price": price,
        "change_pct": change_pct,
        "name": quote.get("name"),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "prev_close": quote.get("prev_close"),
        "volume": quote.get("volume"),
        "amount": quote.get("amount"),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": quote.get("source") or source,
        "provider": source,
    }


def _fetch_direct_http_snapshot(ticker: str) -> dict[str, Any]:
    """Fast quote-only path for realtime loops.

    The full `fetch_basic` path may spend many seconds on rich profile sources
    before reaching Tencent/Sina fallbacks. Realtime papertrade needs the
    opposite order: quote first, fundamentals later.
    """
    ti = parse_ticker(ticker)
    try:
        from lib import providers

        provider = providers.get("direct_http")
        if provider is None or not provider.is_available():
            return _failure_snapshot(
                ti.full,
                error_code="DIRECT_HTTP_UNAVAILABLE",
                reason="direct_http provider is unavailable",
                source="direct_http",
            )
        quote = provider.fetch_quote(ti.code, ti.market)
        snapshot = _snapshot_from_quote(ti.full, quote if isinstance(quote, dict) else {}, source="direct_http")
        if snapshot.get("ok"):
            return snapshot
        return _failure_snapshot(
            ti.full,
            error_code="DIRECT_HTTP_EMPTY_PRICE",
            reason="direct_http returned no usable price",
            source=snapshot.get("source") or "direct_http",
        )
    except Exception as e:
        return _failure_snapshot(
            ti.full,
            error_code="DIRECT_HTTP_QUOTE_FAILED",
            reason=str(e),
            source="direct_http",
        )


def _fetch_realtime_snapshot_inline(ticker: str) -> dict[str, Any]:
    ti = parse_ticker(ticker)
    direct = _fetch_direct_http_snapshot(ti.full)
    if direct.get("ok"):
        return direct

    try:
        basic = fetch_basic(ti) or {}
    except Exception as e:
        return _failure_snapshot(
            ti.full,
            error_code="QUOTE_PROVIDER_EXCEPTION",
            reason=f"{str(e)}; direct_http={direct.get('reason')}",
            source="fetch_basic",
        )

    snapshot = _snapshot_from_basic(ti.full, basic if isinstance(basic, dict) else {})
    if not snapshot.get("ok"):
        snapshot["direct_http_error_code"] = direct.get("error_code")
        snapshot["direct_http_error_reason"] = direct.get("reason")
    return snapshot


def _quote_worker(ticker: str, result_queue: Any) -> None:
    try:
        result_queue.put(_fetch_realtime_snapshot_inline(ticker))
    except BaseException as e:
        result_queue.put(
            _failure_snapshot(
                ticker,
                error_code="QUOTE_PROVIDER_EXCEPTION",
                reason=str(e),
                source="fetch_basic_worker",
            )
        )


def _process_context() -> mp.context.BaseContext:
    for method in ("fork", "spawn"):
        try:
            return mp.get_context(method)
        except ValueError:
            continue
    return mp.get_context()


def _fetch_realtime_snapshot_timed(ticker: str, *, timeout_seconds: float) -> dict[str, Any]:
    ti = parse_ticker(ticker)
    timeout = max(0.1, float(timeout_seconds))
    started = time.monotonic()
    result_queue = None
    proc = None
    try:
        ctx = _process_context()
        result_queue = ctx.Queue(maxsize=1)
        proc = ctx.Process(target=_quote_worker, args=(ti.full, result_queue))
        proc.daemon = True
        proc.start()
        proc.join(timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if proc.is_alive():
            proc.terminate()
            proc.join(1.0)
            if proc.is_alive() and hasattr(proc, "kill"):
                proc.kill()
                proc.join(1.0)
            return _failure_snapshot(
                ti.full,
                error_code="QUOTE_PROVIDER_TIMEOUT",
                reason=f"quote provider timed out after {timeout:.2f}s",
                source="fetch_basic_timeout",
                timeout_seconds=timeout,
                elapsed_ms=elapsed_ms,
            )
        try:
            snapshot = result_queue.get(timeout=0.2)
        except queue.Empty:
            return _failure_snapshot(
                ti.full,
                error_code="QUOTE_PROVIDER_NO_RESULT",
                reason=f"quote provider exited without result (exitcode={proc.exitcode})",
                source="fetch_basic_timeout",
                timeout_seconds=timeout,
                elapsed_ms=elapsed_ms,
            )
        if isinstance(snapshot, dict):
            return snapshot
        return _failure_snapshot(
            ti.full,
            error_code="QUOTE_PROVIDER_BAD_RESULT",
            reason="quote provider returned non-object",
            source="fetch_basic_timeout",
            timeout_seconds=timeout,
            elapsed_ms=elapsed_ms,
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return _failure_snapshot(
            ti.full,
            error_code="QUOTE_PROVIDER_TIMEOUT_WRAPPER_FAILED",
            reason=str(e),
            source="fetch_basic_timeout",
            timeout_seconds=timeout,
            elapsed_ms=elapsed_ms,
        )
    finally:
        if result_queue is not None:
            try:
                result_queue.close()
                result_queue.join_thread()
            except Exception:
                pass


def fetch_realtime_snapshot(ticker: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
    timeout = DEFAULT_QUOTE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        timeout_f = float(timeout)
    except Exception:
        timeout_f = DEFAULT_QUOTE_TIMEOUT_SECONDS
    if timeout_f > 0:
        return _fetch_realtime_snapshot_timed(ticker, timeout_seconds=timeout_f)
    return _fetch_realtime_snapshot_inline(ticker)


def inject_snapshot_into_bundle(bundle: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    if not isinstance(bundle, dict) or not isinstance(snapshot, dict):
        return False
    if not snapshot.get("ok"):
        return False

    raw = bundle.setdefault("raw", {})
    dims = raw.setdefault("dimensions", {})
    d0 = dims.setdefault("0_basic", {})
    data = d0.setdefault("data", {})

    old_price = _f(data.get("price"), 0.0)
    new_price = _f(snapshot.get("price"), 0.0)
    if new_price <= 0:
        return False

    data["price"] = new_price
    if snapshot.get("change_pct") not in (None, ""):
        data["change_pct"] = snapshot.get("change_pct")

    runtime = bundle.setdefault("_papertrade_runtime", {})
    runtime["quote_snapshot"] = snapshot
    runtime["quote_overlay_applied"] = True
    return abs(old_price - new_price) > 1e-8
