"""Tests for the volatility panel's sampling helpers."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from live_trading.portfolio.volatility_data import (
    VolSample,
    make_demo_sample,
    pct_change,
    samples_to_frame,
)

ET = ZoneInfo("America/New_York")


def _sample(vix=None, call_iv=None, put_iv=None, source="ibkr"):
    return VolSample(
        sampled_at=datetime.now(ET),
        source=source,
        expiry=date(2026, 11, 24),
        spot=7180.0,
        atm_strike=7180.0,
        call_iv=call_iv,
        put_iv=put_iv,
        iv_basis="model" if call_iv is not None or put_iv is not None else None,
        vix=vix,
        vix_basis="quote" if vix is not None else None,
    )


def test_pct_change_basic():
    assert pct_change(110.0, 100.0) == pytest.approx(10.0)
    assert pct_change(90.0, 100.0) == pytest.approx(-10.0)


def test_pct_change_missing_inputs():
    assert pct_change(None, 100.0) is None
    assert pct_change(100.0, None) is None
    assert pct_change(100.0, 0.0) is None


def test_atm_iv_averages_available_sides():
    s = _sample(call_iv=0.14, put_iv=0.16)
    assert s.atm_iv == pytest.approx(0.15)
    one_side = _sample(call_iv=0.14)
    assert one_side.atm_iv == pytest.approx(0.14)
    none = _sample()
    assert none.atm_iv is None


def test_samples_to_frame_change_baseline_is_first_available():
    samples = [
        _sample(vix=20.0, call_iv=0.20, put_iv=0.20),
        _sample(vix=18.0, call_iv=0.18, put_iv=0.18),
    ]
    df = samples_to_frame(samples)
    assert df["vix_change_pct"].iloc[0] == pytest.approx(0.0)
    assert df["vix_change_pct"].iloc[1] == pytest.approx(-10.0)
    assert df["atm_iv_pct"].iloc[0] == pytest.approx(20.0)
    assert df["atm_iv_change_pct"].iloc[1] == pytest.approx(-10.0)


def test_samples_to_frame_missing_first_vix_uses_first_available():
    samples = [
        _sample(vix=None, call_iv=0.20, put_iv=0.20),
        _sample(vix=20.0, call_iv=0.20, put_iv=0.20),
        _sample(vix=22.0, call_iv=0.20, put_iv=0.20),
    ]
    df = samples_to_frame(samples)
    assert df["vix_change_pct"].iloc[0] is None or df["vix_change_pct"].iloc[0] != df["vix_change_pct"].iloc[0]
    assert df["vix_change_pct"].iloc[2] == pytest.approx(10.0)


def test_samples_to_frame_missing_iv_stays_missing():
    df = samples_to_frame([_sample(vix=20.0)])
    assert len(df) == 1
    assert df["atm_iv_pct"].isna().iloc[0]


def test_demo_sample_continues_from_previous():
    first = make_demo_sample(seed=1)
    second = make_demo_sample(previous=first, seed=2)
    assert first.source == "demo"
    assert second.source == "demo"
    assert second.atm_strike == first.atm_strike
    assert second.atm_iv is not None
    assert 9.0 <= second.vix <= 60.0



def test_samples_to_frame_iv_change_is_per_basis():
    base = datetime.now(ET)
    samples = [
        VolSample(sampled_at=base, source="ibkr", expiry=date(2026, 11, 24),
                  spot=7180.0, atm_strike=7180.0, call_iv=0.10, put_iv=0.10,
                  iv_basis="model", vix=20.0, vix_basis="quote"),
        VolSample(sampled_at=base, source="ibkr", expiry=date(2026, 11, 24),
                  spot=7180.0, atm_strike=7180.0, call_iv=0.20, put_iv=0.20,
                  iv_basis="price-fallback", vix=22.0, vix_basis="quote"),
        VolSample(sampled_at=base, source="ibkr", expiry=date(2026, 11, 24),
                  spot=7180.0, atm_strike=7180.0, call_iv=0.22, put_iv=0.22,
                  iv_basis="price-fallback", vix=23.0, vix_basis="quote"),
    ]
    df = samples_to_frame(samples)
    changes = list(df["atm_iv_change_pct"])
    # The basis switch resets the baseline: the jump 10% -> 20% is not
    # reported as +100%, and the fallback series restarts at 0.
    assert changes[0] == pytest.approx(0.0)
    assert changes[1] == pytest.approx(0.0)
    assert changes[2] == pytest.approx(10.0)
    # VIX change is unaffected: still against the session's first VIX.
    assert list(df["vix_change_pct"]) == [0.0, 10.0, 15.0]


def test_samples_to_frame_iv_change_within_basis_still_tracked():
    samples = [
        _sample(call_iv=0.10, put_iv=0.10),
        _sample(call_iv=0.11, put_iv=0.11),
    ]
    df = samples_to_frame(samples)
    assert list(df["atm_iv_change_pct"]) == [0.0, pytest.approx(10.0)]


class _FakeIB:
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.attempts = []

    def connect(self, host, port, clientId, timeout):
        self.attempts.append(clientId)
        if clientId in self.fail_ids:
            raise ConnectionError("client id already in use")


def test_connect_auto_picks_id_outside_tws_retry_range():
    from live_trading.portfolio.volatility_data import _connect
    ib = _FakeIB()
    used = _connect(ib, "127.0.0.1", 4001, 0)
    assert used in range(32, 1000)
    assert used not in range(1, 32)
    assert all(a in range(32, 1000) for a in ib.attempts)
    assert len(ib.attempts) <= 10


def test_connect_auto_retries_occupied_ids():
    from live_trading.portfolio.volatility_data import _connect
    import live_trading.portfolio.volatility_data as vd
    # Occupy every candidate except force a deterministic short list.
    import random
    original_sample = random.sample
    try:
        vd.random.sample = lambda rng, n: [50, 51, 52]
        ib = _FakeIB(fail_ids={50, 51})
        used = _connect(ib, "127.0.0.1", 4001, 0)
        assert used == 52
        assert ib.attempts == [50, 51, 52]
    finally:
        vd.random.sample = original_sample


def test_connect_fixed_id_used_as_given_and_failure_propagates():
    from live_trading.portfolio.volatility_data import _connect
    ib = _FakeIB()
    assert _connect(ib, "127.0.0.1", 4001, 11) == 11
    assert ib.attempts == [11]
    failing = _FakeIB(fail_ids={11})
    with pytest.raises(ConnectionError):
        _connect(failing, "127.0.0.1", 4001, 11)
