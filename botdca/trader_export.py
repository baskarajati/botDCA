from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from botdca.backtest import BacktestResult, CycleReplay


@dataclass(frozen=True, slots=True)
class TraderCycle:
    opened_ms: int
    closed_ms: int
    average_entry: float
    closing_price: float
    dca_count: int
    total_qty: float
    leverage: float
    roi_percent_median: float


@dataclass(frozen=True, slots=True)
class CycleMatch:
    actual: TraderCycle
    simulated: CycleReplay
    close_time_error_seconds: float
    exit_price_error_percent: float
    dca_count_error: int


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    actual_cycles: int
    simulated_cycles: int
    matched_cycles: int
    unmatched_actual_cycles: int
    unmatched_simulated_cycles: int
    match_rate_percent: float
    median_close_time_error_seconds: float | None
    median_exit_price_error_percent: float | None
    median_abs_dca_count_error: float | None
    exact_dca_match_percent: float | None


def load_trader_cycles(
    path: str | Path,
    *,
    timezone_offset_minutes: int = 0,
) -> list[TraderCycle]:
    """Parse a Bybit copy-trader CSV without persisting the source file.

    The export contains one row for each trader-initiated entry/add. Rows sharing
    final average entry, closing price, closing timestamp, and leverage are
    treated as one completed strategy cycle.
    """

    tz = timezone(timedelta(minutes=timezone_offset_minutes))
    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "margin_and_leverage",
            "order_qty",
            "roi_percent",
            "entry_price",
            "opened_on",
            "closing_price",
            "closed_on",
        }
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"trader export is missing columns: {sorted(missing)}")

        for row in reader:
            key = (
                row["entry_price"],
                row["closing_price"],
                row["closed_on"],
                row["margin_and_leverage"],
            )
            grouped.setdefault(key, []).append(row)

    cycles: list[TraderCycle] = []
    for rows in grouped.values():
        opened = min(_parse_timestamp(row["opened_on"], tz) for row in rows)
        closed = _parse_timestamp(rows[0]["closed_on"], tz)
        roi_values = [_parse_percent(row["roi_percent"]) for row in rows]
        cycles.append(
            TraderCycle(
                opened_ms=_to_ms(opened),
                closed_ms=_to_ms(closed),
                average_entry=_parse_number(rows[0]["entry_price"]),
                closing_price=_parse_number(rows[0]["closing_price"]),
                dca_count=max(0, len(rows) - 1),
                total_qty=sum(_parse_number(row["order_qty"]) for row in rows),
                leverage=_parse_leverage(rows[0]["margin_and_leverage"]),
                roi_percent_median=median(roi_values),
            )
        )

    return sorted(cycles, key=lambda cycle: (cycle.closed_ms, cycle.opened_ms))


def compare_replay_to_trader(
    replay: BacktestResult,
    actual_cycles: list[TraderCycle],
    *,
    maximum_close_time_error_seconds: float = 300.0,
) -> ReplayComparison:
    """Greedily align completed simulated cycles to actual cycles by close time."""

    available = list(replay.cycles)
    matches: list[CycleMatch] = []

    for actual in actual_cycles:
        if not available:
            break
        candidate_index = min(
            range(len(available)),
            key=lambda index: abs(available[index].closed_ms - actual.closed_ms),
        )
        candidate = available[candidate_index]
        time_error = abs(candidate.closed_ms - actual.closed_ms) / 1000
        if time_error > maximum_close_time_error_seconds:
            continue

        available.pop(candidate_index)
        price_error = abs(candidate.exit_price - actual.closing_price) / actual.closing_price * 100
        matches.append(
            CycleMatch(
                actual=actual,
                simulated=candidate,
                close_time_error_seconds=time_error,
                exit_price_error_percent=price_error,
                dca_count_error=candidate.dca_level - actual.dca_count,
            )
        )

    matched = len(matches)
    actual_count = len(actual_cycles)
    simulated_count = len(replay.cycles)
    close_errors = [match.close_time_error_seconds for match in matches]
    price_errors = [match.exit_price_error_percent for match in matches]
    dca_errors = [abs(match.dca_count_error) for match in matches]
    exact_dca = sum(match.dca_count_error == 0 for match in matches)

    return ReplayComparison(
        actual_cycles=actual_count,
        simulated_cycles=simulated_count,
        matched_cycles=matched,
        unmatched_actual_cycles=max(0, actual_count - matched),
        unmatched_simulated_cycles=max(0, simulated_count - matched),
        match_rate_percent=(matched / actual_count * 100) if actual_count else 0.0,
        median_close_time_error_seconds=median(close_errors) if close_errors else None,
        median_exit_price_error_percent=median(price_errors) if price_errors else None,
        median_abs_dca_count_error=median(dca_errors) if dca_errors else None,
        exact_dca_match_percent=(exact_dca / matched * 100) if matched else None,
    )


def _parse_timestamp(value: str, tz: timezone) -> datetime:
    parsed = datetime.fromisoformat(value.strip()).replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _parse_number(value: str) -> float:
    token = value.strip().split()[0].replace(",", "")
    return float(token)


def _parse_percent(value: str) -> float:
    return float(value.strip().replace("%", "").replace("+", ""))


def _parse_leverage(value: str) -> float:
    token = value.strip().lower().replace("x", "").split()[-1]
    return float(token)
