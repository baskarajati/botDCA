from types import SimpleNamespace

import pytest

from botdca.bybit_exchange import BybitApiError, BybitExchange
from botdca.database import Database
from botdca.execution_recovery import WEEK_MS, ExecutionRecovery
from botdca.persistence import EventStore
from botdca.worker import LiveWorker


def store():
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return EventStore(database)


def row(identity):
    return {
        "symbol": "HYPEUSDT",
        "orderId": "order1",
        "orderLinkId": "botdca-open-test",
        "execId": identity,
        "side": "Buy",
        "execPrice": "80.12",
        "execQty": ".1",
        "execFee": ".004",
        "execTime": "123456",
    }


def test_recovery_paginates_deduplicates_and_checkpoints_only_after_completion():
    class Session:
        def __init__(self):
            self.calls = []

        def get_executions(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "retCode": 0,
                "result": {
                    "list": [row("second" if kwargs.get("cursor") else "first")],
                    "nextPageCursor": "next" if not kwargs.get("cursor") else "",
                },
            }

    s = store()
    session = Session()
    recovery = ExecutionRecovery(
        BybitExchange(session=session), s, "HYPEUSDT", clock=lambda: 1000000
    )
    recovery()
    recovery()
    assert s.execution_count() == 2
    assert s.execution_recovery_checkpoint("HYPEUSDT") == 1000000000
    assert session.calls[1]["cursor"] == "next"
    assert session.calls[2]["startTime"] == 1000000000 - 60000


def test_repeated_cursor_does_not_advance_checkpoint():
    class Session:
        def get_executions(self, **kwargs):
            return {"retCode": 0, "result": {"list": [row("first")], "nextPageCursor": "repeat"}}

    s = store()
    with pytest.raises(BybitApiError, match="pagination"):
        ExecutionRecovery(BybitExchange(session=Session()), s, "HYPEUSDT", clock=lambda: 1000000)()
    assert s.execution_recovery_checkpoint("HYPEUSDT") is None


def test_old_recovery_gap_is_refused_before_api_call():
    s = store()
    s.record_strategy_event(
        event_type="EXECUTION_RECOVERY", symbol="HYPEUSDT", payload={"end_ms": 1}
    )
    with pytest.raises(BybitApiError, match="seven days"):
        ExecutionRecovery(None, s, "HYPEUSDT", clock=lambda: WEEK_MS / 1000 + 100)()


def test_worker_does_not_sync_if_execution_recovery_fails():
    stream = SimpleNamespace(connected=True)
    service = SimpleNamespace(sync=lambda: pytest.fail("must not reconcile"))

    def fail():
        raise BybitApiError("recovery unavailable")

    worker = LiveWorker(service=service, stream=stream, store=None, execution_recovery=fail)
    with pytest.raises(BybitApiError):
        worker.run_once()
    assert worker.last_success_at_ms is None
