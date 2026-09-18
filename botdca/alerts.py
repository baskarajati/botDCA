"""Modular alerting.

Strategy and runtime code raises `Alert` objects. Delivery is a sink concern, so
no vendor ever appears in strategy code.

Persistent states are deduplicated. A condition re-checked every two seconds
must not produce thousands of messages, so a repeated alert with the same
dedupe key is suppressed until its cooldown expires or its state clears.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from time import monotonic
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import request as urlrequest

DEFAULT_DEDUPE_COOLDOWN_SECONDS = 900.0


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCondition(StrEnum):
    """Every condition the runtime may alert on."""

    WORKER_CRASHED = "worker_crashed"
    WORKER_STALE = "worker_stale"
    PRIVATE_STREAM_DISCONNECTED = "private_stream_disconnected"
    RECONCILIATION_FAILED = "reconciliation_failed"
    ORDER_REJECTED = "order_rejected"
    RISK_BLOCKED_ENTRY = "risk_blocked_entry"
    RISK_BLOCKED_DCA = "risk_blocked_dca"
    DCA_DEPTH_REACHED = "dca_depth_reached"
    MAX_DCA_REACHED = "max_dca_reached"
    LOW_AVAILABLE_EQUITY = "low_available_equity"
    PORTFOLIO_FLOATING_LOSS = "portfolio_floating_loss"
    MISSING_TAKE_PROFIT = "missing_take_profit"
    PROTECTION_REPAIRED = "protection_repaired"
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"


@dataclass(frozen=True, slots=True)
class Alert:
    condition: AlertCondition
    severity: AlertSeverity
    symbol: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    cycle_id: str | None = None

    @property
    def dedupe_key(self) -> str:
        """Identity of the persistent state, not of the individual observation."""
        return f"{self.condition}:{self.symbol.upper()}:{self.cycle_id or '-'}"

    def describe(self) -> dict:
        return {
            "condition": str(self.condition),
            "severity": str(self.severity),
            "symbol": self.symbol.upper(),
            "message": self.message,
            "cycle_id": self.cycle_id,
            "context": self.context,
        }


class AlertSink(Protocol):
    """Somewhere an alert can be delivered."""

    name: str

    def emit(self, alert: Alert) -> None: ...


class JournalAlertSink:
    """Persist every important alert durably, and mirror it into the journal."""

    name = "journal"

    def __init__(self, store: Any) -> None:
        self.store = store

    def emit(self, alert: Alert) -> None:
        self.store.record_alert(
            condition=str(alert.condition),
            severity=str(alert.severity),
            symbol=alert.symbol,
            message=alert.message,
            dedupe_key=alert.dedupe_key,
            cycle_id=alert.cycle_id,
            context=alert.context,
        )
        self.store.record_strategy_event(
            event_type="ALERT",
            symbol=alert.symbol,
            cycle_id=alert.cycle_id,
            payload=alert.describe(),
        )

    def resolve(self, condition: AlertCondition, symbol: str, cycle_id: str | None) -> None:
        self.store.resolve_alerts(condition=str(condition), symbol=symbol, cycle_id=cycle_id)


class MemoryAlertSink:
    """Collect alerts in process. Used by tests and by the operator console."""

    name = "memory"

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self.alerts: list[Alert] = []
        self._lock = RLock()

    def emit(self, alert: Alert) -> None:
        with self._lock:
            self.alerts.append(alert)
            if len(self.alerts) > self.capacity:
                del self.alerts[: len(self.alerts) - self.capacity]

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [alert.describe() for alert in self.alerts[-limit:][::-1]]


class WebhookAlertSink:
    """Minimal generic webhook sink.

    A JSON body is POSTed to an operator-supplied URL. This is deliberately
    vendor-neutral: a Telegram, Slack or PagerDuty relay is configuration, not
    code. Delivery failures never propagate into the trading loop.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 5.0,
        min_severity: AlertSeverity = AlertSeverity.WARNING,
        opener: Callable[[Any, float], Any] | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("alert webhook URL must be http(s)")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.min_severity = min_severity
        self._opener = opener or (lambda req, timeout: urlrequest.urlopen(req, timeout=timeout))
        self.last_error: str | None = None

    def emit(self, alert: Alert) -> None:
        order = {
            AlertSeverity.INFO: 0,
            AlertSeverity.WARNING: 1,
            AlertSeverity.CRITICAL: 2,
        }
        if order[alert.severity] < order[self.min_severity]:
            return
        body = json.dumps(alert.describe()).encode("utf-8")
        request = urlrequest.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            self._opener(request, self.timeout_seconds)
            self.last_error = None
        except (urlerror.URLError, OSError, ValueError) as exc:
            # A failing notification channel must never stop reconciliation.
            self.last_error = f"{type(exc).__name__}: {exc}"


class AlertDispatcher:
    """Fan an alert out to every sink, once per persistent state."""

    def __init__(
        self,
        sinks: Iterable[AlertSink] = (),
        *,
        cooldown_seconds: float = DEFAULT_DEDUPE_COOLDOWN_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.sinks = list(sinks)
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._last_emitted: dict[str, float] = {}
        self._lock = RLock()
        self.suppressed_count = 0
        self.sink_errors: list[str] = []

    def add_sink(self, sink: AlertSink) -> None:
        with self._lock:
            self.sinks.append(sink)

    def clear(self, condition: AlertCondition, symbol: str, cycle_id: str | None = None) -> None:
        """Mark a persistent state resolved so its next occurrence alerts again.

        Sinks that keep durable state (the journal) also mark their open alerts
        resolved, so a cleared condition stops counting as open in readiness.
        Without a cycle id the durable resolution covers every cycle.
        """
        key = f"{condition}:{symbol.upper()}:{cycle_id or '-'}"
        with self._lock:
            self._last_emitted.pop(key, None)
            sinks = list(self.sinks)
        for sink in sinks:
            resolve = getattr(sink, "resolve", None)
            if resolve is None:
                continue
            try:
                resolve(condition, symbol, cycle_id)
            except Exception as exc:  # noqa: BLE001 - alerting never breaks the trading loop
                self.sink_errors.append(f"{sink.name}: {type(exc).__name__}: {exc}")
                del self.sink_errors[:-20]

    def dispatch(self, alert: Alert, *, force: bool = False) -> bool:
        """Deliver the alert unless an identical state is still within cooldown."""
        with self._lock:
            now = self.clock()
            key = alert.dedupe_key
            last = self._last_emitted.get(key)
            if not force and last is not None and now - last < self.cooldown_seconds:
                self.suppressed_count += 1
                return False
            self._last_emitted[key] = now
            sinks = list(self.sinks)
        for sink in sinks:
            try:
                sink.emit(alert)
            except Exception as exc:  # noqa: BLE001 - alerting never breaks the trading loop
                self.sink_errors.append(f"{sink.name}: {type(exc).__name__}: {exc}")
                del self.sink_errors[:-20]
        return True
