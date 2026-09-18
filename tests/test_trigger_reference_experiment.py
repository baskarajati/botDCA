from botdca.backtest import Candle
from botdca.strategy import DcaTriggerReference, StrategyConfig
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)
from botdca.trigger_reference_experiment import compare_dca_trigger_references


def _flat_cycle(index: int) -> tuple[TraderCycle, TraderOrder, list[Candle]]:
    start_ms = index * 600_000
    close_ms = start_ms + 60_000
    order = TraderOrder(
        source_row=index + 2,
        symbol="HYPEUSDT",
        side="Long",
        opened_ms=start_ms + 4_000,
        closed_ms=close_ms,
        order_qty=1.0,
        average_entry=100.0,
        closing_price=101.0,
        leverage=24.0,
        roi_percent=20.0,
        followers=1,
    )
    cycle = TraderCycle(
        opened_ms=order.opened_ms,
        closed_ms=order.closed_ms,
        average_entry=100.0,
        closing_price=101.0,
        dca_count=0,
        total_qty=1.0,
        leverage=24.0,
        roi_percent_median=20.0,
        orders=(order,),
    )
    candles = [
        Candle(start_ms=start_ms, open=100, high=100, low=100, close=100),
        Candle(start_ms=close_ms, open=100, high=101, low=100, close=101),
    ]
    return cycle, order, candles


def test_reference_experiment_prefers_current_model_on_tie() -> None:
    cycles: list[TraderCycle] = []
    orders: list[TraderOrder] = []
    candles: list[Candle] = []
    for index in range(4):
        cycle, order, cycle_candles = _flat_cycle(index)
        cycles.append(cycle)
        orders.append(order)
        candles.extend(cycle_candles)
    export = TraderExportData(
        timezone_name="UTC",
        orders=tuple(orders),
        cycles=tuple(cycles),
        grouping=CycleGroupingDiagnostics(
            source_rows=4,
            exact_key_groups=4,
            grouped_cycles=4,
            close_fragment_groups_merged=0,
            overlapping_cycles=0,
        ),
    )

    result = compare_dca_trigger_references(
        export,
        candles,
        baseline_config=StrategyConfig(tp_percent=1.0),
        minimum_regime_cycles=2,
    )

    models = {item["reference"]: item for item in result["aggregate_models"]}
    assert result["conclusion"]["best_observed_reference"] == (
        DcaTriggerReference.WEIGHTED_AVERAGE.value
    )
    assert result["conclusion"]["defensible_alternative_to_current"] is False
    assert models[DcaTriggerReference.INITIAL_ENTRY.value][
        "normalized_full_ladder_executable_on_descent"
    ] is False
