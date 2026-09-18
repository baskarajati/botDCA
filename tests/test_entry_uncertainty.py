from types import SimpleNamespace

import pytest

from botdca.backtest import Candle
from botdca.entry_uncertainty import learn_entry_bounds, training_sample


def test_sample_excludes_later_cycles_and_dca():
    regime = [SimpleNamespace(opened_ms=i, dca_count=0 if i % 2 == 0 else 1) for i in range(100)]
    sample = training_sample(regime)
    assert len(sample) == 20
    assert sample[0].opened_ms == 30
    assert sample[-1].opened_ms == 68
    with pytest.raises(ValueError):
        training_sample(regime[:10])


def test_price_box_and_timing_witness_are_training_derived():
    cycle = SimpleNamespace(opened_ms=1000, average_entry=100)
    bars = [Candle(0, 100, 100, 100, 100), Candle(1000, 101, 101, 101, 101)]
    bounds = learn_entry_bounds([cycle], {1000: bars})
    assert bounds["relative_price_bound"] == pytest.approx(1 - 99.9995 / 101)
    assert bounds["timing_proxy_bound_ms"] == 1000
    assert bounds["actual_fill_timing_identified"] is False


def test_censored_witness_does_not_invent_timing_bound():
    cycle = SimpleNamespace(opened_ms=1000, average_entry=100)
    bounds = learn_entry_bounds([cycle], {1000: [Candle(1000, 101, 101, 101, 101)]})
    assert bounds["timing_censored_cycles"] == 1
    assert bounds["timing_proxy_bound_ms"] is None


def test_missing_entry_data_fails_loudly():
    cycle = SimpleNamespace(opened_ms=1000, average_entry=100)
    with pytest.raises(ValueError):
        learn_entry_bounds([cycle], {1000: []})
