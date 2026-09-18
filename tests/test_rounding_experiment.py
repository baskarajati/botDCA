from decimal import ROUND_FLOOR
from types import SimpleNamespace

import pytest

from botdca.backtest import Candle
from botdca.rounding_experiment import RoundedTpTraceEngine, fit_tp_rule, rounded_price
from botdca.rounding_experiment_cli import quantity_diagnostic
from botdca.strategy import StrategyConfig


def test_decimal_floor_not_binary_float_rounding():
    assert rounded_price(82.06, 1, "0.01", ROUND_FLOOR) == 82.88
    assert rounded_price(56.84, 1.1, "0.001", ROUND_FLOOR) == 57.465


def test_fit_training_grid_recovers_nominal_floor_rule():
    training = [
        SimpleNamespace(
            average_entry=p,
            closing_price=rounded_price(
                p,
                1,
                "0.01",
                ROUND_FLOOR,
            ),
        )
        for p in [82.06, 83.793, 85.591, 81.924, 84.17]
    ]
    rule, candidates = fit_tp_rule(training)
    assert rule["tick"] == "0.01"
    assert rule["mode"] == ROUND_FLOOR
    assert rule["percent"] == 1
    assert rule["mean_absolute_price_error"] == 0
    assert len(candidates) == 14
    with pytest.raises(ValueError):
        fit_tp_rule([])


def test_research_engine_only_changes_tp_not_entry_or_dca():
    config = StrategyConfig(tp_percent=1)
    rule = {"tick": "0.01", "mode": ROUND_FLOOR}
    engine = RoundedTpTraceEngine(config, rule=rule, auto_reentry=False)
    result = engine.run(
        [Candle(0, 82.06, 82.06, 82.06, 82.06), Candle(1000, 82.06, 82.88, 82.06, 82.88)]
    )
    assert result.completed_cycles == 1
    assert result.cycles[0].fill_prices == (82.06,)
    assert result.cycles[0].exit_price == 82.88


def test_quantity_grid_diagnostic_does_not_apply_rounding_to_replay():
    training = [
        SimpleNamespace(orders=[SimpleNamespace(order_qty=a), SimpleNamespace(order_qty=b)])
        for a, b in [(0.71, 0.95), (1.01, 1.35)]
    ]
    result = quantity_diagnostic(training)
    assert result["observed_export_quantity_grid"] == "0.01"
    assert result["replay_quantities_changed"] is False
    nearest = next(s for s in result["scores"] if s["mode"] == "ROUND_HALF_UP")
    assert nearest["exact_matches"] == 2


def test_rounded_tp_recomputes_after_dca_without_rounding_dca_trigger():
    engine = RoundedTpTraceEngine(
        StrategyConfig(tp_percent=1), rule={"tick": "0.01", "mode": ROUND_FLOOR}, auto_reentry=False
    )
    engine.run([Candle(0, 82.06, 82.06, 82.06, 82.06), Candle(1000, 82.06, 82.06, 81, 81)])
    cycle = engine.strategy.current_cycle
    assert cycle is not None
    assert cycle.fills[1].price == pytest.approx(82.06 * (1 - 1.05 / 100))
    assert cycle.tp_price == rounded_price(cycle.average_entry, 1, "0.01", ROUND_FLOOR)
