from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from itertools import pairwise
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from botdca.backtest import BacktestResult, CycleReplay


@dataclass(frozen=True, slots=True)
class TraderOrder:
    source_row: int
    symbol: str
    side: str
    opened_ms: int
    closed_ms: int
    order_qty: float
    average_entry: float
    closing_price: float
    leverage: float
    roi_percent: float
    followers: int


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
    symbol: str = "HYPEUSDT"
    side: str = "Long"
    close_fragment_count: int = 1
    orders: tuple[TraderOrder, ...] = ()


@dataclass(frozen=True, slots=True)
class CycleGroupingDiagnostics:
    source_rows: int
    exact_key_groups: int
    grouped_cycles: int
    close_fragment_groups_merged: int
    overlapping_cycles: int


@dataclass(frozen=True, slots=True)
class TraderExportData:
    timezone_name: str
    orders: tuple[TraderOrder, ...]
    cycles: tuple[TraderCycle, ...]
    grouping: CycleGroupingDiagnostics


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


def parse_trader_export(
    path: str | Path,
    *,
    timezone_name: str = "UTC",
    close_time_tolerance_seconds: float = 5.0,
    close_price_tolerance_usdt: float = 0.002,
    expected_symbol: str | None = None,
    expected_side: str | None = None,
) -> TraderExportData:
    """Parse and defensibly group a Bybit trader-initiated export.

    Bybit can emit multiple close fragments for one economic position. Exact
    grouping splits those fragments when their close timestamps differ by a few
    seconds or their rounded close prices differ by one tick. Rows are merged
    only when symbol, side, leverage, and final average entry agree and both
    close tolerances are satisfied.
    """

    if close_time_tolerance_seconds < 0:
        raise ValueError("close_time_tolerance_seconds cannot be negative")
    if close_price_tolerance_usdt < 0:
        raise ValueError("close_price_tolerance_usdt cannot be negative")

    tz = _named_timezone(timezone_name)
    orders = _load_orders(
        path,
        tz=tz,
        expected_symbol=expected_symbol,
        expected_side=expected_side,
    )
    exact_key_groups = len({_exact_cycle_key(order) for order in orders})
    grouped_orders = _group_close_fragments(
        orders,
        close_time_tolerance_seconds=close_time_tolerance_seconds,
        close_price_tolerance_usdt=close_price_tolerance_usdt,
    )
    cycles = tuple(sorted((_build_cycle(group) for group in grouped_orders), key=_cycle_sort_key))
    overlaps = sum(
        current.opened_ms < previous.closed_ms
        for previous, current in pairwise(cycles)
    )
    if overlaps:
        raise ValueError(
            "grouped trader cycles overlap; review timezone and close-fragment tolerances"
        )

    return TraderExportData(
        timezone_name=timezone_name,
        orders=tuple(orders),
        cycles=cycles,
        grouping=CycleGroupingDiagnostics(
            source_rows=len(orders),
            exact_key_groups=exact_key_groups,
            grouped_cycles=len(cycles),
            close_fragment_groups_merged=exact_key_groups - len(cycles),
            overlapping_cycles=overlaps,
        ),
    )


def load_trader_cycles(
    path: str | Path,
    *,
    timezone_offset_minutes: int = 0,
    timezone_name: str | None = None,
    close_time_tolerance_seconds: float = 5.0,
    close_price_tolerance_usdt: float = 0.002,
) -> list[TraderCycle]:
    """Compatibility wrapper returning grouped completed cycles.

    Existing callers may continue using a fixed offset. New historical
    validation should pass an IANA timezone name so daylight-saving rules are
    explicit and reviewable.
    """

    if timezone_name is not None:
        if timezone_offset_minutes != 0:
            raise ValueError("use either timezone_name or timezone_offset_minutes, not both")
        return list(
            parse_trader_export(
                path,
                timezone_name=timezone_name,
                close_time_tolerance_seconds=close_time_tolerance_seconds,
                close_price_tolerance_usdt=close_price_tolerance_usdt,
            ).cycles
        )

    tz = timezone(timedelta(minutes=timezone_offset_minutes))
    orders = _load_orders(path, tz=tz)
    grouped = _group_close_fragments(
        orders,
        close_time_tolerance_seconds=close_time_tolerance_seconds,
        close_price_tolerance_usdt=close_price_tolerance_usdt,
    )
    cycles = sorted((_build_cycle(group) for group in grouped), key=_cycle_sort_key)
    if any(
        current.opened_ms < previous.closed_ms
        for previous, current in pairwise(cycles)
    ):
        raise ValueError(
            "grouped trader cycles overlap; review timezone and close-fragment tolerances"
        )
    return cycles


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


