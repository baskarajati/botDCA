from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botdca.bybit_events import (
    parse_execution_message,
    parse_order_message,
    parse_position_message,
)
from botdca.persistence import EventStore


@dataclass(frozen=True)
class IngestResult:
    received: int
    persisted: int


class ExchangeEventProcessor:
    def __init__(self, *, store: EventStore, symbol: str) -> None:
        self.store = store
        self.symbol = symbol.upper()

    def handle_execution_message(self, message: dict[str, Any]) -> IngestResult:
        events = parse_execution_message(message, symbol=self.symbol)
        persisted = sum(1 for event in events if self.store.record_execution(event))
        return IngestResult(received=len(events), persisted=persisted)

    def handle_order_message(self, message: dict[str, Any]) -> IngestResult:
        events = parse_order_message(message, symbol=self.symbol)
        persisted = sum(1 for event in events if self.store.record_order(event))
        return IngestResult(received=len(events), persisted=persisted)

    def handle_position_message(self, message: dict[str, Any]) -> IngestResult:
        events = parse_position_message(message, symbol=self.symbol)
        for event in events:
            self.store.record_position(event)
        return IngestResult(received=len(events), persisted=len(events))
