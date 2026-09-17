import pytest

from botdca.bybit_events import ExecutionEvent
from botdca.database import Database
from botdca.persistence import EventStore
from botdca.strategy import DcaStrategy, StrategyConfig


def execution(
    execution_id: str,
    order_id: str,
    side: str,
    price: float,
    qty: float,
    timestamp: int,
) -> ExecutionEvent:
    return ExecutionEvent(
        symbol="HYPEUSDT",
        order_id=order_id,
        order_link_id=f"botdca-{order_id}",
        execution_id=execution_id,
        side=side,
        price=price,
        qty=qty,
        fee=0.0,
        realized_pnl=0.0,
        execution_time_ms=timestamp,
    )


def test_open_cycle_summary_groups_partial_fills_and_resets_after_sell() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    store = EventStore(database)

    store.record_execution(execution("old-buy", "old-order", "Buy", 80.0, 1.0, 100))
    store.record_execution(execution("old-sell", "old-close", "Sell", 81.0, 1.0, 200))
    store.record_execution(execution("e1", "open", "Buy", 79.0, 0.10, 300))
    store.record_execution(execution("e2", "open", "Buy", 79.1, 0.20, 301))
    store.record_execution(execution("e3", "dca-1", "Buy", 78.0, 0.40, 400))

    summary = store.open_cycle_execution_summary("HYPEUSDT")

    assert summary is not None
    assert summary.order_count == 2
    assert summary.dca_level == 1
    assert summary.total_buy_qty == pytest.approx(0.70)
    assert summary.last_order_qty == pytest.approx(0.40)
    assert summary.weighted_average_buy_price == pytest.approx(
        (79.0 * 0.10 + 79.1 * 0.20 + 78.0 * 0.40) / 0.70
    )


def test_restored_cycle_preserves_next_dca_sizing() -> None:
    strategy = DcaStrategy(StrategyConfig())
    cycle = strategy.restore_cycle(
        average_entry=78.5,
        total_qty=1.5,
        dca_level=2,
        last_order_qty=0.40,
        cycle_id="restored-cycle",
    )

    assert cycle.id == "restored-cycle"
    assert cycle.average_entry == pytest.approx(78.5)
    assert cycle.total_qty == pytest.approx(1.5)
    assert cycle.dca_level == 2
    assert strategy.next_dca_qty() == pytest.approx(0.40 * 1.430)

    strategy.apply_dca_fill(fill_price=77.0)

    assert cycle.dca_level == 3
    assert cycle.last_order_qty == pytest.approx(0.40 * 1.430)
    assert cycle.total_qty > 1.5
