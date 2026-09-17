from pathlib import Path

import pytest

from botdca.backtest import Candle
from botdca.historical_profile import build_historical_profile, load_or_fetch_candles
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)


def _order(
    row: int,
    *,
    opened_ms: int,
    closed_ms: int,
    qty: float,
    average_entry: float,
    closing_price: float,
    leverage: float = 24.0,
) -> TraderOrder:
    return TraderOrder(
        source_row=row,
        symbol="HYPEUSDT",
        side="Long",
        opened_ms=opened_ms,
        closed_ms=closed_ms,
        order_qty=qty,
        average_entry=average_entry,
        closing_price=closing_price,
        leverage=leverage,
        roi_percent=20.0,
        followers=1,
    )


def _export() -> TraderExportData:
    first_orders = (
        _order(
            2,
            opened_ms=0,
            closed_ms=120_000,
            qty=1.0,
            average_entry=98.6666666667,
            closing_price=99.75,
        ),
        _order(
            3,
            opened_ms=60_000,
            closed_ms=120_000,
            qty=2.0,
            average_entry=98.6666666667,
            closing_price=99.75,
        ),
    )
    second_orders = (
        _order(
            4,
            opened_ms=180_000,
            closed_ms=240_000,
            qty=1.0,
            average_entry=100.0,
            closing_price=100.99,
        ),
    )
    cycles = (
        TraderCycle(
            opened_ms=0,
            closed_ms=120_000,
            average_entry=98.6666666667,
            closing_price=99.75,
            dca_count=1,
            total_qty=3.0,
            leverage=24.0,
            roi_percent_median=20.0,
            orders=first_orders,
        ),
        TraderCycle(
            opened_ms=180_000,
            closed_ms=240_000,
            average_entry=100.0,
            closing_price=100.99,
            dca_count=0,
            total_qty=1.0,
            leverage=24.0,
            roi_percent_median=20.0,
            orders=second_orders,
        ),
    )
    return TraderExportData(
        timezone_name="UTC",
        orders=first_orders + second_orders,
        cycles=cycles,
        grouping=CycleGroupingDiagnostics(
            source_rows=3,
            exact_key_groups=2,
            grouped_cycles=2,
            close_fragment_groups_merged=0,
            overlapping_cycles=0,
        ),
    )


def test_profile_reports_depth_multipliers_reentry_and_market_alignment() -> None:
    candles = [
        Candle(start_ms=0, open=100, high=100, low=100, close=100),
        Candle(start_ms=60_000, open=98, high=98, low=98, close=98),
        Candle(start_ms=120_000, open=99.7, high=99.8, low=99.7, close=99.75),
        Candle(start_ms=180_000, open=100, high=100, low=100, close=100),
        Candle(start_ms=240_000, open=100.98, high=101, low=100.98, close=100.99),
    ]

    profile = build_historical_profile(_export(), candles=candles)

    assert profile["dataset"]["completed_cycles"] == 2
    assert profile["dca_depth"]["exact_counts"] == {"0": 1, "1": 1}
    assert profile["quantity_multiplier_from_previous"]["1"]["median"] == 2.0
    assert profile["reentry_gap_seconds"]["nonnegative"]["median"] == 60.0
    assert len(profile["regimes"]) == 2
    assert profile["market_alignment"]["weighted_average_feasible_percent"] == 100.0
    assert profile["market_alignment"]["closing_price_in_range_percent"] == 100.0


class _FakeKlineClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_linear_candles(self, **_: object) -> list[Candle]:
        self.calls += 1
        return [Candle(start_ms=0, open=100, high=101, low=99, close=100)]


def test_candle_cache_is_reused(tmp_path: Path) -> None:
    client = _FakeKlineClient()

    first, cache_path, first_hit = load_or_fetch_candles(
        client,  # type: ignore[arg-type]
        symbol="HYPEUSDT",
        start_ms=0,
        end_ms=0,
        cache_dir=tmp_path,
    )
    second, second_path, second_hit = load_or_fetch_candles(
        client,  # type: ignore[arg-type]
        symbol="HYPEUSDT",
        start_ms=0,
        end_ms=0,
        cache_dir=tmp_path,
    )

    assert client.calls == 1
    assert first == second
    assert cache_path == second_path
    assert first_hit is False
    assert second_hit is True


def test_profile_rejects_non_positive_regime_threshold() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_historical_profile(_export(), tp_change_threshold_percent=0)
