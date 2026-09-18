"""The position-protection invariant.

An open bot position must either hold a valid exchange-hosted take profit, or
be explicitly unhealthy and flagged for intervention. A position that is open
with no resting reduce-only exit is the single worst silent failure this bot
can have, so it is journaled, alerted and surfaced in readiness rather than
being quietly retried forever.

Repair is attempted only when it is unambiguously safe, and never in a way that
can leave two reduce-only exits resting at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from botdca.exchange import OpenOrder


class ProtectionStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # flat, nothing to protect
    PROTECTED = "protected"
    REPAIRED = "repaired"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"  # several reduce-only exits; a human must resolve it


@dataclass(frozen=True, slots=True)
class ProtectionAssessment:
    status: ProtectionStatus
    symbol: str
    position_qty: float
    protected_qty: float
    resting_tp_orders: int
    detail: str

    @property
    def healthy(self) -> bool:
        return self.status in {ProtectionStatus.NOT_APPLICABLE, ProtectionStatus.PROTECTED}

    @property
    def requires_intervention(self) -> bool:
        return self.status is ProtectionStatus.AMBIGUOUS

    def describe(self) -> dict:
        return {
            "status": str(self.status),
            "symbol": self.symbol,
            "healthy": self.healthy,
            "position_qty": self.position_qty,
            "protected_qty": self.protected_qty,
            "resting_tp_orders": self.resting_tp_orders,
            "requires_intervention": self.requires_intervention,
            "detail": self.detail,
        }


def is_bot_take_profit(order: OpenOrder) -> bool:
    return (
        order.order_link_id.startswith("botdca-tp-")
        and order.side == "Sell"
        and order.reduce_only
    )


def assess_protection(
    *,
    symbol: str,
    position_qty: float,
    open_orders: list[OpenOrder],
    expected_qty: Decimal | float | None = None,
    qty_relative_tolerance: float = 1e-6,
) -> ProtectionAssessment:
    """Classify whether an open position is covered by a valid resting TP."""
    if position_qty <= 0:
        return ProtectionAssessment(
            status=ProtectionStatus.NOT_APPLICABLE,
            symbol=symbol,
            position_qty=position_qty,
            protected_qty=0.0,
            resting_tp_orders=0,
            detail="no open position to protect",
        )

    tp_orders = [order for order in open_orders if is_bot_take_profit(order)]
    protected_qty = sum(order.qty for order in tp_orders)

    if not tp_orders:
        return ProtectionAssessment(
            status=ProtectionStatus.MISSING,
            symbol=symbol,
            position_qty=position_qty,
            protected_qty=0.0,
            resting_tp_orders=0,
            detail=(
                f"open {symbol} position of {position_qty} has no resting bot take profit"
            ),
        )

    if len(tp_orders) > 1:
        return ProtectionAssessment(
            status=ProtectionStatus.AMBIGUOUS,
            symbol=symbol,
            position_qty=position_qty,
            protected_qty=protected_qty,
            resting_tp_orders=len(tp_orders),
            detail=(
                f"{len(tp_orders)} resting bot take-profit orders found for {symbol}; "
                "a duplicate exit must be resolved manually"
            ),
        )

    target = float(expected_qty) if expected_qty is not None else position_qty
    tolerance = max(1e-9, abs(target) * qty_relative_tolerance)
    if abs(protected_qty - target) > tolerance:
        return ProtectionAssessment(
            status=ProtectionStatus.MISSING,
            symbol=symbol,
            position_qty=position_qty,
            protected_qty=protected_qty,
            resting_tp_orders=len(tp_orders),
            detail=(
                f"resting take profit covers {protected_qty} of {target} {symbol}; "
                "protection must be rebuilt"
            ),
        )

    return ProtectionAssessment(
        status=ProtectionStatus.PROTECTED,
        symbol=symbol,
        position_qty=position_qty,
        protected_qty=protected_qty,
        resting_tp_orders=len(tp_orders),
        detail="position is covered by a valid exchange-hosted take profit",
    )


def repaired(assessment: ProtectionAssessment, detail: str) -> ProtectionAssessment:
    """Mark an assessment as repaired after protection was successfully placed."""
    return ProtectionAssessment(
        status=ProtectionStatus.REPAIRED,
        symbol=assessment.symbol,
        position_qty=assessment.position_qty,
        protected_qty=assessment.position_qty,
        resting_tp_orders=1,
        detail=detail,
    )
