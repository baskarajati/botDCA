"""Training-only precision hypotheses; never imported by the live runtime."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from statistics import median

from botdca.domain import TradingCycle
from botdca.failure_trace import FailureTraceEngine
from botdca.strategy import DcaStrategy


def rounded_price(average: float, percent: float, tick: str | None, mode: str) -> float:
    target = Decimal(str(average)) * (1 + Decimal(str(percent)) / 100)
    if tick is not None:
        increment = Decimal(tick)
        target = (target / increment).to_integral_value(rounding=mode) * increment
    return float(target)


def fit_tp_rule(training):
    """Finite predeclared grid; ties prefer simpler unrounded construction."""
    if not training:
        raise ValueError("training cycles are required")
    realized = median((c.closing_price / c.average_entry - 1) * 100 for c in training)
    nominal = float(Decimal(str(realized)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    candidates = []
    for percent in (realized, nominal):
        for tick in (None, "0.001", "0.01"):
            for mode in (
                (ROUND_FLOOR,) if tick is None else (ROUND_FLOOR, ROUND_HALF_UP, ROUND_CEILING)
            ):
                errors = [
                    abs(rounded_price(c.average_entry, percent, tick, mode) - c.closing_price)
                    for c in training
                ]
                candidates.append(
                    {
                        "percent": percent,
                        "tick": tick,
                        "mode": mode,
                        "mean_absolute_price_error": sum(errors) / len(errors),
                        "within_0_002": sum(e <= 0.00200001 for e in errors),
                        "training_cycles": len(training),
                    }
                )
    return min(candidates, key=lambda c: c["mean_absolute_price_error"]), candidates


@dataclass
class RoundedTpCycle(TradingCycle):
    price_tick: str | None = None
    rounding_mode: str = ROUND_FLOOR

    @property
    def tp_price(self):
        average = self.average_entry
        return (
            None
            if average is None
            else rounded_price(
                average,
                self.tp_percent,
                self.price_tick,
                self.rounding_mode,
            )
        )


class RoundedTpStrategy(DcaStrategy):
    def __init__(self, config, rule):
        super().__init__(config)
        self.rule = rule

    def begin_cycle(self, fill_price):
        original = super().begin_cycle(fill_price)
        self.current_cycle = RoundedTpCycle(
            **vars(original),
            price_tick=self.rule["tick"],
            rounding_mode=self.rule["mode"],
        )
        return self.current_cycle


class RoundedTpTraceEngine(FailureTraceEngine):
    def __init__(self, config, *, rule, **kwargs):
        super().__init__(config, **kwargs)
        self.strategy = RoundedTpStrategy(config, rule)
        self.strategy.resume()
