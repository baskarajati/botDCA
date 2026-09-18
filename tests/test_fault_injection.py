"""Deterministic fault injection.

The invariant under test throughout:

    REST ACKNOWLEDGEMENT IS NOT PROOF OF FILL.

Exchange state plus persisted executions are authoritative. A strategy must
never infer a position from an acknowledgement, and must reconstruct the same
basket after a restart, a stream outage or a partial fill.
"""

from decimal import Decimal

import pytest

from botdca.alerts import AlertCondition, AlertDispatcher, MemoryAlertSink
from botdca.bybit_events import ExecutionEvent
from botdca.database import Database
from botdca.exchange import AccountSnapshot, OpenOrder, OrderAck, PositionSnapshot
from botdca.instruments import InstrumentRules
from botdca.live_service import LiveStrategyService
from botdca.persistence import EventStore
from botdca.portfolio import PortfolioCoordinator, PortfolioGuards
from botdca.risk import RiskLimits
from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy import DcaStrategy, strategy_config_from_version
from botdca.strategy_version import GREENSYNERGY_RECONSTRUCTED_V1 as GS
from botdca.worker import LiveWorker

SYMBOL = "HYPEUSDT"


class FaultyExchange:
    """Exchange double with injectable faults."""

    def __init__(self, position: PositionSnapshot, account: AccountSnapshot) -> None:
        self.position = position
        self.account = account
        self.last_price = 80.0
        self.open_orders: list[OpenOrder] = []
        self.calls: list[tuple] = []
        self.counter = 0
        self.account_failures = 0
        self.submitted: list[tuple] = []
        self.reject_next_dca = False

    def _next(self, prefix: str) -> tuple[str, str]:
        self.counter += 1
        return f"{prefix}-{self.counter}", f"botdca-{prefix}-{self.counter}"

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls.append(("set_leverage", symbol, leverage))

    def get_position(self, symbol: str) -> PositionSnapshot:
        return self.position

    def get_account_snapshot(self) -> AccountSnapshot:
        if self.account_failures > 0:
            self.account_failures -= 1
            raise ConnectionError("account read timed out")
        return self.account

    def get_last_price(self, symbol: str) -> float:
        return self.last_price

    def get_open_orders(self, symbol: str) -> list[OpenOrder]:
        return list(self.open_orders)

    def open_long(self, symbol, qty, *, order_link_id=None) -> OrderAck:
        order_id, link = self._next("open")
        self.submitted.append(("open_long", symbol, qty))
        return OrderAck(order_id, order_link_id or link)

    def add_long(self, symbol, qty) -> OrderAck:
        order_id, link = self._next("add")
        return OrderAck(order_id, link)

    def place_dca_limit(self, symbol, qty, price, *, order_link_id=None) -> OrderAck:
        if self.reject_next_dca:
            raise RuntimeError("Bybit rejected the order: insufficient balance")
        order_id, link = self._next("dca")
        link = order_link_id or link
        self.submitted.append(("place_dca_limit", symbol, qty, price))
        self.open_orders.append(
            OpenOrder(order_id, link, symbol, "Buy", "Limit", price, qty, False, "New")
        )
        return OrderAck(order_id, link)

    def place_tp_limit(self, symbol, qty, price, *, order_link_id=None) -> OrderAck:
        order_id, link = self._next("tp")
        link = order_link_id or link
        self.submitted.append(("place_tp_limit", symbol, qty, price))
        self.open_orders.append(
            OpenOrder(order_id, link, symbol, "Sell", "Limit", price, qty, True, "New")
        )
        return OrderAck(order_id, link)

    def close_long(self, symbol, qty, *, order_link_id=None) -> OrderAck:
        order_id, link = self._next("close")
        self.submitted.append(("close_long", symbol, qty))
        return OrderAck(order_id, order_link_id or link)

    def cancel_all(self, symbol) -> None:
        self.open_orders.clear()

    def cancel_order(self, symbol, order_id) -> None:
        self.calls.append(("cancel_order", symbol, order_id))
        self.open_orders = [o for o in self.open_orders if o.order_id != order_id]


