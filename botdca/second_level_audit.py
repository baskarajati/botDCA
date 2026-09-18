from __future__ import annotations

import csv
import gzip
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from botdca.backtest import Candle, IntrabarPath, ReplayEngine
from botdca.historical_profile import split_regimes
from botdca.historical_validation import evaluate_anchored_cycles
from botdca.strategy import StrategyConfig
from botdca.trader_export import TraderCycle, TraderExportData

SECOND_MS = 1_000


def audit_second_level_entry_uncertainty(
    export: TraderExportData,
    minute_candles: list[Candle],
    archive_dir: str | Path,
    *,
    baseline_config: StrategyConfig | None = None,
    train_fraction: float = 0.70,
    minimum_regime_cycles: int = 30,
    maximum_close_time_error_seconds: float = 300.0,
    tp_change_threshold_percent: float = 0.04,
    taker_fee_rate: float = 0.00055,
    fixed_entry_all_cycles: bool = False,
) -> dict[str, Any]:
    """Diagnose one-minute holdout mismatches with archived public trades."""

    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    if minimum_regime_cycles < 2:
        raise ValueError("minimum_regime_cycles must be at least two")
    if maximum_close_time_error_seconds <= 0:
        raise ValueError("maximum_close_time_error_seconds must be positive")
    if not export.cycles:
        raise ValueError("at least one trader cycle is required")
    if not minute_candles:
        raise ValueError("at least one minute candle is required")

    archive_path = Path(archive_dir).expanduser()
    baseline = baseline_config or StrategyConfig()
    regimes = split_regimes(
        list(export.cycles),
        threshold=tp_change_threshold_percent,
    )
    unresolved: list[dict[str, Any]] = []
    evaluated_cycles = 0
    minute_matches = {path.value: 0 for path in IntrabarPath}

    for regime_index, regime in enumerate(regimes, start=1):
        if len(regime) < minimum_regime_cycles:
            continue
        split_index = max(1, min(len(regime) - 1, int(len(regime) * train_fraction)))
        training = regime[:split_index]
        holdout = regime[split_index:]
        config = replace(
            baseline,
            leverage=int(regime[0].leverage),
            tp_percent=round(median(_tp_percent(cycle) for cycle in training), 6),
        )
        for cycle in holdout:
            evaluated_cycles += 1
            failed_paths: list[str] = []
            for path in IntrabarPath:
                metrics = evaluate_anchored_cycles(
                    [cycle],
                    minute_candles,
                    config,
                    intrabar_path=path,
                    maximum_close_time_error_seconds=maximum_close_time_error_seconds,
                    taker_fee_rate=taker_fee_rate,
                )
                if metrics["matched_cycles"] == 1:
                    minute_matches[path.value] += 1
                else:
                    failed_paths.append(path.value)
            if failed_paths or fixed_entry_all_cycles:
                unresolved.append(
                    {
                        "regime_index": regime_index,
                        "config": config,
                        "cycle": cycle,
                        "failed_minute_paths": failed_paths,
                    }
                )

    windows = [
        _CycleWindow(
            key=_cycle_key(item["cycle"]),
            start_ms=item["cycle"].opened_ms // SECOND_MS * SECOND_MS,
            end_ms=(
                item["cycle"].closed_ms
                + int(maximum_close_time_error_seconds * 1000)
            ),
            order_timestamps_ms=tuple(
                order.opened_ms // SECOND_MS * SECOND_MS
                for order in item["cycle"].orders
            ),
            close_timestamp_ms=item["cycle"].closed_ms // SECOND_MS * SECOND_MS,
        )
        for item in unresolved
    ]
    second_candles, source_quality = load_archived_trade_seconds(
        archive_path,
        symbol=baseline.symbol,
        windows=windows,
    )

    cycle_results: list[dict[str, Any]] = []
    for item in unresolved:
        cycle = item["cycle"]
        key = _cycle_key(cycle)
        candles = second_candles.get(key, [])
        coverage = source_quality["window_coverage"].get(key, {})
        attempts: list[dict[str, Any]] = []
        if candles:
            candidates = (
                [("first_public_trade", candles[0].open)]
                if fixed_entry_all_cycles
                else _entry_candidates(cycle, candles[0])
            )
            for candidate_name, candidate_price in candidates:
                candidate_candles = _with_first_open(candles, candidate_price)
                for path in IntrabarPath:
                    replay = ReplayEngine(
                        item["config"],
                        intrabar_path=path,
                        taker_fee_rate=taker_fee_rate,
                        auto_reentry=False,
                        maximum_completed_cycles=1,
                    ).run(candidate_candles)
                    completed = replay.cycles[0] if replay.cycles else None
                    close_error = (
                        abs(completed.closed_ms - cycle.closed_ms) / 1000
                        if completed is not None
                        else None
                    )
                    depth_matches = (
                        completed is not None and completed.dca_level == cycle.dca_count
                    )
                    closes_in_window = (
                        close_error is not None
                        and close_error <= maximum_close_time_error_seconds
                    )
                    attempts.append(
                        {
                            "entry_candidate": candidate_name,
                            "entry_price": candidate_price,
                            "intrabar_path": path.value,
                            "completed": completed is not None,
                            "simulated_dca_depth": (
                                completed.dca_level if completed is not None else None
                            ),
                            "depth_matches": depth_matches,
                            "close_time_error_seconds": close_error,
                            "matches": depth_matches and closes_in_window,
                        }
                    )

        matches = [attempt for attempt in attempts if attempt["matches"]]
        complete_coverage = bool(coverage.get("entry_second_present")) and all(
            coverage.get("order_seconds_present", [])
        ) and bool(coverage.get("close_second_present"))
        if matches:
            status = "resolution_recoverable"
        elif not complete_coverage:
            status = "data_gap"
        else:
            status = "model_mismatch"
        cycle_results.append(
            {
                "regime_index": item["regime_index"],
                "opened_utc": _iso_ms(cycle.opened_ms),
                "closed_utc": _iso_ms(cycle.closed_ms),
                "observed_dca_depth": cycle.dca_count,
                "failed_minute_paths": item["failed_minute_paths"],
                "source_coverage": coverage,
                "status": status,
                "matching_attempts": len(matches),
                "best_matches": matches,
                "attempts": attempts,
            }
        )

    status_counts = defaultdict(int)
    for result in cycle_results:
        status_counts[result["status"]] += 1
    recoverable = status_counts["resolution_recoverable"]
    fixed_metrics = {}
    if fixed_entry_all_cycles:
        for path in IntrabarPath:
            attempts = [
                attempt
                for result in cycle_results
                for attempt in result["attempts"]
                if attempt["intrabar_path"] == path.value
            ]
            close_matches = sum(
                attempt["close_time_error_seconds"] is not None
                and attempt["close_time_error_seconds"] <= maximum_close_time_error_seconds
                for attempt in attempts
            )
            joint_matches = sum(attempt["matches"] for attempt in attempts)
            fixed_metrics[path.value] = {
                "cycles": evaluated_cycles,
                "replayed_cycles": len(attempts),
                "close_matches": close_matches,
                "joint_close_and_depth_matches": joint_matches,
                "joint_match_percent": joint_matches / evaluated_cycles * 100,
            }
    feasibility_by_path: dict[str, dict[str, float | int]] = {}
    for path in IntrabarPath:
        recovered_failures = sum(
            path.value in result["failed_minute_paths"]
            and any(
                attempt["matches"] and attempt["intrabar_path"] == path.value
                for attempt in result["attempts"]
            )
            for result in cycle_results
        )
        potential_matches = minute_matches[path.value] + recovered_failures
        feasibility_by_path[path.value] = {
            "minute_level_matches": minute_matches[path.value],
            "resolution_recoverable_failures": recovered_failures,
            "potential_matches_after_entry_resolution": potential_matches,
            "potential_match_rate_percent": (
                potential_matches / evaluated_cycles * 100 if evaluated_cycles else 0.0
            ),
        }
    return {
        "methodology": {
            "fixed_entry_all_cycles": fixed_entry_all_cycles,
            "entry_rule": (
                "first archived public trade at or after exported entry timestamp"
                if fixed_entry_all_cycles
                else "entry-price feasibility envelope"
            ),
            "scope": "diagnostic after holdout opening; not untouched validation",
            "source": "Bybit public HYPEUSDT trade archive aggregated to one-second OHLC",
            "entry_candidates": [
                "entry-second open",
                "entry-second low",
                "entry-second high",
                "exact exported entry for DCA0 when inside the entry-second range",
            ],
            "success_rule": (
                "observed DCA depth and close within the unchanged 300-second window"
            ),
            "parameters_retuned_after_holdout": False,
            "limitations": [
                "Public trades identify market prints, not the master's private execution.",
                "One-second OHLC still has unknown intrasecond ordering.",
                "Layered cycles still lack exact individual private fill prices.",
                "Recovered cycles show data-resolution plausibility, not a new validation pass.",
            ],
        },
        "evaluated_holdout_cycles": evaluated_cycles,
        "fixed_entry_metrics": fixed_metrics,
        "minute_level_matches": minute_matches,
        "resolution_feasibility_upper_bound": feasibility_by_path,
        "unique_minute_level_unresolved_cycles": len(unresolved),
        "source_quality": source_quality,
        "cycle_results": cycle_results,
        "conclusion": {
            "resolution_recoverable_cycles": recoverable,
            "model_mismatch_cycles": status_counts["model_mismatch"],
            "data_gap_cycles": status_counts["data_gap"],
            "could_explain_one_or_more_remaining_mismatches": recoverable > 0,
            "untouched_validation_restored": False,
            "runtime_defaults_changed": False,
        },
    }


