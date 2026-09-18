from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class BotState(StrEnum):
    PAUSED = "paused"
    IDLE = "idle"
    OPENING = "opening"
    ACTIVE = "active"
    CLOSING = "closing"
    ERROR = "error"


class BasketStatus(StrEnum):
    """Lifecycle of one basket, independent of operator pause state."""

    FLAT = "flat"
    OPEN = "open"
    #: The configured live maximum was reached. No further normal DCA is placed,
    #: but TP management, reconciliation and manual reduce-only close stay active.
    MAX_DCA_REACHED = "max_dca_reached"


@dataclass(frozen=True)
class DcaStep:
    drop_percent_from_average: float
    size_multiplier_from_previous: float


DEFAULT_DCA_STEPS: tuple[DcaStep, ...] = (
    DcaStep(1.05, 1.339),
    DcaStep(1.41, 1.539),
    DcaStep(1.48, 1.430),
    DcaStep(1.91, 1.442),
    DcaStep(3.12, 1.449),
    DcaStep(3.56, 1.450),
    DcaStep(5.53, 1.466),
    DcaStep(5.11, 1.467),
)


@dataclass
class Fill:
    price: float
    qty: float
    kind: str


@dataclass
class TradingCycle:
    symbol: str
    leverage: int
    base_margin_usdt: float
    tp_percent: float
    id: str = field(default_factory=lambda: str(uuid4()))
    fills: list[Fill] = field(default_factory=list)
    realized_pnl_usdt: float = 0.0
    dca_level_override: int | None = None
    last_order_qty_override: float | None = None
    #: The strategy version this basket opened with. A basket is pinned to it for
    #: its whole life, so editing parameters can never alter an open basket.
    strategy_version_id: str | None = None
    #: Deepest DCA level this basket may reach, fixed at open time.
    max_dca_level: int | None = None

    @property
    def total_qty(self) -> float:
        return sum(fill.qty for fill in self.fills)

    @property
    def average_entry(self) -> float | None:
        qty = self.total_qty
        if qty <= 0:
            return None
        return sum(fill.price * fill.qty for fill in self.fills) / qty

    @property
    def dca_level(self) -> int:
        if self.dca_level_override is not None:
            return self.dca_level_override
        return max(0, len(self.fills) - 1)

    @property
    def last_order_qty(self) -> float | None:
        if self.last_order_qty_override is not None:
            return self.last_order_qty_override
        if not self.fills:
            return None
        return self.fills[-1].qty

    @property
    def tp_price(self) -> float | None:
        avg = self.average_entry
        if avg is None:
            return None
        return avg * (1 + self.tp_percent / 100)

    @property
    def has_dca_ladder(self) -> bool:
        """False for a take-profit-only basket (live depth capped at DCA0)."""
        return self.max_dca_level is None or self.max_dca_level > 0

    @property
    def at_max_dca(self) -> bool:
        # A basket with no ladder cannot exhaust it; it simply holds for its TP.
        return (
            self.max_dca_level is not None
            and self.has_dca_ladder
            and self.dca_level >= self.max_dca_level
        )

    @property
    def status(self) -> BasketStatus:
        if self.total_qty <= 0:
            return BasketStatus.FLAT
        return BasketStatus.MAX_DCA_REACHED if self.at_max_dca else BasketStatus.OPEN
