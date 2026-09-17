from botdca.bybit_stream import BybitPrivateStream
from botdca.database import Database
from botdca.event_processor import ExchangeEventProcessor
from botdca.persistence import EventStore


class FakeWebSocket:
    last_instance = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.callbacks = {}
        self.exited = False
        FakeWebSocket.last_instance = self

    def execution_stream(self, callback):
        self.callbacks["execution"] = callback

    def order_stream(self, callback):
        self.callbacks["order"] = callback

    def position_stream(self, callback):
        self.callbacks["position"] = callback

    def exit(self):
        self.exited = True


def test_private_stream_subscribes_all_reconciliation_topics() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = EventStore(database)
    processor = ExchangeEventProcessor(store=store, symbol="HYPEUSDT")
    stream = BybitPrivateStream(
        api_key="key",
        api_secret="secret",
        testnet=True,
        processor=processor,
        websocket_factory=FakeWebSocket,
    )

    stream.start()
    websocket = FakeWebSocket.last_instance

    assert websocket is not None
    assert websocket.kwargs["channel_type"] == "private"
    assert set(websocket.callbacks) == {"execution", "order", "position"}

    websocket.callbacks["execution"](
        {
            "creationTime": 1000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "HYPEUSDT",
                    "orderId": "order-1",
                    "orderLinkId": "botdca-open-abc",
                    "execId": "exec-1",
                    "side": "Buy",
                    "execPrice": "78",
                    "execQty": "0.3",
                    "execFee": "0.01",
                    "execPnl": "0",
                    "execTime": "999",
                }
            ],
        }
    )
    assert store.execution_count() == 1

    stream.stop()
    assert websocket.exited is True
