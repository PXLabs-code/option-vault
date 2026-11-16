from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .parsing import OCC, contract


def load_config(path: str) -> dict[str, Any]:
    location = Path(path).expanduser().resolve()
    with location.open() as handle:
        cfg = json.load(handle)
    symbols = cfg["symbols"]
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("symbols must be nonempty and unique")
    if any(not isinstance(s, str) or not s.isalnum() or s != s.upper() for s in symbols):
        raise ValueError("Use exact uppercase ticker symbols; no automatic aliases")
    data = Path(cfg["data_dir"]).expanduser()
    cfg["data_dir"] = str((location.parent / data).resolve() if not data.is_absolute() else data)
    http, scope = cfg["http"], cfg["range"]
    if not 0 <= http["retries"] <= 3 or http["interval_seconds"] <= 0:
        raise ValueError("http.retries must be 0..3 and interval_seconds positive")
    if not isinstance(http["retries"], int) or any(
        http[key] <= 0 for key in ("timeout_seconds", "backoff_seconds", "max_backoff_seconds")
    ):
        raise ValueError("Retry count must be an integer; HTTP timing values must be positive")
    if not 0 <= scope["delta_min"] <= scope["delta_max"] <= 1:
        raise ValueError("Invalid delta range")
    if scope["fallback_iv"] <= 0 or scope["max_years"] != 1:
        raise ValueError("fallback_iv must be positive; this collector supports a one-year horizon")
    if scope["moneyness_sigma"] <= 0 or scope["delta_margin"] < 0:
        raise ValueError("Invalid range width")
    for key in ("count_drop_ratio", "missing_fields_ratio", "zero_bid_alert_ratio"):
        if not 0 <= cfg["quality"][key] <= 1:
            raise ValueError(f"quality.{key} must be 0..1")
    if cfg["quality"]["wide_spread_ratio"] <= 0 or cfg["quality"]["stale_seconds"] <= 0:
        raise ValueError("Spread/staleness thresholds must be positive")
    if cfg["schedule"]["grace_minutes"] <= 0 or cfg["schedule"]["close_delay_minutes"] <= 0:
        raise ValueError("Schedule grace/close delay must be positive")
    if cfg["iv"]["target_days"] <= 0 or cfg["iv"]["max_atm_log_moneyness"] <= 0:
        raise ValueError("Invalid standardized IV parameters")
    if cfg["iv"]["history_observations"] < 2:
        raise ValueError("IV history requires at least two observations")
    watch = cfg.get("hourly_watch")
    if watch and watch.get("enabled"):
        symbol, code = watch["symbol"], watch["occ_symbol"]
        match = OCC.fullmatch(code)
        if (
            not match
            or match.group(1) != symbol
            or symbol not in symbols
            or not symbol.isalnum()
            or symbol != symbol.upper()
        ):
            raise ValueError("hourly_watch must use an exact OCC code for a configured symbol")
        contract(code)
    return cfg


def telegram_credentials(cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat
    local = {}
    path = Path(cfg["secrets_file"]).expanduser()
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"Secrets file must be chmod 600: {path}")
        local = json.loads(path.read_text())
    return (
        token or local.get("bot_token"),
        chat or local.get("chat_id"),
    )
