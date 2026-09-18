import csv
import gzip

from botdca.backtest import Candle
from botdca.second_level_audit import audit_second_level_entry_uncertainty
from botdca.trader_export import (
    CycleGroupingDiagnostics,
    TraderCycle,
    TraderExportData,
    TraderOrder,
)


def test_second_level_prices_recover_minute_open_mismatch(tmp_path) -> None:
    orders: list[TraderOrder] = []
    cycles: list[TraderCycle] = []
    minute_candles: list[Candle] = []
    public_rows: list[list[str]] = []
    for index in range(4):
        minute_start = index * 3_600_000
        opened_ms = minute_start + 4_000
        closed_ms = minute_start + 630_000
        order = TraderOrder(
            source_row=index + 2,
            symbol="HYPEUSDT",
            side="Long",
            opened_ms=opened_ms,
            closed_ms=closed_ms,
            order_qty=1.0,
            average_entry=100.0,
            closing_price=101.0,
            leverage=24.0,
            roi_percent=20.0,
            followers=1,
        )
        cycle = TraderCycle(
            opened_ms=opened_ms,
            closed_ms=closed_ms,
            average_entry=100.0,
            closing_price=101.0,
            dca_count=0,
            total_qty=1.0,
            leverage=24.0,
            roi_percent_median=20.0,
            orders=(order,),
        )
        orders.append(order)
        cycles.append(cycle)
        minute_candles.extend(
            [
                Candle(
                    start_ms=minute_start,
                    open=99.0,
                    high=100.0,
                    low=99.0,
                    close=100.0,
                ),
                Candle(
                    start_ms=closed_ms // 60_000 * 60_000,
                    open=100.0,
                    high=101.0,
                    low=100.0,
                    close=101.0,
                ),
            ]
        )
        public_rows.extend(
            [
                [f"{opened_ms / 1000 + 0.1}", "HYPEUSDT", "Buy", "1", "100", f"e{index}"],
                [f"{closed_ms / 1000 + 0.1}", "HYPEUSDT", "Buy", "1", "100", f"c{index}a"],
                [f"{closed_ms / 1000 + 0.2}", "HYPEUSDT", "Buy", "1", "101", f"c{index}b"],
            ]
        )

    archive = tmp_path / "HYPEUSDT1970-01-01.csv.gz"
    with gzip.open(archive, mode="wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "symbol", "side", "size", "price", "trdMatchID"])
        writer.writerows(public_rows)
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

    result = audit_second_level_entry_uncertainty(
        export,
        minute_candles,
        tmp_path,
        train_fraction=0.5,
        minimum_regime_cycles=2,
    )

    assert result["unique_minute_level_unresolved_cycles"] == 2
    assert result["conclusion"]["resolution_recoverable_cycles"] == 2
    assert result["conclusion"]["model_mismatch_cycles"] == 0
    assert all(
        item["status"] == "resolution_recoverable"
        for item in result["cycle_results"]
    )

    fixed = audit_second_level_entry_uncertainty(
        export,
        minute_candles,
        tmp_path,
        train_fraction=0.5,
        minimum_regime_cycles=2,
        fixed_entry_all_cycles=True,
    )
    assert len(fixed["cycle_results"]) == 2
    for path_metrics in fixed["fixed_entry_metrics"].values():
        assert path_metrics["joint_close_and_depth_matches"] == 2
        assert path_metrics["replayed_cycles"] == 2
    assert all(
        attempt["entry_candidate"] == "first_public_trade"
        for cycle in fixed["cycle_results"]
        for attempt in cycle["attempts"]
    )
