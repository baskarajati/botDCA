from botdca.backtest import Candle, IntrabarPath
from botdca.calibration import calibrate_strategy_history
from botdca.domain import DcaStep
from botdca.strategy import StrategyConfig
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)


def _export(cycle_count: int) -> tuple[TraderExportData, list[Candle]]:
    orders: list[TraderOrder] = []
    cycles: list[TraderCycle] = []
    candles: list[Candle] = []
    for index in range(cycle_count):
        opened_ms = index * 180_000
        closed_ms = opened_ms + 60_000
        order = TraderOrder(
            source_row=index + 2,
            symbol="HYPEUSDT",
            side="Long",
            opened_ms=opened_ms,
            closed_ms=closed_ms,
            order_qty=1.0,
            average_entry=100.0,
            closing_price=101.1,
            leverage=24.0,
            roi_percent=20.0,
            followers=1,
        )
        orders.append(order)
        cycles.append(
            TraderCycle(
                opened_ms=opened_ms,
                closed_ms=closed_ms,
                average_entry=100.0,
                closing_price=101.1,
                dca_count=0,
                total_qty=1.0,
                leverage=24.0,
                roi_percent_median=20.0,
                orders=(order,),
            )
        )
        candles.extend(
            [
                Candle(start_ms=opened_ms, open=100, high=100, low=100, close=100),
                Candle(
                    start_ms=closed_ms,
                    open=100,
                    high=101.1,
                    low=100,
                    close=101.1,
                ),
                Candle(
                    start_ms=opened_ms + 120_000,
                    open=101.1,
                    high=101.1,
                    low=101.1,
                    close=101.1,
                ),
            ]
        )
    export = TraderExportData(
        timezone_name="UTC",
        orders=tuple(orders),
        cycles=tuple(cycles),
        grouping=CycleGroupingDiagnostics(
            source_rows=cycle_count,
            exact_key_groups=cycle_count,
            grouped_cycles=cycle_count,
            close_fragment_groups_merged=0,
            overlapping_cycles=0,
        ),
    )
    return export, candles


def test_calibration_uses_chronological_holdout_and_preserves_sizing() -> None:
    export, candles = _export(10)
    steps = (DcaStep(1.05, 1.339), DcaStep(1.41, 1.539))

    result = calibrate_strategy_history(
        export,
        candles,
        baseline_config=StrategyConfig(tp_percent=1.09, dca_steps=steps),
        intrabar_paths=(IntrabarPath.LOW_FIRST,),
        train_fraction=0.7,
        minimum_regime_cycles=10,
        minimum_level_samples=2,
        reentry_delay_candidates_seconds=(0, 48),
    )

    assert result["calibrated_regimes"] == 1
    regime = result["regimes"][0]
    assert regime["training_cycles"] == 7
    assert regime["validation_cycles"] == 3
    assert regime["training_closed_through_ms"] < regime["validation_opened_from_ms"]
    assert regime["parameter_evidence"]["eligible_dca_levels"] == []
    assert "holdout_mismatch_summary" in regime
    calibrated_steps = regime["calibrated_parameters"]["dca_steps"]
    assert [item["size_multiplier_from_previous"] for item in calibrated_steps] == [
        1.339,
        1.539,
    ]
    assert result["conclusion"]["runtime_defaults_changed"] is False


def test_calibration_skips_small_regimes() -> None:
    export, candles = _export(4)

    result = calibrate_strategy_history(
        export,
        candles,
        intrabar_paths=(IntrabarPath.LOW_FIRST,),
        minimum_regime_cycles=5,
    )

    assert result["calibrated_regimes"] == 0
    assert result["conclusion"]["holdout_status"] == "not_validated"
    assert result["skipped_regimes"] == [
        {
            "regime_index": 1,
            "completed_cycles": 4,
            "reason": "insufficient_cycles_for_chronological_holdout",
        }
    ]
