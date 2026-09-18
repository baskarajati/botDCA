from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from botdca.exchange import AccountSnapshot
from botdca.instruments import InstrumentRules
from botdca.risk import RiskLimits, evaluate_next_dca
from botdca.sizing import SizingError, size_initial_order
from botdca.strategy import DcaStrategy


@dataclass(frozen=True)
class LimitOrderTarget:
    qty: Decimal
    price: Decimal


@dataclass(frozen=True)
class RestingOrderPlan:
    take_profit: LimitOrderTarget
    next_dca: LimitOrderTarget | None
    dca_blocked_reason: str | None = None
    #: True when the basket has reached its pinned live maximum. No further
    #: normal DCA is planned, but the take profit above remains active.
    max_dca_reached: bool = False


def _validate_notional(rules: InstrumentRules, target: LimitOrderTarget) -> None:
    notional = target.qty * target.price
    if notional < rules.min_notional_value:
        raise ValueError(
            f"order notional {notional} is below {rules.symbol} minimum "
            f"{rules.min_notional_value}"
        )


def initial_market_qty(
    strategy: DcaStrategy,
    rules: InstrumentRules,
    market_price: float,
) -> Decimal:
    """Size the initial entry from the symbol's configured allocation.

    Supports both FIXED_MARGIN_USDT (qty = margin * leverage / price) and
    FIXED_BASE_QUANTITY (qty = configured quantity), then applies the
    instrument's quantity step, minimum quantity and minimum notional.
    """
    if market_price <= 0:
        raise ValueError("market_price must be positive")
    try:
        sized = size_initial_order(
            strategy.config.initial_allocation,
            rules,
            leverage=strategy.config.leverage,
            price=market_price,
        )
    except SizingError as exc:
        raise ValueError(str(exc)) from exc
    return sized.qty


def initial_order_margin_usdt(
    strategy: DcaStrategy,
    rules: InstrumentRules,
    market_price: float,
) -> float:
    """Margin the initial entry would actually reserve, after rounding."""
    sized = size_initial_order(
        strategy.config.initial_allocation,
        rules,
        leverage=strategy.config.leverage,
        price=market_price,
    )
    return float(sized.margin_usdt)


def build_resting_order_plan(
    strategy: DcaStrategy,
    rules: InstrumentRules,
    *,
    risk_limits: RiskLimits | None = None,
    account_snapshot: AccountSnapshot | None = None,
) -> RestingOrderPlan:
    cycle = strategy.current_cycle
    if cycle is None or cycle.average_entry is None or cycle.tp_price is None:
        raise RuntimeError("no active position to plan")

    tp = LimitOrderTarget(
        qty=rules.floor_qty(cycle.total_qty),
        price=rules.ceil_price(cycle.tp_price),
    )
    _validate_notional(rules, tp)

    dca_price = strategy.next_dca_trigger_price()
    dca_qty = strategy.next_dca_qty()
    if dca_price is None or dca_qty is None:
        # The pinned ladder is exhausted. This is an explicit state, not an
        # absence of code: TP stays active and the basket holds.
        return RestingOrderPlan(
            take_profit=tp,
            next_dca=None,
            dca_blocked_reason=(
                f"{cycle.symbol} basket reached its live maximum of "
                f"DCA{cycle.max_dca_level if cycle.max_dca_level is not None else cycle.dca_level}"
            ),
            max_dca_reached=True,
        )

    if risk_limits is not None:
        decision = evaluate_next_dca(
            strategy,
            risk_limits,
            account=account_snapshot,
        )
        if not decision.allowed:
            return RestingOrderPlan(
                take_profit=tp,
                next_dca=None,
                dca_blocked_reason=decision.reason,
            )

    dca = LimitOrderTarget(
        qty=rules.floor_qty(dca_qty),
        price=rules.floor_price(dca_price),
    )
    _validate_notional(rules, dca)
    return RestingOrderPlan(take_profit=tp, next_dca=dca)
