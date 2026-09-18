import pytest

from botdca.alerts import (
    Alert,
    AlertCondition,
    AlertDispatcher,
    AlertSeverity,
    JournalAlertSink,
    MemoryAlertSink,
    WebhookAlertSink,
)
from botdca.database import Database
from botdca.persistence import EventStore


def _alert(condition=AlertCondition.MAX_DCA_REACHED, symbol="HYPEUSDT", cycle_id="c1") -> Alert:
    return Alert(
        condition=condition,
        severity=AlertSeverity.CRITICAL,
        symbol=symbol,
        message="basket reached its live maximum",
        cycle_id=cycle_id,
    )


def _store() -> EventStore:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return EventStore(database)


def test_persistent_state_alerts_are_deduplicated_within_the_cooldown() -> None:
    now = [0.0]
    sink = MemoryAlertSink()
    dispatcher = AlertDispatcher([sink], cooldown_seconds=900.0, clock=lambda: now[0])

    # A two-second reconciliation loop must not emit thousands of messages.
    emitted = sum(dispatcher.dispatch(_alert()) for _ in range(450))
    now[0] = 900.0

    assert emitted == 1
    assert len(sink.alerts) == 1
    assert dispatcher.suppressed_count == 449


def test_cooldown_expiry_allows_the_state_to_alert_again() -> None:
    now = [0.0]
    sink = MemoryAlertSink()
    dispatcher = AlertDispatcher([sink], cooldown_seconds=100.0, clock=lambda: now[0])

    assert dispatcher.dispatch(_alert()) is True
    now[0] = 101.0
    assert dispatcher.dispatch(_alert()) is True
    assert len(sink.alerts) == 2


def test_clearing_a_resolved_state_lets_its_next_occurrence_alert_immediately() -> None:
    sink = MemoryAlertSink()
    dispatcher = AlertDispatcher([sink], cooldown_seconds=10_000.0)

    assert dispatcher.dispatch(_alert()) is True
    assert dispatcher.dispatch(_alert()) is False

    dispatcher.clear(AlertCondition.MAX_DCA_REACHED, "HYPEUSDT", "c1")
    assert dispatcher.dispatch(_alert()) is True


def test_different_symbols_and_cycles_are_distinct_states() -> None:
    sink = MemoryAlertSink()
    dispatcher = AlertDispatcher([sink], cooldown_seconds=10_000.0)

    dispatcher.dispatch(_alert(symbol="HYPEUSDT", cycle_id="c1"))
    dispatcher.dispatch(_alert(symbol="ONDOUSDT", cycle_id="c1"))
    dispatcher.dispatch(_alert(symbol="HYPEUSDT", cycle_id="c2"))
    dispatcher.dispatch(_alert(symbol="HYPEUSDT", cycle_id="c1"))

    assert len(sink.alerts) == 3


def test_every_important_alert_is_persisted_to_the_journal() -> None:
    store = _store()
    dispatcher = AlertDispatcher([JournalAlertSink(store)])

    dispatcher.dispatch(_alert(condition=AlertCondition.MISSING_TAKE_PROFIT))

    rows = store.recent_alerts(["HYPEUSDT"])
    assert len(rows) == 1
    assert rows[0]["condition"] == "missing_take_profit"
    assert rows[0]["severity"] == "critical"
    assert store.open_alert_conditions(["HYPEUSDT"]) == ["missing_take_profit"]
    events = store.recent_events("HYPEUSDT")
    assert any(event["event_type"] == "ALERT" for event in events)


def test_a_failing_sink_never_breaks_delivery_to_the_other_sinks() -> None:
    class Broken:
        name = "broken"

        def emit(self, alert: Alert) -> None:
            raise RuntimeError("webhook is down")

    good = MemoryAlertSink()
    dispatcher = AlertDispatcher([Broken(), good])

    assert dispatcher.dispatch(_alert()) is True
    assert len(good.alerts) == 1
    assert dispatcher.sink_errors and "broken" in dispatcher.sink_errors[0]


