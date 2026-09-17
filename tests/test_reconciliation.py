import pytest

from botdca.bybit_events import ExecutionEvent
from botdca.database import Database
from botdca.exchange import PositionSnapshot
from botdca.persistence import EventStore
from botdca.reconciliation import ReconciliationError, reconcile_strategy
from botdca.strategy import DcaStrategy, StrategyConfig


class PositionOnlyExchange:
    def __init__(self, position: PositionSnapshot) -> None:
        self.position = position

    def get_position(self, symbol: str) -> PositionSnapshot:
        return self.position


def store_with_buys(*quantities: float) -> EventStore:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = EventStore(database)
    for index, qty in enumerate(quantities):
        store.record_execution(
            ExecutionEvent(
                symbol="HYPEUSDT",
                order_id=f"order-{index}",
                order_link_id=f"botdca-buy-{index}",
                execution_id=f"exec-{index}",
                side="Buy",
                price=80.0 - index,
                qty=qty,
                fee=0.0,
                realized_pnl=0.0,
                execution_time_ms=1000 + index,
            )
        )
    return store


def test_reconciliation_restores_matching_open_long() -> None:
    store = store_with_buys(0.3, 0.4)
    position = PositionSnapshot(
        symbol="HYPEUSDT",
        side="Buy",
        size=0.7,
        average_entry=79.4,
        leverage=24.0,
        mark_price=79.0,
        liquidation_price=72.0,
        unrealized_pnl=-0.28,
    )
    strategy = DcaStrategy(StrategyConfig())

    result = reconcile_strategy(
        strategy=strategy,
        exchange=PositionOnlyExchange(position),
        store=store,
        symbol="HYPEUSDT",
    )

    assert result.status == "restored"
    assert result.restored_dca_level == 1
    assert strategy.current_cycle is not None
    assert strategy.current_cycle.total_qty == pytest.approx(0.7)
    assert strategy.current_cycle.average_entry == pytest.approx(79.4)
    assert strategy.current_cycle.last_order_qty == pytest.approx(0.4)


def test_reconciliation_refuses_quantity_mismatch() -> None:
    store = store_with_buys(0.3, 0.4)
    position = PositionSnapshot(
        symbol="HYPEUSDT",
        side="Buy",
        size=0.8,
        average_entry=79.4,
        leverage=24.0,
        mark_price=79.0,
        liquidation_price=72.0,
        unrealized_pnl=-0.3,
    )

    with pytest.raises(ReconciliationError, match="does not match"):
        reconcile_strategy(
            strategy=DcaStrategy(StrategyConfig()),
            exchange=PositionOnlyExchange(position),
            store=store,
            symbol="HYPEUSDT",
        )


def test_reconciliation_refuses_short_position() -> None:
    store = store_with_buys(0.3)
    position = PositionSnapshot(
        symbol="HYPEUSDT",
        side="Sell",
        size=0.3,
        average_entry=80.0,
        leverage=24.0,
        mark_price=79.0,
        liquidation_price=85.0,
        unrealized_pnl=0.3,
    )

    with pytest.raises(ReconciliationError, match="unexpected Sell"):
        reconcile_strategy(
            strategy=DcaStrategy(StrategyConfig()),
            exchange=PositionOnlyExchange(position),
            store=store,
            symbol="HYPEUSDT",
        )