class FlakyStream:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1
        self.connected = True

    def stop(self) -> None:
        self.stops += 1
        self.connected = False


def _rules() -> InstrumentRules:
    return InstrumentRules(
        symbol=SYMBOL,
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.001"),
        min_notional_value=Decimal(1),
        max_market_order_qty=Decimal(1000),
    )


def _account(*, equity=500.0, available=400.0) -> AccountSnapshot:
    return AccountSnapshot(
        total_equity_usd=equity,
        total_wallet_balance_usd=equity,
        total_margin_balance_usd=equity,
        total_available_balance_usd=available,
        total_initial_margin_usd=equity - available,
        total_maintenance_margin_usd=1.0,
        total_perp_upl_usd=0.0,
        account_im_rate=0.2,
        account_mm_rate=0.01,
    )


def _database() -> Database:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return database


def _strategy(margin: float = 1.0) -> DcaStrategy:
    strategy = DcaStrategy(
        strategy_config_from_version(
            GS,
            symbol=SYMBOL,
            allocation=InitialAllocation(SizingMode.FIXED_MARGIN_USDT, margin),
        )
    )
    strategy.resume()
    return strategy


def _coordinator(services, *, exchange, guards=None) -> PortfolioCoordinator:
    return PortfolioCoordinator(
        guards=guards
        or PortfolioGuards(
            max_total_bot_margin_usdt=10_000.0,
            max_total_bot_notional_usdt=1_000_000.0,
            min_available_equity_ratio=0.0,
            max_simultaneous_deep_baskets=3,
        ),
        account_reader=exchange.get_account_snapshot,
        exposure_reader=lambda: tuple(s.exposure() for s in services),
    )


def _service(strategy, exchange, store, *, alerts=None, coordinator=None, risk=None):
    return LiveStrategyService(
        strategy=strategy,
        exchange=exchange,
        store=store,
        rules=_rules(),
        risk_limits=risk or RiskLimits(max_strategy_margin_usdt=10_000.0),
        reentry_delay_seconds=0,
        alerts=alerts,
        coordinator=coordinator,
    )


def _fill(store, *, exec_id, link, qty, price, side="Buy", ms=1000):
    return store.record_execution(
        ExecutionEvent(
            execution_id=exec_id,
            order_id=link,
            order_link_id=link,
            symbol=SYMBOL,
            side=side,
            price=price,
            qty=qty,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=ms,
        )
    )


def _alerts() -> tuple[AlertDispatcher, MemoryAlertSink]:
    sink = MemoryAlertSink()
    return AlertDispatcher([sink], cooldown_seconds=0.0), sink


def _conditions(sink: MemoryAlertSink) -> set[str]:
    return {alert.condition for alert in sink.alerts}


# 1 -----------------------------------------------------------------------
def test_rest_timeout_after_bybit_accepted_the_order_is_not_treated_as_a_fill() -> None:
    """The acknowledgement never arrived, but the exchange did accept the entry."""
    store = EventStore(_database())
    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "", 0.0, 0.0, 0.0, 80.0, None, 0.0), _account()
    )

    class TimeoutOnOpen(FaultyExchange):
        def open_long(self, symbol, qty, *, order_link_id=None):
            raise TimeoutError("read timed out")

    timing_out = TimeoutOnOpen(exchange.position, exchange.account)
    with pytest.raises(TimeoutError):
        _service(strategy, timing_out, store).sync()

    # No basket was invented from a request that never returned.
    assert strategy.current_cycle is None

    # The order actually rested and filled. Exchange truth plus the persisted
    # execution is what restores the basket on the next pass.
    _fill(store, exec_id="e1", link="botdca-open-1", qty=0.3, price=80.0)
    filled = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.3, 80.0, 24.0, 80.0, 70.0, 0.0), _account()
    )
    result = _service(strategy, filled, store).sync()

    assert result.status == "orders_rebuilt"
    assert strategy.current_cycle is not None
    assert strategy.current_cycle.total_qty == pytest.approx(0.3)


