from __future__ import annotations

from dataclasses import dataclass

from botdca.exchange import AccountSnapshot
from botdca.strategy import DcaStrategy


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_dca_level: int = 8
    max_strategy_margin_usdt: float = 80.0
    min_available_balance_usdt: float = 0.0
    min_available_equity_ratio: float = 0.20

    def __post_init__(self) -> None:
        if self.max_dca_level < 0:
            raise ValueError("max_dca_level cannot be negative")
        if self.max_strategy_margin_usdt <= 0:
            raise ValueError("max_strategy_margin_usdt must be positive")
        if self.min_available_balance_usdt < 0:
            raise ValueError("min_available_balance_usdt cannot be negative")
        if not 0 <= self.min_available_equity_ratio < 1:
            raise ValueError("min_available_equity_ratio must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class DcaRiskDecision:
    allowed: bool
    reason: str | None
    current_margin_usdt: float
    projected_margin_usdt: float
    current_dca_level: int
    next_dca_level: int | None
    account_available_balance_usd: float | None = None
    projected_available_balance_usd: float | None = None
    reserve_floor_usd: float | None = None


def committed_margin_usdt(strategy: DcaStrategy) -> float:
    cycle = strategy.current_cycle
    if cycle is None:
        return 0.0
    notional = sum(fill.price * fill.qty for fill in cycle.fills)
    return notional / cycle.leverage


def evaluate_next_dca(
    strategy: DcaStrategy,
    limits: RiskLimits,
    *,
    account: AccountSnapshot | None = None,
) -> DcaRiskDecision:
    cycle = strategy.current_cycle
    if cycle is None:
        return DcaRiskDecision(
            allowed=False,
            reason="no active cycle",
            current_margin_usdt=0.0,
            projected_margin_usdt=0.0,
            current_dca_level=0,
            next_dca_level=None,
            account_available_balance_usd=(
                account.total_available_balance_usd if account is not None else None
            ),
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
            account_available_balance_usd=(
                account.total_available_balance_usd if account is not None else None
            ),
        )

    next_level = cycle.dca_level + 1
    additional_margin = next_price * next_qty / cycle.leverage
    projected_margin = current_margin + additional_margin

    decision_kwargs = {
        "current_margin_usdt": current_margin,
        "projected_margin_usdt": projected_margin,
        "current_dca_level": cycle.dca_level,
        "next_dca_level": next_level,
    }

    if next_level > limits.max_dca_level:
        return DcaRiskDecision(
            allowed=False,
            reason=(
                f"next DCA level {next_level} exceeds configured maximum "
                f"{limits.max_dca_level}"
            ),
            **decision_kwargs,
        )

    if projected_margin > limits.max_strategy_margin_usdt:
        return DcaRiskDecision(
            allowed=False,
            reason=(
                f"projected strategy margin {projected_margin:.4f} USDT exceeds configured "
                f"maximum {limits.max_strategy_margin_usdt:.4f} USDT"
            ),
            **decision_kwargs,
        )

    if account is not None:
        reserve_floor = max(
            limits.min_available_balance_usdt,
            account.total_equity_usd * limits.min_available_equity_ratio,
        )
        projected_available = account.total_available_balance_usd - additional_margin
        account_kwargs = {
            "account_available_balance_usd": account.total_available_balance_usd,
            "projected_available_balance_usd": projected_available,
            "reserve_floor_usd": reserve_floor,
        }
        if projected_available < reserve_floor:
            return DcaRiskDecision(
                allowed=False,
                reason=(
                    f"projected available balance {projected_available:.4f} USD would fall "
                    f"below reserve floor {reserve_floor:.4f} USD"
                ),
                **decision_kwargs,
                **account_kwargs,
            )
        return DcaRiskDecision(
            allowed=True,
            reason=None,
            **decision_kwargs,
            **account_kwargs,
        )

    return DcaRiskDecision(
        allowed=True,
        reason=None,
        **decision_kwargs,
    )
