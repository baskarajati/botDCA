from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from botdca.strategy import DcaStrategy, StrategyConfig


class IntrabarPath(StrEnum):
    LOW_FIRST = "low-first"
    HIGH_FIRST = "high-first"


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
    ) -> None:
        if taker_fee_rate < 0:
            raise ValueError("taker_fee_rate cannot be negative")
        self.config = config
        self.path = intrabar_path
        self.taker_fee_rate = taker_fee_rate
        self.strategy = DcaStrategy(config)
        self.strategy.resume()

        self._cycles: list[CycleReplay] = []
        self._current_opened_ms: int | None = None
        self._current_entry_fees = 0.0
        self._current_margin = 0.0
        self._gross_realized = 0.0
        self._fees_paid = 0.0
        self._net_realized = 0.0
        self._max_dca = 0
        self._max_margin = 0.0
        self._equity_peak = 0.0
        self._max_drawdown = 0.0
        self._last_price: float | None = None

    def run(self, candles: list[Candle]) -> BacktestResult:
        if not candles:
            raise ValueError("at least one candle is required")
        ordered = sorted(candles, key=lambda item: item.start_ms)

        for candle in ordered:
            self._process_candle(candle)

        last = ordered[-1]
        self._last_price = last.close
        final_unrealized = self._current_unrealized_net(last.close)
        cycle = self.strategy.current_cycle

        return BacktestResult(
            symbol=self.config.symbol,
            intrabar_path=self.path.value,
            candles=len(ordered),
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
        )

    def _process_candle(self, candle: Candle) -> None:
        points = candle.points(self.path)
        if self.strategy.current_cycle is None:
            self._open_cycle(points[0], candle.start_ms)
        self._mark_equity(points[0])

        for start, end in zip(points, points[1:]):
            self._traverse_segment(start, end, candle.start_ms)

    def _traverse_segment(self, start: float, end: float, timestamp_ms: int) -> None:
        current = start
        if end > start:
            while True:
                cycle = self.strategy.current_cycle
                if cycle is None:
                    self._open_cycle(current, timestamp_ms)
                    cycle = self.strategy.current_cycle
                tp = cycle.tp_price if cycle is not None else None
                if tp is None or tp > end or tp < current:
                    break
                self._mark_equity(tp)
                self._close_cycle(tp, timestamp_ms)
                self._open_cycle(tp, timestamp_ms)
                current = tp
        elif end < start:
            while True:
                trigger = self.strategy.next_dca_trigger_price()
                if trigger is None or trigger < end or trigger > current:
                    break
                self._mark_equity(trigger)
                self._add_dca(trigger)
                current = trigger

        self._mark_equity(end)
        self._last_price = end

    def _open_cycle(self, price: float, timestamp_ms: int) -> None:
        cycle = self.strategy.begin_cycle(price)
        fill = cycle.fills[0]
        notional = fill.price * fill.qty
        self._current_opened_ms = timestamp_ms
        self._current_entry_fees = notional * self.taker_fee_rate
        self._current_margin = notional / self.config.leverage
        self._max_margin = max(self._max_margin, self._current_margin)

    def _add_dca(self, price: float) -> None:
        qty = self.strategy.next_dca_qty()
        if qty is None:
            return
        fill = self.strategy.apply_dca_fill(price, qty)
        notional = fill.price * fill.qty
        self._current_entry_fees += notional * self.taker_fee_rate
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
        )
        self._cycles.append(replay)
        self._gross_realized += gross
        self._fees_paid += fees
        self._net_realized += net
        self._max_dca = max(self._max_dca, cycle.dca_level)
        self.strategy.mark_closed(net)
        self._current_opened_ms = None
        self._current_entry_fees = 0.0
        self._current_margin = 0.0
        self._equity_peak = max(self._equity_peak, self._net_realized)

    def _current_unrealized_net(self, price: float) -> float:
        cycle = self.strategy.current_cycle
        if cycle is None:
            return 0.0
        gross = sum((price - fill.price) * fill.qty for fill in cycle.fills)
        estimated_exit_fee = price * cycle.total_qty * self.taker_fee_rate
        return gross - self._current_entry_fees - estimated_exit_fee

    def _mark_equity(self, price: float) -> None:
        equity = self._net_realized + self._current_unrealized_net(price)
        self._equity_peak = max(self._equity_peak, equity)
        self._max_drawdown = max(self._max_drawdown, self._equity_peak - equity)
