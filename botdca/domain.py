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
        return max(0, len(self.fills) - 1)

    @property
    def tp_price(self) -> float | None:
        avg = self.average_entry
        if avg is None:
            return None
        return avg * (1 + self.tp_percent / 100)
