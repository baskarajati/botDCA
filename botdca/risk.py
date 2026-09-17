from __future__ import annotations

from dataclasses import dataclass

from botdca.strategy import DcaStrategy


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_dca_level: int = 8
    max_strategy_margin_usdt: float = 80.0

    def __post_init__(self) -> None:
        if self.max_dca_level < 0:
            raise ValueError("max_dca_level cannot be negative")
        if self.max_strategy_margin_usdt <= 0:
            raise ValueError("max_strategy_margin_usdt must be positive")


@dataclass(frozen=True, slots=True)
class DcaRiskDecision:
    allowed: bool
    reason: str | None
    current_margin_usdt: float
    projected_margin_usdt: float
    current_dca_level: int
    next_dca_level: int | None


def committed_margin_usdt(strategy: DcaStrategy) -> float:
    cycle = strategy.current_cycle
    if cycle is None:
        return 0.0
    notional = sum(fill.price * fill.qty for fill in cycle.fills)
    return notional / cycle.leverage


def evaluate_next_dca(strategy: DcaStrategy, limits: RiskLimits) -> DcaRiskDecision:
    cycle = strategy.current_cycle
    if cycle is None:
        return DcaRiskDecision(
            allowed=False,
            reason="no active cycle",
            current_margin_usdt=0.0,
            projected_margin_usdt=0.0,
            current_dca_level=0,
            next_dca_level=None,
        )

    current_margin = committed_margin_usdt(strategy)
    next_price = strategy.next_dca_trigger_price()
    next_qty = strategy.next_dca_qty()
    if next_price is None or next_qty is None:
        return DcaRiskDecision(
            allowed=False,
            reason="DCA ladder exhausted",
            current_margin_usdt=current_margin,
            projected_margin_usdt=current_margin,
            current_dca_level=cycle.dca_level,
            next_dca_level=None,
        )

    next_level = cycle.dca_level + 1
    projected_margin = current_margin + (next_price * next_qty / cycle.leverage)

    if next_level > limits.max_dca_level:
        return DcaRiskDecision(
            allowed=False,
            reason=(
                f"next DCA level {next_level} exceeds configured maximum "
                f"{limits.max_dca_level}"
            ),
            current_margin_usdt=current_margin,
            projected_margin_usdt=projected_margin,
            current_dca_level=cycle.dca_level,
            next_dca_level=next_level,
        )

    if projected_margin > limits.max_strategy_margin_usdt:
        return DcaRiskDecision(
            allowed=False,
            reason=(
                f"projected strategy margin {projected_margin:.4f} USDT exceeds configured "
                f"maximum {limits.max_strategy_margin_usdt:.4f} USDT"
            ),
            current_margin_usdt=current_margin,
            projected_margin_usdt=projected_margin,
            current_dca_level=cycle.dca_level,
            next_dca_level=next_level,
        )

    return DcaRiskDecision(
        allowed=True,
        reason=None,
        current_margin_usdt=current_margin,
        projected_margin_usdt=projected_margin,
        current_dca_level=cycle.dca_level,
        next_dca_level=next_level,
    )
