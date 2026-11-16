from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .models import Chain

NY = ZoneInfo("America/New_York")


def year_horizon(today: date) -> date:
    year = today.year + 1
    return today.replace(year=year, day=min(today.day, calendar.monthrange(year, today.month)[1]))


def decorate(chain: Chain, cfg: dict[str, Any], filter_range: bool = True) -> list[dict[str, Any]]:
    today = chain.fetched_at.astimezone(NY).date()
    scope, quality = cfg["range"], cfg["quality"]
    lag = (chain.fetched_at - chain.timestamp).total_seconds() if chain.timestamp else None
    result = []
    for original in chain.options:
        row = dict(original)
        expiry = date.fromisoformat(row["expiry"])
        dte = (expiry - today).days
        if dte < 0 or expiry > year_horizon(today):
            continue
        strike, side, delta = row["strike"], row["side"], row["delta"]
        otm = strike >= chain.spot if side == "C" else strike <= chain.spot
        if filter_range:
            if not otm:
                continue
            if delta is not None:
                if (
                    not max(0, scope["delta_min"] - scope["delta_margin"]) - 1e-12
                    <= abs(delta)
                    <= min(1, scope["delta_max"] + scope["delta_margin"]) + 1e-12
                ):
                    continue
            else:
                iv = row["iv"] if row["iv"] and row["iv"] > 0 else scope["fallback_iv"]
                width = scope["moneyness_sigma"] * iv * math.sqrt(max(dte, 1) / 365)
                if strike <= 0 or abs(math.log(strike / chain.spot)) > width:
                    continue
        bid, ask = row["bid"], row["ask"]
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        spread = abs(ask - bid) if bid is not None and ask is not None else None
        ratio = spread / mid if spread is not None and mid and mid > 0 else None
        flags = []
        if bid == 0:
            flags.append("zero_bid")
        if bid is None or ask is None:
            flags.append("missing_quote")
        if row["iv"] is None or row["iv"] <= 0:
            flags.append("missing_or_zero_iv")
        if any(row[k] is None for k in ("delta", "gamma", "theta", "vega")):
            flags.append("missing_greeks")
        if row["oi"] is None:
            flags.append("missing_oi")
        elif row["oi"] < quality["low_oi"]:
            flags.append("low_oi")
        if ratio is not None and ratio > quality["wide_spread_ratio"]:
            flags.append("wide_spread")
        if bid is not None and ask is not None and (bid < 0 or ask < 0 or ask < bid):
            flags.append("invalid_quote")
        if lag is None:
            flags.append("missing_source_timestamp")
        elif lag > quality["stale_seconds"]:
            flags.append("stale_timestamp")
        elif lag < -quality["future_timestamp_tolerance_seconds"]:
            flags.append("future_timestamp")
        row.update(
            collected_utc=chain.fetched_at.isoformat(),
            collected_et=chain.fetched_at.astimezone(NY).isoformat(),
            source_timestamp=chain.timestamp.isoformat() if chain.timestamp else None,
            symbol=chain.symbol,
            spot=chain.spot,
            source=chain.source,
            mid=mid,
            dte=dte,
            annualized_yield=mid / strike * 365 / dte
            if mid is not None and strike > 0 and dte > 0
            else None,
            spread=spread,
            spread_ratio=ratio,
            flags=flags,
        )
        result.append(row)
    return result


def standardized_iv(chain: Chain, cfg: dict[str, Any]) -> dict[str, Any] | None:
    today = chain.fetched_at.astimezone(NY).date()
    target = cfg["iv"]["target_days"]
    by_expiry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in chain.options:
        expiry = date.fromisoformat(row["expiry"])
        if (
            today < expiry <= year_horizon(today)
            and row["iv"] is not None
            and row["iv"] > 0
            and row["strike"] > 0
            and abs(math.log(row["strike"] / chain.spot)) <= cfg["iv"]["max_atm_log_moneyness"]
        ):
            by_expiry[row["expiry"]].append(row)
    atm = []
    for expiry, rows in by_expiry.items():
        distance = min(abs(math.log(r["strike"] / chain.spot)) for r in rows)
        selected = [
            r for r in rows if abs(abs(math.log(r["strike"] / chain.spot)) - distance) < 1e-10
        ]
        atm.append(
            {
                "expiry": expiry,
                "dte": (date.fromisoformat(expiry) - today).days,
                "iv": median(r["iv"] for r in selected),
                "contracts": [
                    {
                        "occ_symbol": r["occ_symbol"],
                        "strike": r["strike"],
                        "side": r["side"],
                        "iv": r["iv"],
                    }
                    for r in selected
                ],
            }
        )
    lower = [r for r in atm if r["dte"] <= target]
    upper = [r for r in atm if r["dte"] >= target]
    if not lower or not upper:
        return None
    lo, hi = max(lower, key=lambda r: r["dte"]), min(upper, key=lambda r: r["dte"])
    weight = (target - lo["dte"]) / (hi["dte"] - lo["dte"]) if hi["dte"] != lo["dte"] else 0
    return {
        "day": today.isoformat(),
        "symbol": chain.symbol,
        "iv": lo["iv"] + weight * (hi["iv"] - lo["iv"]),
        "source": chain.source,
        "source_timestamp": chain.timestamp.isoformat() if chain.timestamp else None,
        "inputs": [lo, hi],
        "target_days": target,
    }


def iv_metrics(
    current: float, history: list[float], required: int = 252
) -> dict[str, float | None]:
    """History is preceding daily observations, excluding the current observation."""
    if required < 2:
        raise ValueError("IV history requires at least two observations")
    clean = [x for x in history if x is not None and math.isfinite(x) and x > 0]
    if len(clean) < required or not math.isfinite(current) or current <= 0:
        return {"rank": None, "percentile": None}
    window = clean[-required:]
    low, high = min(window), max(window)
    return {
        "rank": (current - low) / (high - low) * 100 if high != low else None,
        "percentile": sum(x < current for x in window) / required * 100,
    }
