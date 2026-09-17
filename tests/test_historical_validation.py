from botdca.backtest import Candle, IntrabarPath
from botdca.historical_validation import validate_strategy_history
from botdca.strategy import StrategyConfig
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)


def _order(row: int, opened_ms: int, closed_ms: int) -> TraderOrder:
    return TraderOrder(
        source_row=row,
        symbol="HYPEUSDT",
        side="Long",
        opened_ms=opened_ms,
        closed_ms=closed_ms,
        order_qty=1.0,
        average_entry=100.0,
        closing_price=101.09,
        leverage=24.0,
        roi_percent=20.0,
        followers=1,
    )


def _cycle(order: TraderOrder) -> TraderCycle:
    return TraderCycle(
        opened_ms=order.opened_ms,
        closed_ms=order.closed_ms,
        average_entry=100.0,
        closing_price=101.09,
        dca_count=0,
        total_qty=1.0,
        leverage=24.0,
        roi_percent_median=20.0,
        orders=(order,),
    )


def test_anchored_and_continuous_validation_match_synthetic_cycles() -> None:
    orders = (_order(2, 0, 60_000), _order(3, 120_000, 180_000))
    export = TraderExportData(
        timezone_name="UTC",
        orders=orders,
        cycles=tuple(_cycle(order) for order in orders),
        grouping=CycleGroupingDiagnostics(
            source_rows=2,
            exact_key_groups=2,
            grouped_cycles=2,
            close_fragment_groups_merged=0,
            overlapping_cycles=0,
        ),
    )
    candles = [
        Candle(start_ms=0, open=100, high=100, low=100, close=100),
        Candle(start_ms=60_000, open=100, high=101.09, low=100, close=101.09),
        Candle(start_ms=120_000, open=100, high=100, low=100, close=100),
        Candle(start_ms=180_000, open=100, high=101.09, low=100, close=101.09),
        Candle(start_ms=240_000, open=101.09, high=101.09, low=101.09, close=101.09),
        Candle(start_ms=300_000, open=101.09, high=101.09, low=101.09, close=101.09),
        Candle(start_ms=360_000, open=101.09, high=101.09, low=101.09, close=101.09),
        Candle(start_ms=420_000, open=101.09, high=101.09, low=101.09, close=101.09),
        Candle(start_ms=480_000, open=101.09, high=101.09, low=101.09, close=101.09),
    ]

    result = validate_strategy_history(
        export,
        candles,
        baseline_config=StrategyConfig(tp_percent=1.09),
        intrabar_paths=(IntrabarPath.LOW_FIRST,),
        maximum_close_time_error_seconds=300,
        reentry_delay_seconds=48,
    )

    assert result["regime_count"] == 1
    assert result["conclusion"]["reconstructed_strategy_status"] == "validated"
    assert len(result["scenarios"]) == 2
    for scenario in result["scenarios"]:
        assert scenario["anchored"]["matched_cycles"] == 2
        assert scenario["continuous"]["matched_cycles"] == 2
        assert scenario["anchored"]["exact_dca_depth_match_percent"] == 100.0
        assert scenario["continuous"]["close_time_error_seconds"]["median"] == 0.0
