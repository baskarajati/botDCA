from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Lock
from typing import Any

from pybit.unified_trading import WebSocket

from botdca.event_processor import ExchangeEventProcessor

logger = logging.getLogger(__name__)


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
        # pybit closes the socket without reconnecting when a callback raises
        # anything other than a network error, so one failed database write
        # would silently cost the stream. Failures are counted instead and the
        # worker recovers the missed executions over REST.
        self._failure_lock = Lock()
        self.failed_messages = 0
        self.last_message_error: str | None = None

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
        websocket.execution_stream(
            callback=self._guarded("execution", self.processor.handle_execution_message)
        )
        websocket.order_stream(callback=self._guarded("order", self.processor.handle_order_message))
        websocket.position_stream(
            callback=self._guarded("position", self.processor.handle_position_message)
        )
        self.websocket = websocket

    def stop(self) -> None:
        websocket = self.websocket
        self.websocket = None
        if websocket is not None and hasattr(websocket, "exit"):
            websocket.exit()

    def _guarded(self, topic: str, handler: Callable[[dict[str, Any]], Any]):
        def callback(message: dict[str, Any]) -> None:
            try:
                handler(message)
            except Exception as exc:  # never let pybit close the socket
                detail = f"{topic}: {type(exc).__name__}: {exc}"[:500]
                with self._failure_lock:
                    self.failed_messages += 1
                    self.last_message_error = detail
                logger.exception("private stream %s message could not be processed", topic)

        return callback
