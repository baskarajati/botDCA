"""Read-only instrumentation of frozen replay rules; not an execution model."""

from botdca.backtest import Candle, ReplayEngine


class FailureTraceEngine(ReplayEngine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events: list[dict] = []
        self.open_gap_crossings: list[dict] = []
        self._trace_timestamp = 0

    def _process_candle(self, candle: Candle) -> None:
        self._trace_timestamp = candle.start_ms
        cycle = self.strategy.current_cycle
        previous = self._last_price
        if cycle is not None and previous is not None:
            thresholds = (
                ("tp", cycle.tp_price),
                ("dca", self.strategy.next_dca_trigger_price()),
            )
            for kind, threshold in thresholds:
                if threshold is None:
                    continue
                crossed = (
                    previous < threshold <= candle.open
                    if kind == "tp"
                    else previous > threshold >= candle.open
                )
                if crossed:
                    self.open_gap_crossings.append(
                        {
                            "kind": kind,
                            "timestamp_ms": candle.start_ms,
                            "previous_close": previous,
                            "open": candle.open,
                            "threshold": threshold,
                            "entire_candle_beyond_threshold": (
                                candle.low > threshold if kind == "tp" else candle.high < threshold
                            ),
                        }
                    )
        super()._process_candle(candle)

    def _add_dca(self, price: float) -> None:
        super()._add_dca(price)
        cycle = self.strategy.current_cycle
        assert cycle is not None
        self.events.append(
            {
                "kind": "dca",
                "timestamp_ms": self._trace_timestamp,
                "price": price,
                "depth": cycle.dca_level,
                "average_entry": cycle.average_entry,
                "tp": cycle.tp_price,
            }
        )

    def _close_cycle(self, price: float, timestamp_ms: int) -> None:
        cycle = self.strategy.current_cycle
        assert cycle is not None
        self.events.append(
            {
                "kind": "tp",
                "timestamp_ms": timestamp_ms,
                "price": price,
                "depth": cycle.dca_level,
                "average_entry": cycle.average_entry,
            }
        )
        super()._close_cycle(price, timestamp_ms)
