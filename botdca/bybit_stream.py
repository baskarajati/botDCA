from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pybit.unified_trading import WebSocket

from botdca.event_processor import ExchangeEventProcessor


class BybitPrivateStream:
    """Wire Bybit private V5 streams into the durable event processor."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        testnet: bool,
        processor: ExchangeEventProcessor,
        websocket_factory: Callable[..., Any] = WebSocket,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Bybit API credentials are required for private streams")
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.processor = processor
        self.websocket_factory = websocket_factory
        self.websocket: Any | None = None

    @property
    def connected(self) -> bool:
        websocket = self.websocket
        if websocket is None:
            return False
        is_connected = getattr(websocket, "is_connected", None)
        return bool(is_connected()) if callable(is_connected) else True

    def start(self) -> None:
        if self.websocket is not None:
            raise RuntimeError("private stream is already started")
        websocket = self.websocket_factory(
            testnet=self.testnet,
            channel_type="private",
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        websocket.execution_stream(callback=self.processor.handle_execution_message)
        websocket.order_stream(callback=self.processor.handle_order_message)
        websocket.position_stream(callback=self.processor.handle_position_message)
        self.websocket = websocket

    def stop(self) -> None:
        websocket = self.websocket
        self.websocket = None
        if websocket is not None and hasattr(websocket, "exit"):
            websocket.exit()
