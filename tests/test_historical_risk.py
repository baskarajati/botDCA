import pytest

from botdca.backtest import Candle, IntrabarPath
from botdca.historical_risk import (
    build_historical_risk_report,
    build_ladder_stress_analysis,
)
from botdca.strategy import StrategyConfig
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)


def _parameter_summary(config: StrategyConfig, delay: float) -> dict[str, object]:
    return {
        "leverage": config.leverage,
        "tp_percent": config.tp_percent,
        "reentry_delay_seconds": delay,
        "dca_steps": [
            {
                "drop_percent_from_average": step.drop_percent_from_average,
                "size_multiplier_from_previous": step.size_multiplier_from_previous,
            }
            for step in config.dca_steps
        ],
    }


def _history(cycle_count: int = 6) -> tuple[TraderExportData, list[Candle]]:
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
            closing_price=101.09,
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
                closing_price=101.09,
                dca_count=0,
                total_qty=1.0,
                leverage=24.0,
                roi_percent_median=20.0,
                orders=(order,),
            )
        )
        candles.extend(
            [
                Candle(start_ms=opened_ms, open=100, high=100, low=99, close=99),
                Candle(
                    start_ms=closed_ms,
                    open=99,
                    high=101.2,
                    low=99,
                    close=101.09,
                ),
                Candle(
                    start_ms=opened_ms + 120_000,
                    open=101.09,
                    high=101.09,
                    low=101.09,
                    close=101.09,
                ),
            ]
        )
    return (
        TraderExportData(
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
        ),
        candles,
    )


def test_post_ladder_stress_is_monotonic_and_explicitly_approximate() -> None:
    result = build_ladder_stress_analysis(StrategyConfig())

    assert result["is_exact_bybit_uta_liquidation_model"] is False
    assert result["ladder"]["dca_levels_exhausted"] == 8
    assert result["ladder"]["entry_notional_multiple_of_initial"] > 10
    losses = [scenario["floating_loss_usdt"] for scenario in result["scenarios"]]
    reserves = [
        scenario["approximate_account_equity_reserve_usdt"]
        for scenario in result["scenarios"]
    ]
    assert losses == sorted(losses)
    assert reserves == sorted(reserves)
    assert losses[-1] > losses[0]


def test_historical_risk_report_separates_fees_and_excludes_funding() -> None:
    export, candles = _history()
    baseline = StrategyConfig(tp_percent=1.09)
    calibrated = StrategyConfig(tp_percent=1.10)
    calibration = {
        "calibration": {
            "regimes": [
                {
                    "regime_index": 1,
                    "completed_cycles": 6,
                    "baseline_parameters": _parameter_summary(baseline, 48),
                    "calibrated_parameters": _parameter_summary(calibrated, 60),
                }
            ]
        }
    }

    report = build_historical_risk_report(
        export,
        candles,
        calibration,
        intrabar_paths=(IntrabarPath.LOW_FIRST,),
    )

    assert report["coverage"]["analyzed_completed_cycles"] == 6
    assert report["observed_dca_depth_full_export"]["at_least"]["0"] == 6
    assert report["methodology"]["funding"]["funding_paid_usdt"] is None
    assert report["hypothesis_evaluation"]["status"] == "supported"
    for configuration in ("baseline", "calibrated"):
        aggregate = report["comparisons"][configuration]["low-first"]["aggregate"]
        assert aggregate["completed_cycles"] > 0
        assert aggregate["total_fees_paid_usdt"] == pytest.approx(
            aggregate["entry_fees_paid_usdt"] + aggregate["exit_fees_paid_usdt"]
        )
        assert aggregate["net_pnl_includes_funding"] is False
        assert aggregate["longest_underwater_seconds"] >= 0
