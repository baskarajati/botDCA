from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Protocol

from botdca.strategy import DcaStrategy, DcaTriggerReference, StrategyConfig


class IntrabarPath(StrEnum):
    LOW_FIRST = "low-first"
    HIGH_FIRST = "high-first"


class FundingModel(Protocol):
    """Optional timestamped funding source; positive payments are costs."""

    def payment_usdt(
        self,
        *,
        timestamp_ms: int,
        mark_price: float,
        position_quantity: float,
    ) -> float: ...


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be <= open/close/high")

    def points(self, path: IntrabarPath) -> tuple[float, float, float, float]:
        if path == IntrabarPath.LOW_FIRST:
            return (self.open, self.low, self.high, self.close)
        return (self.open, self.high, self.low, self.close)


@dataclass(frozen=True)
class CycleReplay:
    cycle_id: str
    opened_ms: int
    closed_ms: int
    exit_price: float
    gross_pnl_usdt: float
    fees_usdt: float
    net_pnl_usdt: float
    dca_level: int
    margin_deployed_usdt: float
    average_entry: float | None = None
    fill_prices: tuple[float, ...] = ()
    fill_quantities: tuple[float, ...] = ()
    entry_fees_usdt: float = 0.0
    exit_fee_usdt: float = 0.0
    duration_seconds: float = 0.0
    peak_position_notional_usdt: float = 0.0
    maximum_adverse_excursion_usdt: float = 0.0
    maximum_favorable_excursion_usdt: float = 0.0
    maximum_adverse_excursion_percent: float = 0.0
    maximum_favorable_excursion_percent: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    intrabar_path: str
    candles: int
    completed_cycles: int
    gross_realized_pnl_usdt: float
    fees_paid_usdt: float
    net_realized_pnl_usdt: float
    max_dca_level: int
    max_margin_deployed_usdt: float
    max_mark_to_market_drawdown_usdt: float
    final_unrealized_pnl_usdt: float
    open_cycle_dca_level: int | None
    cycles: list[CycleReplay] = field(default_factory=list)
    peak_position_notional_usdt: float = 0.0
    worst_floating_pnl_usdt: float = 0.0
    longest_open_cycle_seconds: float = 0.0
    longest_underwater_seconds: float = 0.0
    recovery_durations_seconds: tuple[float, ...] = ()
    ongoing_underwater_seconds: float = 0.0
    funding_paid_usdt: float | None = None
    net_pnl_includes_funding: bool = False
    entry_fees_paid_usdt: float = 0.0
    exit_fees_paid_usdt: float = 0.0
    estimated_open_cycle_exit_fee_usdt: float = 0.0
    realized_cycle_fees_usdt: float = 0.0
    open_cycle_entry_fees_paid_usdt: float = 0.0
    net_realized_after_funding_usdt: float | None = None