# 2 -----------------------------------------------------------------------
def test_execution_during_a_websocket_outage_is_recovered_before_reconciling() -> None:
    store = EventStore(_database())
    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.3, 80.0, 24.0, 80.0, 70.0, 0.0), _account()
    )
    stream = FlakyStream(connected=False)
    alerts, sink = _alerts()
    recovered: list[str] = []

    def recovery() -> None:
        # The fill that the stream missed is fetched from execution history.
        _fill(store, exec_id="missed", link="botdca-open-1", qty=0.3, price=80.0)
        recovered.append("done")

    worker = LiveWorker(
        service=_service(strategy, exchange, store, alerts=alerts),
        stream=stream,
        store=store,
        execution_recovery=recovery,
        alerts=alerts,
    )
    result = worker.run_once()

    assert recovered == ["done"], "recovery must run before reconciliation"
    assert stream.starts == 1
    assert result.status == "orders_rebuilt"
    assert AlertCondition.PRIVATE_STREAM_DISCONNECTED in _conditions(sink)


# 3 -----------------------------------------------------------------------
def test_partial_dca_fill_then_restart_rebuilds_the_basket_from_exchange_truth() -> None:
    database = _database()
    store = EventStore(database)
    _fill(store, exec_id="e1", link="botdca-open-1", qty=0.30, price=80.0, ms=1000)
    # The DCA filled only partially before the process died.
    _fill(store, exec_id="e2", link="botdca-dca-1", qty=0.20, price=79.0, ms=2000)

    restarted = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.50, 79.6, 24.0, 79.5, 70.0, -0.05), _account()
    )
    result = _service(restarted, exchange, EventStore(database)).sync()

    assert result.status == "orders_rebuilt"
    cycle = restarted.current_cycle
    assert cycle is not None
    assert cycle.total_qty == pytest.approx(0.50)
    assert cycle.dca_level == 1
    # The next ladder step continues from the partial quantity actually filled.
    assert restarted.next_dca_qty() == pytest.approx(0.20 * 1.42)


# 4 -----------------------------------------------------------------------
def test_partial_tp_fill_leaves_the_remaining_position_protected() -> None:
    database = _database()
    store = EventStore(database)
    _fill(store, exec_id="e1", link="botdca-open-1", qty=0.30, price=80.0, ms=1000)
    _fill(store, exec_id="e2", link="botdca-tp-1", qty=0.10, price=81.0, side="Sell", ms=2000)

    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.20, 80.0, 24.0, 80.5, 70.0, 0.1), _account()
    )
    result = _service(strategy, exchange, EventStore(database)).sync()

    assert result.status == "orders_rebuilt"
    assert strategy.current_cycle.total_qty == pytest.approx(0.20)
    # A fresh TP is placed for the residual size, never for the original size.
    tp_calls = [c for c in exchange.submitted if c[0] == "place_tp_limit"]
    assert tp_calls and tp_calls[-1][2] == pytest.approx(0.20)


# 5 -----------------------------------------------------------------------
def test_duplicate_execution_messages_are_persisted_exactly_once() -> None:
    store = EventStore(_database())
    assert _fill(store, exec_id="dup", link="botdca-open-1", qty=0.3, price=80.0) is True
    assert _fill(store, exec_id="dup", link="botdca-open-1", qty=0.3, price=80.0) is False

    summary = store.open_cycle_execution_summary(SYMBOL)
    assert summary.total_buy_qty == pytest.approx(0.3)
    assert summary.order_count == 1