def _load_orders(
    path: str | Path,
    *,
    tz: tzinfo,
    expected_symbol: str | None = None,
    expected_side: str | None = None,
) -> list[TraderOrder]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "position_symbol",
            "position_side",
            "margin_and_leverage",
            "order_qty",
            "roi_percent",
            "entry_price",
            "opened_on",
            "closing_price",
            "closed_on",
            "followers",
        }
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"trader export is missing columns: {sorted(missing)}")

        orders: list[TraderOrder] = []
        for source_row, row in enumerate(reader, start=2):
            empty = sorted(column for column in required if not (row.get(column) or "").strip())
            if empty:
                raise ValueError(f"trader export row {source_row} has empty columns: {empty}")
            symbol = row["position_symbol"].strip().upper()
            side = row["position_side"].strip().title()
            if expected_symbol is not None and symbol != expected_symbol.upper():
                raise ValueError(
                    f"trader export row {source_row} has symbol={symbol}, "
                    f"expected {expected_symbol.upper()}"
                )
            if expected_side is not None and side != expected_side.title():
                raise ValueError(
                    f"trader export row {source_row} has side={side}, "
                    f"expected {expected_side.title()}"
                )
            opened = _parse_timestamp(row["opened_on"], tz)
            closed = _parse_timestamp(row["closed_on"], tz)
            if opened > closed:
                raise ValueError(f"trader export row {source_row} closes before it opens")
            order = TraderOrder(
                source_row=source_row,
                symbol=symbol,
                side=side,
                opened_ms=_to_ms(opened),
                closed_ms=_to_ms(closed),
                order_qty=_parse_number(row["order_qty"]),
                average_entry=_parse_number(row["entry_price"]),
                closing_price=_parse_number(row["closing_price"]),
                leverage=_parse_leverage(row["margin_and_leverage"]),
                roi_percent=_parse_percent(row["roi_percent"]),
                followers=int(_parse_number(row["followers"])),
            )
            if min(
                order.order_qty,
                order.average_entry,
                order.closing_price,
                order.leverage,
            ) <= 0:
                raise ValueError(f"trader export row {source_row} contains non-positive values")
            orders.append(order)

    if not orders:
        raise ValueError("trader export contains no rows")
    return orders


def _group_close_fragments(
    orders: list[TraderOrder],
    *,
    close_time_tolerance_seconds: float,
    close_price_tolerance_usdt: float,
) -> list[list[TraderOrder]]:
    buckets: dict[tuple[str, str, float, float], list[TraderOrder]] = defaultdict(list)
    for order in orders:
        buckets[(order.symbol, order.side, order.leverage, order.average_entry)].append(order)

    groups: list[list[TraderOrder]] = []
    for bucket in buckets.values():
        groups.extend(
            _close_components(
                bucket,
                time_tolerance_ms=close_time_tolerance_seconds * 1000,
                price_tolerance_usdt=close_price_tolerance_usdt,
            )
        )
    return groups


def _close_components(
    bucket: list[TraderOrder],
    *,
    time_tolerance_ms: float,
    price_tolerance_usdt: float,
) -> list[list[TraderOrder]]:
    bucket.sort(key=lambda order: (order.closed_ms, order.closing_price, order.source_row))
    components: list[list[TraderOrder]] = []
    for order in bucket:
        compatible = [
            component
            for component in components
            if all(
                abs(order.closed_ms - member.closed_ms) <= time_tolerance_ms
                and abs(order.closing_price - member.closing_price) <= price_tolerance_usdt
                for member in component
            )
        ]
        if len(compatible) > 1:
            raise ValueError("ambiguous close-fragment grouping within configured tolerances")
        if compatible:
            compatible[0].append(order)
        else:
            components.append([order])
    return components


def _build_cycle(orders: list[TraderOrder]) -> TraderCycle:
    ordered = tuple(sorted(orders, key=lambda order: (order.opened_ms, order.source_row)))
    total_qty = sum(order.order_qty for order in ordered)
    closing_price = sum(order.closing_price * order.order_qty for order in ordered) / total_qty
    close_fragments = {(order.closed_ms, order.closing_price) for order in ordered}
    return TraderCycle(
        opened_ms=min(order.opened_ms for order in ordered),
        closed_ms=max(order.closed_ms for order in ordered),
        average_entry=ordered[0].average_entry,
        closing_price=closing_price,
        dca_count=max(0, len(ordered) - 1),
        total_qty=total_qty,
        leverage=ordered[0].leverage,
        roi_percent_median=median(order.roi_percent for order in ordered),
        symbol=ordered[0].symbol,
        side=ordered[0].side,
        close_fragment_count=len(close_fragments),
        orders=ordered,
    )


def _exact_cycle_key(order: TraderOrder) -> tuple[object, ...]:
    return (
        order.symbol,
        order.side,
        order.average_entry,
        order.closing_price,
        order.closed_ms,
        order.leverage,
    )


def _cycle_sort_key(cycle: TraderCycle) -> tuple[int, int]:
    return (cycle.closed_ms, cycle.opened_ms)


def _named_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {name}") from exc


def _parse_timestamp(value: str, tz: tzinfo) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
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