class ReplayEngine:
    """Replay the same DcaStrategy used by the runtime on historical candles.

    Each candle is traversed as monotonic segments. When a TP or DCA threshold
    lies inside a segment, the engine fills at that threshold, recalculates the
    strategy, then continues through the remainder of the segment.
    """

    def __init__(
        self,
        config: StrategyConfig,
        *,
        intrabar_path: IntrabarPath = IntrabarPath.LOW_FIRST,
        taker_fee_rate: float = 0.00055,
        reentry_delay_seconds: float = 0.0,
        auto_reentry: bool = True,
        maximum_completed_cycles: int | None = None,
        funding_model: FundingModel | None = None,
        trigger_reference: DcaTriggerReference = DcaTriggerReference.WEIGHTED_AVERAGE,
    ) -> None:
        if taker_fee_rate < 0:
            raise ValueError("taker_fee_rate cannot be negative")
        if reentry_delay_seconds < 0:
            raise ValueError("reentry_delay_seconds cannot be negative")
        if maximum_completed_cycles is not None and maximum_completed_cycles <= 0:
            raise ValueError("maximum_completed_cycles must be positive")
        self.config = config
        self.path = intrabar_path
        self.taker_fee_rate = taker_fee_rate
        self.reentry_delay_ms = int(reentry_delay_seconds * 1000)
        self.auto_reentry = auto_reentry
        self.maximum_completed_cycles = maximum_completed_cycles
        self.funding_model = funding_model
        self.trigger_reference = trigger_reference
        self.strategy = DcaStrategy(config, trigger_reference=trigger_reference)
        self.strategy.resume()

        self._cycles: list[CycleReplay] = []
        self._current_opened_ms: int | None = None
        self._current_entry_fees = 0.0
        self._current_margin = 0.0
        self._gross_realized = 0.0
        self._fees_paid = 0.0
        self._entry_fees_paid = 0.0
        self._exit_fees_paid = 0.0
        self._funding_paid = 0.0
        self._net_realized = 0.0
        self._max_dca = 0
        self._max_margin = 0.0
        self._equity_peak = 0.0
        self._max_drawdown = 0.0
        self._last_price: float | None = None
        self._last_timestamp_ms: int | None = None
        self._next_entry_eligible_ms = 0
        self._stopped = False
        self._candles_processed = 0
        self._current_peak_notional = 0.0
        self._current_mae_usdt = 0.0
        self._current_mfe_usdt = 0.0
        self._current_mae_percent = 0.0
        self._current_mfe_percent = 0.0
        self._peak_position_notional = 0.0
        self._worst_floating_pnl = 0.0
        self._longest_open_cycle_seconds = 0.0
        self._underwater_started_ms: int | None = None
        self._recovery_durations_seconds: list[float] = []

    def run(self, candles: list[Candle]) -> BacktestResult:
        if not candles:
            raise ValueError("at least one candle is required")
        ordered = sorted(candles, key=lambda item: item.start_ms)

        for candle in ordered:
            if self._stopped:
                break
            self._process_candle(candle)
            self._candles_processed += 1

        final_price = self._last_price if self._last_price is not None else ordered[0].open
        final_timestamp_ms = (
            self._last_timestamp_ms
            if self._last_timestamp_ms is not None
            else ordered[0].start_ms
        )
        final_unrealized = self._current_unrealized_net(final_price)
        open_cycle_exit_fee = self._estimated_exit_fee(final_price)
        cycle = self.strategy.current_cycle
        if cycle is not None and self._current_opened_ms is not None:
            self._longest_open_cycle_seconds = max(
                self._longest_open_cycle_seconds,
                (final_timestamp_ms - self._current_opened_ms) / 1000,
            )
        ongoing_underwater = (
            (final_timestamp_ms - self._underwater_started_ms) / 1000
            if self._underwater_started_ms is not None
            else 0.0
        )
        longest_underwater = max(
            [ongoing_underwater, *self._recovery_durations_seconds],
            default=0.0,
        )

        return BacktestResult(
            symbol=self.config.symbol,
            intrabar_path=self.path.value,
            candles=self._candles_processed,
            completed_cycles=len(self._cycles),
            gross_realized_pnl_usdt=self._gross_realized,
            fees_paid_usdt=self._fees_paid + self._current_entry_fees,
            net_realized_pnl_usdt=self._net_realized,
            max_dca_level=self._max_dca,
            max_margin_deployed_usdt=self._max_margin,
            max_mark_to_market_drawdown_usdt=self._max_drawdown,
            final_unrealized_pnl_usdt=final_unrealized,
            open_cycle_dca_level=cycle.dca_level if cycle is not None else None,
            cycles=list(self._cycles),
            peak_position_notional_usdt=self._peak_position_notional,
            worst_floating_pnl_usdt=self._worst_floating_pnl,
            longest_open_cycle_seconds=self._longest_open_cycle_seconds,
            longest_underwater_seconds=longest_underwater,
            recovery_durations_seconds=tuple(self._recovery_durations_seconds),
            ongoing_underwater_seconds=ongoing_underwater,
            funding_paid_usdt=(
                None if self.funding_model is None else self._funding_paid
            ),
            entry_fees_paid_usdt=self._entry_fees_paid,
            exit_fees_paid_usdt=self._exit_fees_paid,
            estimated_open_cycle_exit_fee_usdt=open_cycle_exit_fee,
            realized_cycle_fees_usdt=self._fees_paid,
            open_cycle_entry_fees_paid_usdt=self._current_entry_fees,
            net_realized_after_funding_usdt=(
                None
                if self.funding_model is None
                else self._net_realized - self._funding_paid
            ),
        )

    def _process_candle(self, candle: Candle) -> None:
        points = candle.points(self.path)
        self._apply_funding(candle.start_ms, points[0])
        if self.strategy.current_cycle is None:
            if not self.auto_reentry and self._cycles:
                return
            if candle.start_ms < self._next_entry_eligible_ms:
                return
            self._open_cycle(points[0], candle.start_ms)
        self._mark_equity(points[0], candle.start_ms)

        for start, end in pairwise(points):
            self._traverse_segment(start, end, candle.start_ms)

    def _traverse_segment(self, start: float, end: float, timestamp_ms: int) -> None:
        current = start
        if end > start:
            while True:
                cycle = self.strategy.current_cycle
                if cycle is None:
                    break
                tp = cycle.tp_price if cycle is not None else None
                if tp is None or tp > end or tp < current:
                    break
                self._mark_equity(tp, timestamp_ms)
                self._close_cycle(tp, timestamp_ms)
                current = tp
                if (
                    self.maximum_completed_cycles is not None
                    and len(self._cycles) >= self.maximum_completed_cycles
                ):
                    self._stopped = True
                    break
                if not self.auto_reentry:
                    self._stopped = True
                    break
                self._next_entry_eligible_ms = timestamp_ms + self.reentry_delay_ms
                if self.reentry_delay_ms > 0:
                    break
                self._open_cycle(tp, timestamp_ms)
        elif end < start:
            while True:
                trigger = self.strategy.next_dca_trigger_price()
                if trigger is None or trigger < end or trigger > current:
                    break
                self._mark_equity(trigger, timestamp_ms)
                self._add_dca(trigger)
                self._mark_equity(trigger, timestamp_ms)
                current = trigger

        self._mark_equity(end, timestamp_ms)
        self._last_price = end
        self._last_timestamp_ms = timestamp_ms

    def _open_cycle(self, price: float, timestamp_ms: int) -> None:
        cycle = self.strategy.begin_cycle(price)
        fill = cycle.fills[0]
        notional = fill.price * fill.qty
        self._current_opened_ms = timestamp_ms
        self._current_entry_fees = notional * self.taker_fee_rate
        self._entry_fees_paid += notional * self.taker_fee_rate
        self._current_margin = notional / self.config.leverage
        self._max_margin = max(self._max_margin, self._current_margin)
        self._current_peak_notional = 0.0
        self._current_mae_usdt = 0.0
        self._current_mfe_usdt = 0.0
        self._current_mae_percent = 0.0
        self._current_mfe_percent = 0.0

    def _add_dca(self, price: float) -> None:
        qty = self.strategy.next_dca_qty()
        if qty is None:
            return
        fill = self.strategy.apply_dca_fill(price, qty)
        notional = fill.price * fill.qty
        self._current_entry_fees += notional * self.taker_fee_rate
        self._entry_fees_paid += notional * self.taker_fee_rate
        self._current_margin += notional / self.config.leverage
        cycle = self.strategy.current_cycle
        if cycle is not None:
            self._max_dca = max(self._max_dca, cycle.dca_level)
        self._max_margin = max(self._max_margin, self._current_margin)

    def _close_cycle(self, price: float, timestamp_ms: int) -> None:
        cycle = self.strategy.current_cycle
        if cycle is None:
            return
        gross = sum((price - fill.price) * fill.qty for fill in cycle.fills)
        exit_fee = price * cycle.total_qty * self.taker_fee_rate
        fees = self._current_entry_fees + exit_fee
        net = gross - fees
        opened_ms = self._current_opened_ms if self._current_opened_ms is not None else timestamp_ms
        duration_seconds = (timestamp_ms - opened_ms) / 1000
        replay = CycleReplay(
            cycle_id=cycle.id,
            opened_ms=opened_ms,
            closed_ms=timestamp_ms,
            exit_price=price,
            gross_pnl_usdt=gross,
            fees_usdt=fees,
            net_pnl_usdt=net,
            dca_level=cycle.dca_level,
            margin_deployed_usdt=self._current_margin,
            average_entry=cycle.average_entry,
            fill_prices=tuple(fill.price for fill in cycle.fills),
            fill_quantities=tuple(fill.qty for fill in cycle.fills),
            entry_fees_usdt=self._current_entry_fees,
            exit_fee_usdt=exit_fee,
            duration_seconds=duration_seconds,
            peak_position_notional_usdt=self._current_peak_notional,
            maximum_adverse_excursion_usdt=self._current_mae_usdt,
            maximum_favorable_excursion_usdt=self._current_mfe_usdt,
            maximum_adverse_excursion_percent=self._current_mae_percent,
            maximum_favorable_excursion_percent=self._current_mfe_percent,
        )
        self._cycles.append(replay)
        self._gross_realized += gross
        self._fees_paid += fees
        self._exit_fees_paid += exit_fee
        self._net_realized += net
        self._max_dca = max(self._max_dca, cycle.dca_level)
        self._longest_open_cycle_seconds = max(
            self._longest_open_cycle_seconds,
            duration_seconds,
        )
        self.strategy.mark_closed(net)
        self._current_opened_ms = None
        self._current_entry_fees = 0.0
        self._current_margin = 0.0
        self._equity_peak = max(
            self._equity_peak,
            self._net_realized - self._funding_paid,
        )

    def _current_unrealized_net(self, price: float) -> float:
        cycle = self.strategy.current_cycle
        if cycle is None:
            return 0.0
        gross = sum((price - fill.price) * fill.qty for fill in cycle.fills)
        estimated_exit_fee = self._estimated_exit_fee(price)
        return gross - self._current_entry_fees - estimated_exit_fee

    def _estimated_exit_fee(self, price: float) -> float:
        cycle = self.strategy.current_cycle
        return 0.0 if cycle is None else price * cycle.total_qty * self.taker_fee_rate

    def _apply_funding(self, timestamp_ms: int, mark_price: float) -> None:
        cycle = self.strategy.current_cycle
        if self.funding_model is None or cycle is None:
            return
        self._funding_paid += self.funding_model.payment_usdt(
            timestamp_ms=timestamp_ms,
            mark_price=mark_price,
            position_quantity=cycle.total_qty,
        )

    def _mark_equity(self, price: float, timestamp_ms: int) -> None:
        unrealized = self._current_unrealized_net(price)
        equity = self._net_realized - self._funding_paid + unrealized
        previous_peak = self._equity_peak
        if equity >= previous_peak:
            if self._underwater_started_ms is not None:
                self._recovery_durations_seconds.append(
                    (timestamp_ms - self._underwater_started_ms) / 1000
                )
                self._underwater_started_ms = None
            self._equity_peak = equity
        elif self._underwater_started_ms is None:
            self._underwater_started_ms = timestamp_ms
        self._max_drawdown = max(self._max_drawdown, self._equity_peak - equity)
        self._worst_floating_pnl = min(self._worst_floating_pnl, unrealized)
        self._mark_position_risk(price)

    def _mark_position_risk(self, price: float) -> None:
        cycle = self.strategy.current_cycle
        if cycle is None or cycle.average_entry is None:
            return
        gross_unrealized = sum((price - fill.price) * fill.qty for fill in cycle.fills)
        excursion_percent = (price / cycle.average_entry - 1) * 100
        position_notional = price * cycle.total_qty
        self._current_peak_notional = max(self._current_peak_notional, position_notional)
        self._peak_position_notional = max(self._peak_position_notional, position_notional)
        self._current_mae_usdt = max(self._current_mae_usdt, -gross_unrealized)
        self._current_mfe_usdt = max(self._current_mfe_usdt, gross_unrealized)
        self._current_mae_percent = max(self._current_mae_percent, -excursion_percent)
        self._current_mfe_percent = max(self._current_mfe_percent, excursion_percent)
