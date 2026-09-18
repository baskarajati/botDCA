import pytest

from botdca.strategy import DcaStrategy, DcaTriggerReference, StrategyConfig


def test_initial_cycle_and_tp() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    cycle = strategy.begin_cycle(100.0)

    assert cycle.average_entry == 100.0
    assert round(cycle.total_qty, 8) == 0.24
    assert round(cycle.tp_price or 0, 4) == 101.09
    assert strategy.should_take_profit(101.09)


def test_first_dca_trigger_and_weighted_average() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    cycle = strategy.begin_cycle(100.0)

    trigger = strategy.next_dca_trigger_price()
    qty = strategy.next_dca_qty()

    assert trigger is not None
    assert qty is not None
    assert round(trigger, 4) == 98.95
    assert round(qty / cycle.fills[-1].qty, 3) == 1.339

    strategy.apply_dca_fill(trigger)
    assert cycle.dca_level == 1
    assert cycle.average_entry is not None
    assert cycle.average_entry < 100.0
    assert cycle.tp_price is not None
    assert cycle.tp_price < 101.09


def test_pause_keeps_open_cycle_manageable_but_disables_reentry() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    strategy.begin_cycle(100.0)

    strategy.pause()

    assert strategy.state.value == "paused"
    assert strategy.reentry_enabled is False
    assert strategy.next_dca_trigger_price() is not None
    assert strategy.should_take_profit(101.09)

    strategy.mark_closed(0.25)

    assert strategy.current_cycle is None
    assert strategy.state.value == "paused"


def test_resume_after_paused_cycle_restores_active_management() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    strategy.begin_cycle(100.0)
    strategy.pause()

    strategy.resume()

    assert strategy.state.value == "active"
    assert strategy.reentry_enabled is True


def test_trigger_reference_models_use_distinct_second_dca_bases() -> None:
    triggers: dict[DcaTriggerReference, float] = {}
    for reference in DcaTriggerReference:
        strategy = DcaStrategy(StrategyConfig(), trigger_reference=reference)
        strategy.resume()
        strategy.begin_cycle(100.0)
        first_trigger = strategy.next_dca_trigger_price()
        assert first_trigger is not None
        strategy.apply_dca_fill(first_trigger)
        second_trigger = strategy.next_dca_trigger_price()
        assert second_trigger is not None
        triggers[reference] = second_trigger

    assert triggers[DcaTriggerReference.PREVIOUS_FILL] < triggers[
        DcaTriggerReference.WEIGHTED_AVERAGE
    ]
    assert triggers[DcaTriggerReference.WEIGHTED_AVERAGE] < triggers[
        DcaTriggerReference.INITIAL_ENTRY
    ]


def test_non_average_reference_rejects_aggregated_deep_restore() -> None:
    strategy = DcaStrategy(
        StrategyConfig(), trigger_reference=DcaTriggerReference.PREVIOUS_FILL
    )

    with pytest.raises(ValueError, match="individual restored fill history"):
        strategy.restore_cycle(
            average_entry=98.0,
            total_qty=1.0,
            dca_level=1,
            last_order_qty=0.5,
        )
