from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentMetadata:
    ticker: str
    market: str
    board: str
    is_st: bool
    price_limit_pct: float
    lot_size: int


class InstrumentMetadataProvider:
    """Infer minimal A-share instrument metadata for rule checks (MVP)."""

    version = "ashare-instrument-v1"

    def resolve(self, ticker: str, *, market: str = "A", name: str | None = None) -> InstrumentMetadata:
        tk = str(ticker or "").upper().strip()
        mk = str(market or "A").upper().strip()
        code = tk.split(".")[0]

        if code.startswith("688"):
            board = "STAR"
            base_limit = 20.0
        elif code.startswith("300"):
            board = "GEM"
            base_limit = 20.0
        else:
            board = "MAIN"
            base_limit = 10.0

        name_u = str(name or "").upper()
        is_st = ("ST" in name_u) or ("*ST" in name_u)
        limit_pct = 5.0 if is_st else base_limit

        return InstrumentMetadata(
            ticker=tk,
            market=mk,
            board=board,
            is_st=is_st,
            price_limit_pct=float(limit_pct),
            lot_size=100,
        )
