"""Initial allocation sizing.

Quantity, notional, margin, leverage and ROI% are kept strictly separate.

    FIXED_MARGIN_USDT    qty = margin * leverage / price
    FIXED_BASE_QUANTITY  qty = configured quantity, margin = qty * price / leverage

Higher leverage at the same quantity does not change the dollar profit or loss
from a price move. It only reduces reserved initial margin and moves the
liquidation geometry, so a higher ROI% is never a stronger strategy edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite

from botdca.instruments import InstrumentRules


class SizingMode(StrEnum):
    FIXED_MARGIN_USDT = "fixed_margin_usdt"
    FIXED_BASE_QUANTITY = "fixed_base_quantity"


@dataclass(frozen=True, slots=True)
class InitialAllocation:
    """One symbol's starting allocation under a shared strategy version."""

    mode: SizingMode
    value: float

    def __post_init__(self) -> None:
        if not isfinite(self.value) or self.value <= 0:
            raise ValueError("initial sizing value must be positive and finite")
        if self.value > 1_000_000:
            raise ValueError("initial sizing value is unreasonably large")

    def describe(self) -> dict:
        return {
            "mode": str(self.mode),
            "value": self.value,
            "unit": "USDT margin"
            if self.mode is SizingMode.FIXED_MARGIN_USDT
            else "base quantity",
        }


@dataclass(frozen=True, slots=True)
class SizedOrder:
    """A rounded, exchange-legal order with its four separate quantities."""

    qty: Decimal
    price: Decimal
    notional_usdt: Decimal
    margin_usdt: Decimal
    leverage: int


class SizingError(ValueError):
    """The requested allocation cannot be expressed as a legal exchange order."""


def raw_initial_qty(allocation: InitialAllocation, *, leverage: int, price: float) -> Decimal:
    """Unrounded quantity implied by the allocation, before instrument rules."""
    if leverage < 1:
        raise SizingError("leverage must be at least 1")
    if not isfinite(price) or price <= 0:
        raise SizingError("price must be positive and finite")
    if allocation.mode is SizingMode.FIXED_MARGIN_USDT:
        return Decimal(str(allocation.value)) * Decimal(leverage) / Decimal(str(price))
    return Decimal(str(allocation.value))


def size_initial_order(
    allocation: InitialAllocation,
    rules: InstrumentRules,
    *,
    leverage: int,
    price: float,
) -> SizedOrder:
    """Round the allocation to the instrument and report qty/notional/margin."""
    raw = raw_initial_qty(allocation, leverage=leverage, price=price)
    try:
        qty = rules.floor_qty(raw)
    except ValueError as exc:
        raise SizingError(
            f"{rules.symbol} initial allocation {allocation.value} "
            f"({allocation.mode}) rounds below the instrument limits: {exc}"
        ) from exc
    return _sized(qty, price, rules, leverage=leverage)


def size_dca_order(
    previous_qty: float | Decimal,
    multiplier: float,
    rules: InstrumentRules,
    *,
    leverage: int,
    price: float,
) -> SizedOrder:
    """Next ladder quantity: previous_qty * multiplier, then instrument rounding."""
    if multiplier <= 1:
        raise SizingError("DCA size multiplier must be greater than 1")
    raw = Decimal(str(previous_qty)) * Decimal(str(multiplier))
    try:
        qty = rules.floor_qty(raw)
    except ValueError as exc:
        raise SizingError(f"{rules.symbol} DCA quantity is not exchange-legal: {exc}") from exc
    return _sized(qty, price, rules, leverage=leverage)


def _sized(qty: Decimal, price: float, rules: InstrumentRules, *, leverage: int) -> SizedOrder:
    price_decimal = Decimal(str(price))
    notional = qty * price_decimal
    if notional < rules.min_notional_value:
        raise SizingError(
            f"order notional {notional} is below {rules.symbol} minimum "
            f"{rules.min_notional_value}"
        )
    return SizedOrder(
        qty=qty,
        price=price_decimal,
        notional_usdt=notional,
        margin_usdt=notional / Decimal(leverage),
        leverage=leverage,
    )


def margin_for_quantity(qty: float, price: float, leverage: int) -> float:
    """margin = qty * price / leverage."""
    if leverage < 1:
        raise SizingError("leverage must be at least 1")
    return qty * price / leverage


def quantity_for_margin(margin_usdt: float, price: float, leverage: int) -> float:
    """qty = margin * leverage / price."""
    if not isfinite(price) or price <= 0:
        raise SizingError("price must be positive and finite")
    if leverage < 1:
        raise SizingError("leverage must be at least 1")
    return margin_usdt * leverage / price


def roi_percent(entry_price: float, exit_price: float, leverage: int) -> float:
    """ROI on reserved initial margin. Never a measure of strategy edge."""
    if entry_price <= 0:
        raise SizingError("entry_price must be positive")
    return (exit_price / entry_price - 1.0) * 100.0 * leverage
