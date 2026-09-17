from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class OrderResult:
    order_id: str
    status: str
    price: float
    qty: float


class ExchangeExecutor(Protocol):
    def open_long(self, symbol: str, qty: float) -> OrderResult: ...
    def add_long(self, symbol: str, qty: float) -> OrderResult: ...
    def close_long(self, symbol: str, qty: float) -> OrderResult: ...
    def cancel_all(self, symbol: str) -> None: ...


class DryRunExecutor:
    def __init__(self, price_provider) -> None:
        self.price_provider = price_provider
        self.counter = 0

    def _result(self, qty: float) -> OrderResult:
        self.counter += 1
        price = float(self.price_provider())
        return OrderResult(
            order_id=f"dry-{self.counter}",
            status="filled",
            price=price,
            qty=qty,
        )

    def open_long(self, symbol: str, qty: float) -> OrderResult:
        return self._result(qty)

    def add_long(self, symbol: str, qty: float) -> OrderResult:
        return self._result(qty)

    def close_long(self, symbol: str, qty: float) -> OrderResult:
        return self._result(qty)

    def cancel_all(self, symbol: str) -> None:
        return None