# 6 -----------------------------------------------------------------------
def test_stale_open_orders_are_replaced_protection_first() -> None:
    database = _database()
    store = EventStore(database)
    _fill(store, exec_id="e1", link="botdca-open-1", qty=0.30, price=80.0)

    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.30, 80.0, 24.0, 79.5, 70.0, -0.1), _account()
    )
    # Resting orders from a previous, now-wrong plan.
    exchange.open_orders = [
        OpenOrder("old-tp", "botdca-tp-old", SYMBOL, "Sell", "Limit", 999.0, 0.30, True, "New"),
        OpenOrder("old-dca", "botdca-dca-old", SYMBOL, "Buy", "Limit", 1.0, 9.99, False, "New"),
    ]
    _service(strategy, exchange, EventStore(database)).sync()

    order_of_calls = [c[0] for c in exchange.submitted] + [
        c[0] for c in exchange.calls if c[0] == "cancel_order"
    ]
    cancelled = {c[2] for c in exchange.calls if c[0] == "cancel_order"}
    assert "old-tp" in cancelled and "old-dca" in cancelled
    # The replacement TP is submitted before the stale one is withdrawn.
    assert order_of_calls.index("place_tp_limit") < order_of_calls.index("cancel_order")


# 7 -----------------------------------------------------------------------
def test_a_temporary_account_read_failure_never_submits_an_order() -> None:
    store = EventStore(_database())
    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "", 0.0, 0.0, 0.0, 80.0, None, 0.0), _account()
    )
    exchange.account_failures = 1

    with pytest.raises(ConnectionError):
        _service(strategy, exchange, store).sync()
    assert exchange.submitted == []

    # The next pass, once the account reads again, proceeds normally.
    result = _service(strategy, exchange, store).sync()
    assert result.status == "entry_submitted"


# 8 -----------------------------------------------------------------------
def test_one_failing_worker_does_not_stop_the_other_symbols() -> None:
    store = EventStore(_database())
    alerts, _sink = _alerts()

    healthy_strategy = _strategy()
    healthy_exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "", 0.0, 0.0, 0.0, 80.0, None, 0.0), _account()
    )
    healthy = LiveWorker(
        service=_service(healthy_strategy, healthy_exchange, store, alerts=alerts),
        stream=FlakyStream(),
        store=store,
        alerts=alerts,
    )

    broken_strategy = _strategy()
    broken_exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "", 0.0, 0.0, 0.0, 80.0, None, 0.0), _account()
    )
    broken_exchange.account_failures = 99
    broken = LiveWorker(
        service=_service(broken_strategy, broken_exchange, store, alerts=alerts),
        stream=FlakyStream(),
        store=store,
        alerts=alerts,
    )

    with pytest.raises(ConnectionError):
        broken.run_once()
    assert healthy.run_once().status == "entry_submitted"
    assert healthy.last_error is None


# 9 -----------------------------------------------------------------------
def test_a_temporary_database_failure_pauses_reentry_and_alerts() -> None:
    class BrokenStore(EventStore):
        def open_cycle_execution_summary(self, symbol):
            raise RuntimeError("database connection lost")

    alerts, sink = _alerts()
    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.3, 80.0, 24.0, 79.5, 70.0, -0.1), _account()
    )
    service = _service(strategy, exchange, BrokenStore(_database()), alerts=alerts)
    worker = LiveWorker(
        service=service, stream=FlakyStream(), store=EventStore(_database()), alerts=alerts
    )

    # A journal failure must surface, never be silently swallowed.
    with pytest.raises(RuntimeError, match="database connection lost"):
        worker.run_once()
    assert exchange.submitted == []

    # The daemon loop's safety boundary then fails closed: one iteration only.
    def failing_once():
        worker._stop.set()
        raise RuntimeError("database connection lost")

    worker.run_once = failing_once
    worker._stop.clear()
    worker._run()

    assert strategy.reentry_enabled is False
    assert worker.last_error is not None
    assert AlertCondition.WORKER_CRASHED in _conditions(sink)


