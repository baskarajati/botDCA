from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from botdca.instruments import InstrumentRules
from botdca.strategy import DcaStrategy


@dataclass(frozen=True)
class LimitOrderTarget:
    qty: Decimal
    price: Decimal


@dataclass(frozen=True)
class RestingOrderPlan:
    take_profit: LimitOrderTarget
    next_dca: LimitOrderTarget | None


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
    if market_price <= 0:
        raise ValueError("market_price must be positive")
    raw_qty = (
        Decimal(str(strategy.config.base_margin_usdt))
        * Decimal(str(strategy.config.leverage))
        / Decimal(str(market_price))
    )
    qty = rules.floor_qty(raw_qty)
    if qty * Decimal(str(market_price)) < rules.min_notional_value:
        raise ValueError("initial order is below the instrument minimum notional")
    return qty


def build_resting_order_plan(
    strategy: DcaStrategy,
    rules: InstrumentRules,
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
        return RestingOrderPlan(take_profit=tp, next_dca=None)

    dca = LimitOrderTarget(
        qty=rules.floor_qty(dca_qty),
        price=rules.floor_price(dca_price),
    )
    _validate_notional(rules, dca)
    return RestingOrderPlan(take_profit=tp, next_dca=dca)
