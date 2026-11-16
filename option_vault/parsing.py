from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .models import Chain

# Verified against live SPY option/quote JSON on 2026-11-16.
CBOE_FIELDS = {
    "container": "data",
    "options": "options",
    "symbol": "symbol",
    "spot": "current_price",
    "timestamp": "timestamp",
    "occ_symbol": "option",
    "bid": "bid",
    "ask": "ask",
    "last": "last_trade_price",
    "volume": "volume",
    "oi": "open_interest",
    "iv": "iv",
    "delta": "delta",
    "gamma": "gamma",
    "theta": "theta",
    "vega": "vega",
}
YAHOO_FIELDS = {
    "occ_symbol": "contractSymbol",
    "bid": "bid",
    "ask": "ask",
    "last": "lastPrice",
    "volume": "volume",
    "oi": "openInterest",
    "iv": "impliedVolatility",
}
NUMERIC_FIELDS = ("bid", "ask", "last", "volume", "oi", "iv", "delta", "gamma", "theta", "vega")
OCC = re.compile(r"^(.+?)(\d{6})([CP])(\d{8})$")


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def contract(code: str) -> dict[str, Any]:
    match = OCC.fullmatch(code.replace(" ", ""))
    if match is None:
        raise ValueError(f"Unrecognized OCC symbol: {code!r}")
    _, expiry, side, strike = match.groups()
    return {
        "occ_symbol": code,
        "expiry": datetime.strptime(expiry, "%y%m%d").date().isoformat(),
        "side": side,
        "strike": int(strike) / 1000,
    }


def source_time(value: Any, tz: str) -> datetime | None:
    if not value:
        return None
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(tz))
    return stamp.astimezone(timezone.utc)


def parse_cboe(
    payload: dict[str, Any],
    symbol: str,
    fetched_at: datetime,
    timestamp_tz: str,
) -> Chain:
    f = CBOE_FIELDS
    data = payload[f["container"]]
    if payload.get(f["symbol"]) != symbol or data.get(f["symbol"]) != symbol:
        raise ValueError(f"Cboe response symbol does not exactly match {symbol}")
    spot = number(data[f["spot"]])
    if spot is None or spot <= 0:
        raise ValueError(f"Invalid underlying price for {symbol}")
    raw_options = data[f["options"]]
    if not isinstance(raw_options, list) or not raw_options:
        raise ValueError(f"No option chain returned for {symbol}")
    rows = []
    for raw in raw_options:
        row = contract(raw[f["occ_symbol"]])
        row.update({key: number(raw.get(f[key])) for key in NUMERIC_FIELDS})
        rows.append(row)
    return Chain(
        symbol,
        spot,
        source_time(payload.get(f["timestamp"]), timestamp_tz),
        "cboe",
        rows,
        fetched_at,
    )


def parse_yahoo_row(raw: dict[str, Any]) -> dict[str, Any]:
    row = contract(raw[YAHOO_FIELDS["occ_symbol"]])
    row.update(
        {
            key: number(raw.get(YAHOO_FIELDS[key])) if key in YAHOO_FIELDS else None
            for key in NUMERIC_FIELDS
        }
    )
    return row