def test_webhook_sink_posts_json_and_respects_the_severity_floor() -> None:
    posted: list[tuple[str, bytes]] = []

    def opener(request, timeout):
        posted.append((request.full_url, request.data))

    sink = WebhookAlertSink(
        "https://example.invalid/hook",
        min_severity=AlertSeverity.CRITICAL,
        opener=opener,
    )
    sink.emit(
        Alert(AlertCondition.DCA_DEPTH_REACHED, AlertSeverity.WARNING, "HYPEUSDT", "depth")
    )
    assert posted == []

    sink.emit(_alert())
    assert len(posted) == 1
    assert posted[0][0] == "https://example.invalid/hook"
    assert b"max_dca_reached" in posted[0][1]


def test_webhook_delivery_failure_is_recorded_and_swallowed() -> None:
    def opener(request, timeout):
        raise OSError("connection refused")

    sink = WebhookAlertSink(
        "https://example.invalid/hook", min_severity=AlertSeverity.INFO, opener=opener
    )
    sink.emit(_alert())
    assert sink.last_error is not None and "OSError" in sink.last_error


def test_webhook_url_must_be_http_or_https() -> None:
    with pytest.raises(ValueError, match="must be http"):
        WebhookAlertSink("file:///etc/passwd")


def test_clearing_a_state_resolves_its_persisted_alert_for_that_cycle_only() -> None:
    store = _store()
    dispatcher = AlertDispatcher([JournalAlertSink(store)])
    dispatcher.dispatch(_alert(condition=AlertCondition.MISSING_TAKE_PROFIT, cycle_id="c1"))
    dispatcher.dispatch(_alert(condition=AlertCondition.MISSING_TAKE_PROFIT, cycle_id="c2"))
    dispatcher.dispatch(_alert(condition=AlertCondition.MISSING_TAKE_PROFIT, symbol="ONDOUSDT"))

    dispatcher.clear(AlertCondition.MISSING_TAKE_PROFIT, "HYPEUSDT", "c1")

    rows = {(row["symbol"], row["cycle_id"]): row for row in store.recent_alerts()}
    assert rows[("HYPEUSDT", "c1")]["resolved_at"] is not None
    assert rows[("HYPEUSDT", "c2")]["resolved_at"] is None
    assert store.open_alert_conditions(["ONDOUSDT"]) == ["missing_take_profit"]


def test_clearing_without_a_cycle_resolves_every_cycle_of_the_symbol() -> None:
    store = _store()
    dispatcher = AlertDispatcher([JournalAlertSink(store)])
    dispatcher.dispatch(_alert(condition=AlertCondition.WORKER_CRASHED, cycle_id="c1"))
    dispatcher.dispatch(_alert(condition=AlertCondition.WORKER_CRASHED, cycle_id=None))

    dispatcher.clear(AlertCondition.WORKER_CRASHED, "HYPEUSDT")

    assert store.open_alert_conditions(["HYPEUSDT"]) == []
    assert len(store.recent_alerts(["HYPEUSDT"])) == 2  # history is kept


def test_operator_acknowledgement_closes_alerts_but_keeps_history() -> None:
    store = _store()
    dispatcher = AlertDispatcher([JournalAlertSink(store)])
    dispatcher.dispatch(_alert(condition=AlertCondition.PRIVATE_STREAM_DISCONNECTED))
    dispatcher.dispatch(_alert(condition=AlertCondition.RECONCILIATION_FAILED))

    acknowledged = store.acknowledge_alerts(
        symbols=["HYPEUSDT"], conditions=["private_stream_disconnected"]
    )

    assert acknowledged == 1
    assert store.open_alert_conditions(["HYPEUSDT"]) == ["reconciliation_failed"]
    assert len(store.recent_alerts(["HYPEUSDT"])) == 2


def test_a_failing_resolve_never_breaks_the_trading_loop() -> None:
    class Broken:
        name = "broken"

        def emit(self, alert: Alert) -> None:
            pass

        def resolve(self, condition, symbol, cycle_id) -> None:
            raise RuntimeError("database offline")

    dispatcher = AlertDispatcher([Broken()])

    dispatcher.clear(AlertCondition.MISSING_TAKE_PROFIT, "HYPEUSDT", "c1")

    assert dispatcher.sink_errors == ["broken: RuntimeError: database offline"]
