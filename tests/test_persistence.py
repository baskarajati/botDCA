from botdca.bybit_events import ExecutionEvent, PositionEvent
from botdca.database import Database
from botdca.persistence import EventStore


def make_store() -> EventStore:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return EventStore(database)


def test_execution_is_idempotent_by_execution_id() -> None:
    store = make_store()
    event = ExecutionEvent(
        symbol="HYPEUSDT",
        order_id="order-1",
        order_link_id="botdca-open-1",
        execution_id="exec-1",
        side="Buy",
        price=78.0,
        qty=0.2,
        fee=0.01,
        realized_pnl=0.0,
        execution_time_ms=1000,
    )

    assert store.record_execution(event) is True
    assert store.record_execution(event) is False
    assert store.execution_count() == 1


def test_latest_position_uses_exchange_creation_time() -> None:
    store = make_store()
    older = PositionEvent(
        symbol="HYPEUSDT",
        side="Buy",
        size=1.0,
        average_entry=78.0,
        leverage=24.0,
        mark_price=78.2,
        liquidation_price=72.0,
        unrealized_pnl=0.2,
        position_idx=0,
        creation_time_ms=1000,
    )
    newer = PositionEvent(
        symbol="HYPEUSDT",
        side="Buy",
        size=2.0,
        average_entry=77.5,
        leverage=24.0,
        mark_price=78.0,
        liquidation_price=71.0,
        unrealized_pnl=1.0,
        position_idx=0,
        creation_time_ms=2000,
    )

    store.record_position(newer)
    store.record_position(older)
    latest = store.latest_position("hypeusdt")

    assert latest is not None
    assert latest.creation_time_ms == 2000
    assert latest.size == 2.0


def test_strategy_event_is_recorded() -> None:
    store = make_store()
    record_id = store.record_strategy_event(
        event_type="manual_close_requested",
        symbol="HYPEUSDT",
        cycle_id="cycle-1",
        payload={"reason": "ui"},
    )

    assert record_id == 1