class _CycleWindow:
    def __init__(
        self,
        *,
        key: str,
        start_ms: int,
        end_ms: int,
        order_timestamps_ms: tuple[int, ...],
        close_timestamp_ms: int,
    ) -> None:
        self.key = key
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.order_timestamps_ms = order_timestamps_ms
        self.close_timestamp_ms = close_timestamp_ms


def load_archived_trade_seconds(
    archive_dir: Path,
    *,
    symbol: str,
    windows: list[_CycleWindow],
) -> tuple[dict[str, list[Candle]], dict[str, Any]]:
    """Stream archived trades once per date and retain only requested windows."""

    by_date: dict[str, list[_CycleWindow]] = defaultdict(list)
    for window in windows:
        for date in _utc_dates(window.start_ms, window.end_ms):
            by_date[date].append(window)

    mutable: dict[str, dict[int, list[float | int]]] = {
        window.key: {} for window in windows
    }
    trade_ids: dict[str, set[str]] = {window.key: set() for window in windows}
    duplicate_ids = defaultdict(int)
    files: list[dict[str, Any]] = []
    total_rows = 0
    retained_rows = 0

    for date, date_windows in sorted(by_date.items()):
        path = archive_dir / f"{symbol.upper()}{date}.csv.gz"
        if not path.exists():
            files.append({"date": date, "path": path.name, "status": "missing"})
            continue
        rows = 0
        retained = 0
        out_of_order = 0
        previous_timestamp_ms: int | None = None
        with gzip.open(path, mode="rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"timestamp", "symbol", "price", "trdMatchID"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ValueError(f"unexpected public-trade schema in {path.name}")
            for row in reader:
                rows += 1
                timestamp_ms = int(float(row["timestamp"]) * 1000)
                if previous_timestamp_ms is not None and timestamp_ms < previous_timestamp_ms:
                    out_of_order += 1
                previous_timestamp_ms = timestamp_ms
                if row["symbol"] != symbol.upper():
                    continue
                price = float(row["price"])
                for window in date_windows:
                    if not window.start_ms <= timestamp_ms <= window.end_ms:
                        continue
                    retained += 1
                    trade_id = row["trdMatchID"]
                    if trade_id in trade_ids[window.key]:
                        duplicate_ids[window.key] += 1
                    else:
                        trade_ids[window.key].add(trade_id)
                    second_ms = timestamp_ms // SECOND_MS * SECOND_MS
                    values = mutable[window.key].get(second_ms)
                    if values is None:
                        mutable[window.key][second_ms] = [price, price, price, price, 1]
                    else:
                        values[1] = max(float(values[1]), price)
                        values[2] = min(float(values[2]), price)
                        values[3] = price
                        values[4] = int(values[4]) + 1
        total_rows += rows
        retained_rows += retained
        files.append(
            {
                "date": date,
                "path": path.name,
                "status": "loaded",
                "compressed_bytes": path.stat().st_size,
                "rows": rows,
                "retained_window_assignments": retained,
                "out_of_order_timestamps": out_of_order,
            }
        )

    candles: dict[str, list[Candle]] = {}
    coverage: dict[str, dict[str, Any]] = {}
    for window in windows:
        items = mutable[window.key]
        candles[window.key] = [
            Candle(
                start_ms=timestamp,
                open=float(values[0]),
                high=float(values[1]),
                low=float(values[2]),
                close=float(values[3]),
            )
            for timestamp, values in sorted(items.items())
        ]
        coverage[window.key] = {
            "retained_trades": sum(int(values[4]) for values in items.values()),
            "one_second_candles": len(items),
            "entry_second_present": window.start_ms in items,
            "order_seconds_present": [
                timestamp in items for timestamp in window.order_timestamps_ms
            ],
            "close_second_present": window.close_timestamp_ms in items,
            "duplicate_trade_ids": duplicate_ids[window.key],
        }

    return candles, {
        "archive_directory": str(archive_dir),
        "files": files,
        "rows_scanned": total_rows,
        "retained_window_assignments": retained_rows,
        "window_coverage": coverage,
    }


def _entry_candidates(cycle: TraderCycle, first: Candle) -> list[tuple[str, float]]:
    candidates = [
        ("second_open", first.open),
        ("second_low", first.low),
        ("second_high", first.high),
    ]
    if cycle.dca_count == 0 and first.low <= cycle.average_entry <= first.high:
        candidates.append(("exact_exported_dca0_entry", cycle.average_entry))
    unique: list[tuple[str, float]] = []
    seen: set[float] = set()
    for name, value in candidates:
        if value not in seen:
            unique.append((name, value))
            seen.add(value)
    return unique


def _with_first_open(candles: list[Candle], price: float) -> list[Candle]:
    first = candles[0]
    return [
        Candle(
            start_ms=first.start_ms,
            open=price,
            high=max(first.high, price),
            low=min(first.low, price),
            close=first.close,
        ),
        *candles[1:],
    ]


def _utc_dates(start_ms: int, end_ms: int) -> list[str]:
    current = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    final = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
    dates: list[str] = []
    while current <= final:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _cycle_key(cycle: TraderCycle) -> str:
    return f"{cycle.opened_ms}-{cycle.closed_ms}"


def _tp_percent(cycle: TraderCycle) -> float:
    return (cycle.closing_price / cycle.average_entry - 1) * 100


def _iso_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()
