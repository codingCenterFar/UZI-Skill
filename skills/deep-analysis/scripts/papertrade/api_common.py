from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class PaperTradeAPIError(Exception):
    error_code: str
    message: str
    details: dict[str, Any] | None = None
    retryable: bool = False
    status_code: int = 400
    correlation_id: str | None = None

    def as_error(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details or {},
            "retryable": bool(self.retryable),
            "correlation_id": self.correlation_id or new_id("crr"),
        }


def ok_envelope(data: dict[str, Any], *, correlation_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {
            "correlation_id": correlation_id or new_id("crr"),
            "ts_ms": now_ms(),
        },
    }


def error_envelope(error: PaperTradeAPIError, *, correlation_id: str | None = None) -> dict[str, Any]:
    err = error.as_error()
    if correlation_id:
        err["correlation_id"] = correlation_id
    return {"ok": False, "error": err}
