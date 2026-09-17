from __future__ import annotations

from dataclasses import asdict
from threading import Event, Lock, Thread
from typing import Protocol

from botdca.live_service import LiveStrategyService, LiveSyncResult
from botdca.persistence import EventStore


class PrivateStream(Protocol):
    @property
    def connected(self) -> bool: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


class LiveWorker:
    """Continuously reconcile exchange state and rebuild strategy orders.

    The worker is intentionally not auto-started by importing the API. A
    deployment must explicitly start it after constructing authenticated live
    dependencies.
    """

    def __init__(
        self,
        *,
        service: LiveStrategyService,
        stream: PrivateStream,
        store: EventStore,
        interval_seconds: float = 2.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.service = service
        self.stream = stream
        self.store = store
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self.last_result: LiveSyncResult | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def run_once(self) -> LiveSyncResult:
        if not self.stream.connected:
            self.stream.stop()
            self.stream.start()
            self.store.record_strategy_event(
                event_type="LIVE_STREAM_RECONNECTED",
                symbol=self.service.symbol,
                payload={},
            )
        result = self.service.sync()
        self.last_result = result
        self.last_error = None
        self.store.record_strategy_event(
            event_type="LIVE_SYNC",
            symbol=self.service.symbol,
            cycle_id=(
                self.service.strategy.current_cycle.id
                if self.service.strategy.current_cycle is not None
                else None
            ),
            payload=asdict(result),
        )
        return result

    def start(self) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("live worker is already running")
            self._stop.clear()
            self.stream.start()
            self._thread = Thread(target=self._run, name="botdca-live-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self.stream.stop()
            if thread is not None:
                thread.join(timeout=max(5.0, self.interval_seconds * 2))
                if thread.is_alive():
                    raise RuntimeError("live worker did not stop within the safety timeout")
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - daemon safety boundary must fail closed
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.service.strategy.pause()
                self.store.record_strategy_event(
                    event_type="LIVE_SYNC_ERROR",
                    symbol=self.service.symbol,
                    cycle_id=(
                        self.service.strategy.current_cycle.id
                        if self.service.strategy.current_cycle is not None
                        else None
                    ),
                    payload={"error": self.last_error},
                )
            self._stop.wait(self.interval_seconds)