# 10 ----------------------------------------------------------------------
def test_portfolio_risk_denies_a_dca_while_the_take_profit_stays_active() -> None:
    database = _database()
    store = EventStore(database)
    _fill(store, exec_id="e1", link="botdca-open-1", qty=0.30, price=80.0)

    alerts, sink = _alerts()
    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.30, 80.0, 24.0, 79.5, 70.0, -0.1),
        _account(equity=100.0, available=0.05),
    )
    services: list = []
    coordinator = _coordinator(
        services,
        exchange=exchange,
        guards=PortfolioGuards(
            max_total_bot_margin_usdt=10_000.0,
            max_total_bot_notional_usdt=1_000_000.0,
            min_available_balance_usdt=1.0,
            min_available_equity_ratio=0.0,
        ),
    )
    service = _service(
        strategy, exchange, EventStore(database), alerts=alerts, coordinator=coordinator
    )
    services.append(service)
    result = service.sync()

    assert result.dca_order is None
    assert result.dca_blocked_reason is not None
    assert result.portfolio_decision["guard"] == "min_available_balance"
    # Exits must remain possible when new risk is blocked.
    assert result.take_profit_order is not None
    assert AlertCondition.RISK_BLOCKED_DCA in _conditions(sink)


# 11 ----------------------------------------------------------------------
def test_a_max_dca_basket_survives_restart_and_never_invents_dca9() -> None:
    database = _database()
    store = EventStore(database)
    qty = 0.30
    total = 0.0
    notional = 0.0
    for level in range(9):  # initial + DCA1..DCA8
        price = 80.0 - level
        _fill(
            store,
            exec_id=f"e{level}",
            link=f"botdca-{'open' if level == 0 else 'dca'}-{level}",
            qty=qty,
            price=price,
            ms=1000 + level,
        )
        total += qty
        notional += qty * price
        qty *= 1.42

    average = notional / total
    alerts, sink = _alerts()
    restarted = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", total, average, 24.0, average * 0.95, 60.0, -5.0),
        _account(equity=10_000.0, available=9_000.0),
    )
    result = _service(restarted, exchange, EventStore(database), alerts=alerts).sync()

    assert result.status == "max_dca_reached"
    assert result.max_dca_reached is True
    assert result.manual_intervention_required is True
    cycle = restarted.current_cycle
    assert cycle.dca_level == 8
    assert cycle.at_max_dca

    # No DCA9 is invented, but the take profit is still installed.
    assert result.take_profit_order is not None
    assert not any(c[0] == "place_dca_limit" for c in exchange.submitted)
    assert restarted.next_dca_trigger_price() is None
    assert AlertCondition.MAX_DCA_REACHED in _conditions(sink)


# 12 ----------------------------------------------------------------------
def test_manual_close_while_a_stale_dca_rests_cancels_the_entry_liability_first() -> None:
    from botdca.controller import TradingController

    database = _database()
    store = EventStore(database)
    _fill(store, exec_id="e1", link="botdca-open-1", qty=0.30, price=80.0)

    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.30, 80.0, 24.0, 79.5, 70.0, -0.1), _account()
    )
    exchange.open_orders = [
        OpenOrder("stale", "botdca-dca-stale", SYMBOL, "Buy", "Limit", 70.0, 9.0, False, "New")
    ]
    controller = TradingController(strategy=strategy, exchange=exchange, symbol=SYMBOL)
    controller.manual_close_and_pause()

    assert exchange.open_orders == []
    assert any(c[0] == "close_long" for c in exchange.submitted)
    assert strategy.reentry_enabled is False


# 13 ----------------------------------------------------------------------
def test_manual_close_works_while_the_private_stream_is_disconnected() -> None:
    from botdca.controller import TradingController

    strategy = _strategy()
    exchange = FaultyExchange(
        PositionSnapshot(SYMBOL, "Buy", 0.30, 80.0, 24.0, 79.5, 70.0, -0.1), _account()
    )
    stream = FlakyStream(connected=False)

    controller = TradingController(strategy=strategy, exchange=exchange, symbol=SYMBOL)
    result = controller.manual_close_and_pause()

    # A disconnected stream must never block a reduce-only exit.
    assert stream.connected is False
    assert any(c[0] == "close_long" for c in exchange.submitted)
    assert strategy.reentry_enabled is False
    assert result is not None
