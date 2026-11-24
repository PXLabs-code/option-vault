from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any

from .calculations import NY

DEFAULTS = {
    "weights": {"efficiency": 45, "history": 20, "tail": 20, "quality": 15},
    "efficiency_scale": 0.20,
    "tail_scale": 0.20,
    "min_samples": 20,
    "max_history_gap_days": 10,
    "endpoint_tolerance_days": 4,
}


def scenarios(
    prices: list[dict[str, Any]],
    dte: int,
    cutoff: date,
    tolerance: int = 4,
) -> list[dict[str, Any]]:
    """Non-overlapping close-to-close windows, anchored at the oldest observation."""
    if dte <= 0:
        return []
    clean = [
        p
        for p in prices
        if date.fromisoformat(p["day"]) < cutoff
        and isinstance(p["close"], (int, float))
        and math.isfinite(p["close"])
        and p["close"] > 0
    ]
    clean.sort(key=lambda p: p["day"])
    dates = [date.fromisoformat(p["day"]) for p in clean]
    result = []
    start = 0
    while start < len(clean):
        target = dates[start] + timedelta(days=dte)
        if not dates or target > dates[-1]:
            break
        end = bisect_right(dates, target) - 1
        if end > start and (target - dates[end]).days <= tolerance:
            result.append(
                {
                    "start": dates[start].isoformat(),
                    "end": dates[end].isoformat(),
                    "days": (dates[end] - dates[start]).days,
                    "return": clean[end]["close"] / clean[start]["close"] - 1,
                }
            )
            start = end + 1
        else:
            start += 1
    return result


def summarize(samples: list[dict], ratio: float, side: str, details: bool = False) -> dict:
    if not samples:
        return {
            "samples": 0,
            "rate": None,
            "tail_loss": None,
            "worst": [],
            "upside": [],
            "interval": None,
            "distribution": [],
        }
    cases, losses = [], []
    successes = 0
    for sample in samples:
        end_ratio = 1 + sample["return"]
        otm = end_ratio >= ratio if side == "P" else end_ratio <= ratio
        adverse = max(0, ratio - end_ratio) if side == "P" else max(0, -sample["return"])
        successes += otm
        losses.append(adverse)
        if details:
            cases.append(
                {
                    **sample,
                    "otm": otm,
                    "strike_gap": end_ratio / ratio - 1,
                    "adverse": adverse,
                    "missed_upside": max(0, end_ratio - ratio),
                }
            )
    n = len(samples)
    rate = successes / n
    z = 1.96
    center = (rate + z * z / (2 * n)) / (1 + z * z / n)
    width = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    losses.sort()
    return {
        "samples": n,
        "rate": rate,
        "interval": [max(0, center - width), min(1, center + width)],
        "first_day": samples[0]["start"],
        "last_day": samples[-1]["end"],
        "tail_loss": losses[max(0, math.ceil(n * 0.95) - 1)],
        "worst": sorted(cases, key=lambda c: c["adverse"], reverse=True)[:5],
        "upside": sorted(cases, key=lambda c: c["missed_upside"], reverse=True)[:5]
        if side == "C"
        else [],
        "distribution": [c["strike_gap"] for c in cases],
    }


def evaluate(
    row: dict,
    samples: list[dict],
    now: datetime,
    cfg: dict,
    quality: dict,
    latest_history: str | None,
    details: bool = False,
) -> dict:
    r = dict(row)
    collected = datetime.fromisoformat(r["collected_utc"])
    cutoff = collected.astimezone(NY).date()
    stats = summarize(samples, r["strike"] / r["spot"], r["side"], details)
    reasons = []
    bid, ask = r["bid"], r["ask"]
    valid_quote = (
        bid is not None
        and ask is not None
        and math.isfinite(bid)
        and math.isfinite(ask)
        and 0 < bid <= ask
    )
    mid = (bid + ask) / 2 if valid_quote else None
    denominator = r["strike"] if r["side"] == "P" else r["spot"]
    efficiency = mid / denominator * 365 / r["dte"] if mid and r["dte"] > 0 else None
    spread = (ask - bid) / mid if mid else None
    if not valid_quote:
        reasons.append("零价、缺失或交叉报价")
    if r["dte"] <= 0:
        reasons.append("到期或剩余天数不足")
    if cutoff != now.astimezone(NY).date():
        reasons.append("非今日采集")
    timestamp = r.get("source_timestamp")
    lag = (now - datetime.fromisoformat(timestamp)).total_seconds() if timestamp else None
    if (
        lag is None
        or lag > quality["stale_seconds"]
        or lag < -quality["future_timestamp_tolerance_seconds"]
    ):
        reasons.append("报价时间未知、陈旧或超前")
    if spread is not None and spread > quality["wide_spread_ratio"]:
        reasons.append("价差过大")
    if r["oi"] is None or r["oi"] < quality["low_oi"]:
        reasons.append("OI缺失或偏低")
    if stats["samples"] < cfg["min_samples"]:
        reasons.append(f"非重叠样本不足{cfg['min_samples']}组")
    if (
        latest_history is None
        or (cutoff - date.fromisoformat(latest_history)).days > cfg["max_history_gap_days"]
    ):
        reasons.append("股价历史覆盖过旧或缺失")
    parts = {
        "efficiency": 100 * efficiency / (efficiency + cfg["efficiency_scale"])
        if efficiency is not None
        else None,
        "history": stats["rate"] * 100 if stats["rate"] is not None else None,
        "tail": 100 / (1 + stats["tail_loss"] / cfg["tail_scale"])
        if stats["tail_loss"] is not None
        else None,
        "quality": 100
        * (1 - min(1, spread))
        * max(0, min(1, (r["oi"] or 0) / max(1, quality["low_oi"])))
        if spread is not None
        else None,
    }
    return {
        **r,
        "mid": mid,
        "efficiency": efficiency,
        "spread_ratio": spread,
        "parts": parts,
        "score": weighted_score(parts, cfg["weights"]),
        "low_confidence": bool(reasons),
        "reasons": reasons,
        "statistics": stats,
        "lag_seconds": lag,
    }


def weighted_score(parts: dict, weights: dict) -> float | None:
    total = sum(weights.values())
    if total <= 0:
        return None
    if any(parts[k] is None for k, weight in weights.items() if weight > 0):
        return None
    return sum((parts[k] or 0) * weight for k, weight in weights.items()) / total
