import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from option_vault.config import load_config
from option_vault.models import Chain


@pytest.fixture
def cfg(tmp_path):
    config = load_config(str(Path(__file__).parents[1] / "config.example.json"))
    config["data_dir"] = str(tmp_path / "data")
    config["secrets_file"] = str(tmp_path / "no-secrets.json")
    config["telegram"]["enabled"] = False
    config["hourly_watch"]["enabled"] = False
    config["http"].update(
        interval_seconds=0.001, backoff_seconds=0, max_backoff_seconds=0, retries=2
    )
    return config


@pytest.fixture
def option():
    return {
        "occ_symbol": "SPY261218C00105000",
        "expiry": "2026-12-18",
        "strike": 105.0,
        "side": "C",
        "bid": 1.0,
        "ask": 1.2,
        "last": 999.0,
        "volume": 5.0,
        "oi": 100.0,
        "iv": 0.3,
        "delta": 0.3,
        "gamma": 0.1,
        "theta": -0.02,
        "vega": 0.4,
    }


@pytest.fixture
def chain(option):
    return Chain(
        "SPY",
        100.0,
        datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc),
        "cboe",
        [option],
        datetime(2026, 11, 16, 20, 15, tzinfo=timezone.utc),
    )


@pytest.fixture
def cboe_payload():
    return json.loads("""{
      "timestamp": "2026-11-16 20:00:00", "symbol": "SPY",
      "data": {"symbol": "SPY", "current_price": 100.0, "options": [
        {"option": "SPY261218C00105000", "bid": 1.0, "ask": 1.2,
         "last_trade_price": 999.0, "volume": 5, "open_interest": 100,
         "iv": 0.3, "delta": 0.3, "gamma": 0.1, "theta": -0.02, "vega": 0.4}
      ]}
    }""")
