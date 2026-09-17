from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


class LiveTradingDisabled(RuntimeError):
    """Raised when a mutating exchange action is attempted while live trading is off."""


@dataclass(frozen=True)
class OrderAck:
    order_id: str
    order_link_id: str
    accepted: bool = True


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    side: str
    size: float
    average_entry: float
    leverage: float
    mark_price: float
    liquidation_price: float | None
    unrealized_pnl: float

    @property
    def is_open(self) -> bool:
        return self.size > 0


class ExchangeExecutor(Protocol):
    def set_leverage(self, symbol: str, leverage: int) -> None: ...
    def get_position(self, symbol: str) -> PositionSnapshot: ...
    def open_long(self, symbol: str, qty: float) -> OrderAck: ...
    def add_long(self, symbol: str, qty: float) -> OrderAck: ...
    def close_long(self, symbol: str, qty: float) -> OrderAck: ...
    def cancel_all(self, symbol: str) -> None: ...


def new_order_link_id(prefix: str) -> str:
    token = uuid4().hex[:20]
    return f"botdca-{prefix}-{token}"[:36]


class DryRunExecutor:
    """Non-mutating adapter used by development runtime tests and UI work."""

    def __init__(self) -> None:
        self.counter = 0
        self.position = PositionSnapshot(
            symbol="",
            side="",
            size=0.0,
            average_entry=0.0,
            leverage=0.0,
            mark_price=0.0,
            liquidation_price=None,
            unrealized_pnl=0.0,
        )

    def _ack(self, prefix: str) -> OrderAck:
        self.counter += 1
        return OrderAck(
            order_id=f"dry-{self.counter}",
            order_link_id=new_order_link_id(prefix),
        )

    def set_leverage(self, symbol: str, leverage: int) -> None:
        return None

    def get_position(self, symbol: str) -> PositionSnapshot:
        if self.position.symbol in {"", symbol}:
            return PositionSnapshot(
                symbol=symbol,
                side=self.position.side,
                size=self.position.size,
                average_entry=self.position.average_entry,
                leverage=self.position.leverage,
                mark_price=self.position.mark_price,
                liquidation_price=self.position.liquidation_price,
                unrealized_pnl=self.position.unrealized_pnl,
            )
        return PositionSnapshot(symbol, "", 0.0, 0.0, 0.0, 0.0, None, 0.0)

    def open_long(self, symbol: str, qty: float) -> OrderAck:
        return self._ack("open")

    def add_long(self, symbol: str, qty: float) -> OrderAck:
        return self._ack("dca")

    def close_long(self, symbol: str, qty: float) -> OrderAck:
        return self._ack("close")

    def cancel_all(self, symbol: str) -> None:
        return None
