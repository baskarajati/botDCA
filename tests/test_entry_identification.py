from botdca.backtest import Candle
from botdca.entry_identification import identify_entry_and_deep_dca_behavior
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)


def _single_order_cycle(index: int) -> tuple[TraderCycle, TraderOrder, Candle]:
    opened_ms = index * 60_000 + 4_000
    closed_ms = index * 60_000 + 30_000
    price = 100 + index * 0.01
    order = TraderOrder(
        source_row=index + 2,
        symbol="HYPEUSDT",
        side="Long",
        opened_ms=opened_ms,
        closed_ms=closed_ms,
        order_qty=1.0,
        average_entry=price,
        closing_price=price * 1.01,
        leverage=24.0,
        roi_percent=20.0,
        followers=1,
    )
    cycle = TraderCycle(
        opened_ms=opened_ms,
        closed_ms=closed_ms,
        average_entry=price,
        closing_price=price * 1.01,
        dca_count=0,
        total_qty=1.0,
        leverage=24.0,
        roi_percent_median=20.0,
        orders=(order,),
    )
    candle = Candle(
        start_ms=index * 60_000,
        open=price,
        high=price,
        low=price,
        close=price,
    )
    return cycle, order, candle


def test_next_minute_scheduler_beats_literal_fixed_delay_on_holdout() -> None:
    cycles: list[TraderCycle] = []
    orders: list[TraderOrder] = []
    candles: list[Candle] = []
    for index in range(10):
        cycle, order, candle = _single_order_cycle(index)
        cycles.append(cycle)
        orders.append(order)
        candles.append(candle)
    export = TraderExportData(
        timezone_name="UTC",
        orders=tuple(orders),
        cycles=tuple(cycles),
        grouping=CycleGroupingDiagnostics(
            source_rows=10,
            exact_key_groups=10,
            grouped_cycles=10,
            close_fragment_groups_merged=0,
            overlapping_cycles=0,
        ),
    )

    result = identify_entry_and_deep_dca_behavior(export, candles)

    holdout = result["reentry_timing"]["holdout"]
    assert holdout["immediate_next_minute_percent"] == 100.0
    assert holdout["scheduler_model_absolute_error_seconds"]["median"] == 0.0
    assert holdout["fixed_delay_model_absolute_error_seconds"]["median"] == 14.0
    assert result["conclusion"]["next_candle_open_replay_proxy"] == "supported"
    assert result["first_entry_price"]["entry_price_inside_entry_minute_range"] == 10


def _deep_cycle(
    *,
    source_row: int,
    start_ms: int,
    leverage: float,
    quantities: list[float],
    prices: list[float],
) -> tuple[TraderCycle, list[TraderOrder], list[Candle]]:
    total_quantity = sum(quantities)
    average = sum(quantity * price for quantity, price in zip(quantities, prices)) / total_quantity
    closed_ms = start_ms + len(prices) * 60_000 + 30_000
    orders = [
        TraderOrder(
            source_row=source_row + index,
            symbol="HYPEUSDT",
            side="Long",
            opened_ms=start_ms + index * 60_000 + 5_000,
            closed_ms=closed_ms,
            order_qty=quantity,
            average_entry=average,
            closing_price=average * 1.01,
            leverage=leverage,
            roi_percent=20.0,
            followers=1,
        )
        for index, quantity in enumerate(quantities)
    ]
    candles = [
        Candle(
            start_ms=start_ms + index * 60_000,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        for index, price in enumerate(prices)
    ]
    cycle = TraderCycle(
        opened_ms=orders[0].opened_ms,
        closed_ms=closed_ms,
        average_entry=average,
        closing_price=average * 1.01,
        dca_count=len(orders) - 1,
        total_qty=total_quantity,
        leverage=leverage,
        roi_percent_median=20.0,
        orders=tuple(orders),
    )
    return cycle, orders, candles


def test_deep_levels_remain_separated_by_regime_and_detect_capped_size() -> None:
    mature_quantities = [1, 1.34, 2.06, 2.95, 4.25, 6.16, 8.93, 13.1, 19.2, 19.32]
    mature_prices = [100, 98.95, 97.2, 95.8, 93.7, 90.8, 87.8, 83.5, 79.2, 74.8]
    old_quantities = [1.0]
    for _ in range(10):
        old_quantities.append(old_quantities[-1] * 1.41)
    old_prices = [100, 99, 97.5, 96, 94, 92, 90, 88, 86, 84, 82]
    mature, mature_orders, mature_candles = _deep_cycle(
        source_row=2,
        start_ms=0,
        leverage=24,
        quantities=mature_quantities,
        prices=mature_prices,
    )
    old, old_orders, old_candles = _deep_cycle(
        source_row=20,
        start_ms=((mature.closed_ms // 60_000) + 2) * 60_000,
        leverage=20,
        quantities=old_quantities,
        prices=old_prices,
    )
    export = TraderExportData(
        timezone_name="UTC",
        orders=tuple(mature_orders + old_orders),
        cycles=(mature, old),
        grouping=CycleGroupingDiagnostics(
            source_rows=len(mature_orders) + len(old_orders),
            exact_key_groups=2,
            grouped_cycles=2,
            close_fragment_groups_merged=0,
            overlapping_cycles=0,
        ),
    )

    result = identify_entry_and_deep_dca_behavior(
        export,
        mature_candles + old_candles,
    )

    summary = {
        (item["leverage"], item["level"]): item
        for item in result["deep_dca_summary"]
    }
    assert summary[(24, 9)]["samples"] == 1
    assert summary[(24, 9)]["eligible_for_runtime_configuration"] is False
    assert "capped emergency add" in summary[(24, 9)]["interpretation"]
    assert summary[(20, 10)]["samples"] == 1
    assert summary[(20, 10)]["eligible_for_runtime_configuration"] is False
    assert "older 20x" in summary[(20, 10)]["interpretation"]
    assert result["conclusion"]["runtime_defaults_changed"] is False
