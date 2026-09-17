from botdca.strategy import DcaStrategy, StrategyConfig


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
