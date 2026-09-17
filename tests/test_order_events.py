from botdca.bybit_events import OrderEvent, parse_order_message
from botdca.database import Database
from botdca.persistence import EventStore


def make_event(status: str, updated: int, *, cum_qty: float = 0.0) -> OrderEvent:
    return OrderEvent(
        symbol="HYPEUSDT",
        order_id="order-1",
        order_link_id="botdca-dca-abc",
        side="Buy",
        order_type="Limit",
        status=status,
        price=78.0,
        qty=0.4,
        cumulative_executed_qty=cum_qty,
        average_price=78.0 if cum_qty else 0.0,
        reduce_only=False,
        reject_reason="EC_NoError",
        cancel_type="",
        updated_time_ms=updated,
    )


def test_parse_order_message() -> None:
    message = {
        "creationTime": 2000,
        "data": [
            {
                "category": "linear",
                "symbol": "HYPEUSDT",
                "orderId": "order-1",
                "orderLinkId": "botdca-tp-abc",
                "side": "Sell",
                "orderType": "Limit",
                "orderStatus": "Filled",
                "price": "80.50",
                "qty": "0.70",
                "cumExecQty": "0.70",
                "avgPrice": "80.51",
                "reduceOnly": True,
                "rejectReason": "EC_NoError",
                "cancelType": "",
                "updatedTime": "1999",
            }
        ],
    }

    events = parse_order_message(message, symbol="HYPEUSDT")

    assert len(events) == 1
    assert events[0].status == "Filled"
    assert events[0].reduce_only is True
    assert events[0].cumulative_executed_qty == 0.70
    assert events[0].updated_time_ms == 1999


def test_order_store_ignores_duplicate_and_stale_updates() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = EventStore(database)

    assert store.record_order(make_event("New", 1000)) is True
    assert store.record_order(make_event("New", 1000)) is False
    assert len(store.active_bot_orders("HYPEUSDT")) == 1

    assert store.record_order(make_event("Filled", 1100, cum_qty=0.4)) is True
    assert store.record_order(make_event("Cancelled", 1050)) is False
    assert store.active_bot_orders("HYPEUSDT") == []
