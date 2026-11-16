import copy
import gzip
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from option_vault.calculations import decorate, iv_metrics, standardized_iv, year_horizon
from option_vault.models import SymbolResult
from option_vault.parsing import parse_cboe, parse_yahoo_row
from option_vault.storage import Archive, Storage


def test_observed_cboe_mapping_and_exact_symbol(cboe_payload):
    chain = parse_cboe(cboe_payload, "SPY", datetime.now(timezone.utc), "UTC")
    assert chain.options[0]["last"] == 999
    assert chain.options[0]["strike"] == 105
    assert chain.options[0]["expiry"] == "2026-12-18"
    assert chain.timestamp.utcoffset() == timedelta(0)
    cboe_payload["symbol"] = "SPAX"
    with pytest.raises(ValueError, match="exactly match"):
        parse_cboe(cboe_payload, "SPCX", datetime.now(timezone.utc), "UTC")


def test_bad_structure_does_not_look_successful(cboe_payload):
    del cboe_payload["data"]["options"]
    with pytest.raises(KeyError):
        parse_cboe(cboe_payload, "SPY", datetime.now(timezone.utc), "UTC")


def test_quality_retained_and_mid_not_last(chain, cfg):
    chain.options[0].update(bid=0, ask=2, oi=0, iv=None)
    row = decorate(chain, cfg)[0]
    assert row["mid"] == 1
    assert row["annualized_yield"] == pytest.approx(1 / 105 * 365 / 32)
    assert row["spread_ratio"] == 2
    assert set(row["flags"]) == {"zero_bid", "missing_or_zero_iv", "low_oi", "wide_spread"}
    chain.options[0]["bid"] = None
    row = decorate(chain, cfg)[0]
    assert "missing_quote" in row["flags"]
    assert row["mid"] is None and row["annualized_yield"] is None


def test_delta_range_margin_otm_and_one_year(chain, cfg):
    original = chain.options[0]
    chain.options = [
        dict(original, delta=0.03),
        dict(original, delta=0.02),
        dict(original, side="P", strike=95, delta=-0.3),
        dict(original, strike=99),
        dict(original, expiry="2027-11-17"),
    ]
    assert len(decorate(chain, cfg)) == 2
    assert year_horizon(date(2024, 2, 29)) == date(2025, 2, 28)


def test_missing_delta_width_expands_with_iv_and_term(chain, cfg):
    chain.options[0].update(delta=None, strike=140, iv=0.2, expiry="2026-12-18")
    assert decorate(chain, cfg) == []
    chain.options[0]["iv"] = 1.0
    assert len(decorate(chain, cfg)) == 1
    chain.options[0].update(iv=0.2, expiry="2027-10-15")
    assert len(decorate(chain, cfg)) == 1


def test_same_day_zero_mid_and_stale_timestamp(chain, cfg):
    chain.options[0].update(expiry="2026-11-16", bid=0, ask=0)
    chain.timestamp -= timedelta(hours=2)
    row = decorate(chain, cfg)[0]
    assert row["dte"] == 0 and row["annualized_yield"] is None
    assert row["spread_ratio"] is None
    assert "stale_timestamp" in row["flags"]
    chain.timestamp = None
    assert "missing_source_timestamp" in decorate(chain, cfg)[0]["flags"]


def test_iv_brackets_atm_from_unfiltered_chain(chain, cfg):
    base = chain.options[0]
    chain.options = [
        dict(base, expiry="2026-12-06", strike=100, iv=0.2, delta=0.6),
        dict(base, expiry="2026-12-26", strike=100, iv=0.4, delta=0.6),
        dict(base, expiry="2026-12-26", strike=120, iv=3, delta=0.6),
    ]
    result = standardized_iv(chain, cfg)
    assert result["iv"] == pytest.approx(0.3)
    assert result["inputs"][0]["contracts"][0]["strike"] == 100
    assert decorate(chain, cfg) == []
    chain.options = chain.options[:1]
    assert standardized_iv(chain, cfg) is None


def test_iv_exact_target_and_history_insufficient(chain, cfg):
    chain.options[0].update(expiry="2026-12-16", strike=100)
    assert standardized_iv(chain, cfg)["iv"] == 0.3
    assert iv_metrics(0.3, [0.2] * 251) == {"rank": None, "percentile": None}
    assert iv_metrics(0.3, [0.2, 0.4], required=2) == pytest.approx(
        {"rank": 50.0, "percentile": 50.0}
    )
    assert iv_metrics(0.3, [0.2, 0.2], required=2)["rank"] is None


def test_yahoo_never_fabricates_greeks():
    row = parse_yahoo_row(
        {
            "contractSymbol": "SPY261218P00100000",
            "bid": float("nan"),
            "ask": 1.2,
            "impliedVolatility": 0.3,
        }
    )
    assert row["delta"] is None and row["gamma"] is None and row["bid"] is None


def test_transaction_atomic_and_backup(chain, cfg, tmp_path):
    store = Storage(tmp_path / "vault")
    try:
        run = store.start("2026-11-16", "10:00")
        rows = decorate(chain, cfg)
        bad = copy.deepcopy(rows)
        bad[0]["not_a_column"] = 1
        result = SymbolResult("SPY", "ok", "cboe", 1)
        with pytest.raises(sqlite3.Error):
            store.save_symbol(run, result, bad)
        assert store.db.execute("SELECT COUNT(*) FROM symbol_runs").fetchone()[0] == 0
        store.save_symbol(run, result, rows)
        store.finish(run, "ok")
        target = tmp_path / "backup.sqlite3"
        store.backup(target)
        with sqlite3.connect(target) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 1
        assert store.previous_counts("SPY", "2026-11-17", "10:00") == [1, 1]
    finally:
        store.close()


def test_raw_archive_is_lossless_json(tmp_path, cboe_payload):
    path = Archive(tmp_path).write("2026-11-16", "SPCX", "16:15-close", "chain", cboe_payload)
    assert "SPCX/1615-close" in str(path)
    with gzip.open(path, "rt") as handle:
        assert json.load(handle) == cboe_payload
