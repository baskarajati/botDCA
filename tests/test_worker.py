from threading import Event

from botdca.exchange import PositionSnapshot
from botdca.live_service import LiveSyncResult
from botdca.strategy import DcaStrategy, StrategyConfig
from botdca.worker import LiveWorker


class FakeStream:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.start_count = 0

    @property
    def connected(self) -> bool:
        return self.started and not self.stopped

    def start(self) -> None:
        self.started = True
        self.stopped = False
        self.start_count += 1

    def stop(self) -> None:
        self.stopped = True


class FakeStore:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_strategy_event(self, **kwargs) -> int:
        self.events.append(kwargs)
        return len(self.events)


class FakeService:
    def __init__(self) -> None:
        self.strategy = DcaStrategy(StrategyConfig())
        self.calls = 0
        self.called = Event()

    @property
    def symbol(self) -> str:
        return "HYPEUSDT"

    def sync(self) -> LiveSyncResult:
        self.calls += 1
        self.called.set()
        return LiveSyncResult(
            status="flat_paused",
            position=PositionSnapshot(
                symbol="HYPEUSDT",
                side="",
                size=0.0,
                average_entry=0.0,
                leverage=0.0,
                mark_price=80.0,
                liquidation_price=None,
                unrealized_pnl=0.0,
            ),
        )


def test_run_once_records_durable_sync_event() -> None:
    service = FakeService()
    store = FakeStore()
    stream = FakeStream()
    stream.start()
    worker = LiveWorker(
        service=service,
        stream=stream,
        store=store,
        interval_seconds=1.0,
    )

    result = worker.run_once()

    assert result.status == "flat_paused"
    assert worker.last_result == result
    assert worker.last_error is None
    assert store.events[0]["event_type"] == "LIVE_SYNC"
    assert store.events[0]["symbol"] == "HYPEUSDT"
    assert store.events[0]["payload"]["status"] == "flat_paused"


def test_worker_starts_stream_runs_and_stops_cleanly() -> None:
    service = FakeService()
    store = FakeStore()
    stream = FakeStream()
    worker = LiveWorker(
        service=service,
        stream=stream,
        store=store,
        interval_seconds=0.05,
    )

    worker.start()
    assert service.called.wait(timeout=1.0)
    worker.stop()

    assert stream.started is True
    assert stream.stopped is True
    assert worker.running is False
    assert service.calls >= 1


def test_worker_reconnects_stream_before_sync() -> None:
    service = FakeService()
    store = FakeStore()
    stream = FakeStream()
    stream.start()
    stream.stopped = True
    worker = LiveWorker(service=service, stream=stream, store=store)

    worker.run_once()

    assert stream.start_count == 2
    assert [event["event_type"] for event in store.events] == [
        "LIVE_STREAM_RECONNECTED",
        "LIVE_SYNC",
    ]


def test_a_failed_stream_message_alerts_once_and_recovers_over_rest_at_once() -> None:
    from botdca.alerts import AlertDispatcher, MemoryAlertSink

    service = FakeService()
    stream = FakeStream()
    stream.start()
    stream.failed_messages = 0
    stream.last_message_error = None
    recoveries: list[int] = []
    sink = MemoryAlertSink()
    worker = LiveWorker(
        service=service,
        stream=stream,
        store=FakeStore(),
        interval_seconds=1.0,
        execution_recovery=lambda: recoveries.append(1),
        alerts=AlertDispatcher([sink]),
    )
    worker.run_once()  # first run always recovers
    assert len(recoveries) == 1

    stream.failed_messages = 2
    stream.last_message_error = "execution: DataError: integer out of range"
    worker.run_once()

    assert len(recoveries) == 2  # immediately, not after the 30 s cadence
    alerts = sink.recent()
    assert [alert["condition"] for alert in alerts] == ["stream_message_failed"]
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["context"]["last_error"].startswith("execution: DataError")

    worker.run_once()  # no new failures: no alert, no extra recovery
    assert len(recoveries) == 2
    assert len(sink.recent()) == 1
