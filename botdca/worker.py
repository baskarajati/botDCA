from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from threading import Event, Lock, Thread
from time import monotonic, time
from typing import Protocol

from botdca.alerts import Alert, AlertCondition, AlertDispatcher, AlertSeverity
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
        execution_recovery: Callable[[], None] | None = None,
        alerts: AlertDispatcher | None = None,
        sync_journal_heartbeat_seconds: float = 300.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if sync_journal_heartbeat_seconds <= 0:
            raise ValueError("sync_journal_heartbeat_seconds must be positive")
        self.alerts = alerts or AlertDispatcher()
        self.service = service
        self.stream = stream
        self.store = store
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self.last_result: LiveSyncResult | None = None
        self.last_error: str | None = None
        self.last_success_at_ms: int | None = None
        self.execution_recovery = execution_recovery
        self._last_recovery_at: float | None = None
        self._stream_alerted = False
        self._seen_stream_failures = 0
        # A sync every two seconds would otherwise journal ~43,000 identical
        # rows a day. Journal a sync when its meaning changes, plus a heartbeat.
        self.sync_journal_heartbeat_seconds = sync_journal_heartbeat_seconds
        self._last_journaled_state: tuple | None = None
        self._last_journaled_at: float | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def run_once(self) -> LiveSyncResult:
        reconnected = not self.stream.connected
        if not self.stream.connected:
            self._alert(
                AlertCondition.PRIVATE_STREAM_DISCONNECTED,
                AlertSeverity.CRITICAL,
                f"{self.service.symbol} private stream is disconnected; reconnecting",
            )
            self.stream.stop()
            self.stream.start()
            self.store.record_strategy_event(
                event_type="LIVE_STREAM_RECONNECTED",
                symbol=self.service.symbol,
                payload={},
            )
            self._stream_alerted = True
        elif self._stream_alerted:
            # Only clear a disconnect we actually raised, so the healthy path
            # does no work at all.
            self.alerts.clear(
                AlertCondition.PRIVATE_STREAM_DISCONNECTED, self.service.symbol
            )
            self._stream_alerted = False
        failures = getattr(self.stream, "failed_messages", 0)
        missed_messages = failures > self._seen_stream_failures
        if missed_messages:
            self._alert(
                AlertCondition.STREAM_MESSAGE_FAILED,
                AlertSeverity.WARNING,
                f"{self.service.symbol} private stream could not process "
                f"{failures - self._seen_stream_failures} message(s); recovering over REST",
                last_error=getattr(self.stream, "last_message_error", None),
            )
            self._seen_stream_failures = failures
        if self.execution_recovery is not None and (
            reconnected
            or missed_messages
            or self._last_recovery_at is None
            or monotonic() - self._last_recovery_at >= 30
        ):
            self.execution_recovery()
            self._last_recovery_at = monotonic()
        result = self.service.sync()
        self.last_result = result
        self.last_error = None
        cycle = self.service.strategy.current_cycle
        state = _journal_state(result, cycle.id if cycle is not None else None)
        now = monotonic()
        if (
            state != self._last_journaled_state
            or self._last_journaled_at is None
            or now - self._last_journaled_at >= self.sync_journal_heartbeat_seconds
        ):
            self.store.record_strategy_event(
                event_type="LIVE_SYNC",
                symbol=self.service.symbol,
                cycle_id=cycle.id if cycle is not None else None,
                payload=asdict(result),
            )
            self._last_journaled_state = state
            self._last_journaled_at = now
        self.last_success_at_ms = int(time() * 1000)
        self.alerts.clear(AlertCondition.WORKER_CRASHED, self.service.symbol)
        return result

    def _alert(
        self,
        condition: AlertCondition,
        severity: AlertSeverity,
        message: str,
        **context: object,
    ) -> None:
        cycle = self.service.strategy.current_cycle
        self.alerts.dispatch(
            Alert(
                condition=condition,
                severity=severity,
                symbol=self.service.symbol,
                message=message,
                cycle_id=cycle.id if cycle is not None else None,
                context=dict(context),
            )
        )

    @property
    def stale(self) -> bool:
        """No successful reconciliation within three worker intervals."""
        if self.last_success_at_ms is None:
            return self.running
        return time() * 1000 - self.last_success_at_ms > max(
            15000.0, self.interval_seconds * 3000
        )

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
                self._alert(
                    AlertCondition.WORKER_CRASHED,
                    AlertSeverity.CRITICAL,
                    f"{self.service.symbol} worker failed and paused re-entry: "
                    f"{self.last_error}",
                    error=self.last_error,
                )
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


def _journal_state(result: LiveSyncResult, cycle_id: str | None) -> tuple:
    """What a sync means, ignoring values that move on every tick (mark, PnL, account)."""
    position = result.position

    def order_id(ack) -> str | None:
        return ack.order_id if ack is not None else None

    return (
        cycle_id,
        result.status,
        position.side,
        position.size,
        position.average_entry,
        order_id(result.entry_order),
        order_id(result.take_profit_order),
        order_id(result.dca_order),
        result.dca_blocked_reason,
        result.max_dca_reached,
        result.manual_intervention_required,
        result.protection_status,
        result.strategy_version_id,
    )
