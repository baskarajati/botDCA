from decimal import Decimal

from botdca.bybit_events import ExecutionEvent
from botdca.database import Database
from botdca.exchange import AccountSnapshot, OpenOrder, OrderAck, PositionSnapshot
from botdca.instruments import InstrumentRules
from botdca.live_service import LiveStrategyService
from botdca.persistence import EventStore
from botdca.risk import RiskLimits
from botdca.strategy import DcaStrategy, StrategyConfig


class FakeExchange:
    def __init__(self, position: PositionSnapshot, account: AccountSnapshot) -> None:
        self.position = position
        self.account = account
        self.last_price = 80.0
        self.calls: list[tuple] = []
        self.counter = 0
        self.open_orders: list[OpenOrder] = []

    def _ack(self, prefix: str) -> OrderAck:
        self.counter += 1
        return OrderAck(f"{prefix}-{self.counter}", f"botdca-{prefix}-{self.counter}")

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls.append(("set_leverage", symbol, leverage))

    def get_position(self, symbol: str) -> PositionSnapshot:
        self.calls.append(("get_position", symbol))
        return self.position

    def get_account_snapshot(self) -> AccountSnapshot:
        self.calls.append(("get_account_snapshot",))
        return self.account

    def get_last_price(self, symbol: str) -> float:
        self.calls.append(("get_last_price", symbol))
        return self.last_price

    def get_open_orders(self, symbol: str) -> list[OpenOrder]:
        self.calls.append(("get_open_orders", symbol))
        return list(self.open_orders)

    def open_long(
        self, symbol: str, qty: float, *, order_link_id: str | None = None
    ) -> OrderAck:
        self.calls.append(("open_long", symbol, qty))
        ack = self._ack("open")
        return OrderAck(ack.order_id, order_link_id or ack.order_link_id)

    def add_long(self, symbol: str, qty: float) -> OrderAck:
        self.calls.append(("add_long", symbol, qty))
        return self._ack("add")

    def place_dca_limit(
        self,
        symbol: str,
        qty: float,
        price: float,
        *,
        order_link_id: str | None = None,
    ) -> OrderAck:
        self.calls.append(("place_dca_limit", symbol, qty, price))
        ack = self._ack("dca")
        link_id = order_link_id or ack.order_link_id
        self.open_orders.append(
            OpenOrder(ack.order_id, link_id, symbol, "Buy", "Limit", price, qty, False, "New")
        )
        return OrderAck(ack.order_id, link_id)

    def place_tp_limit(
        self,
        symbol: str,
        qty: float,
        price: float,
        *,
        order_link_id: str | None = None,
    ) -> OrderAck:
        self.calls.append(("place_tp_limit", symbol, qty, price))
        ack = self._ack("tp")
        link_id = order_link_id or ack.order_link_id
        self.open_orders.append(
            OpenOrder(ack.order_id, link_id, symbol, "Sell", "Limit", price, qty, True, "New")
        )
        return OrderAck(ack.order_id, link_id)

    def close_long(
        self, symbol: str, qty: float, *, order_link_id: str | None = None
    ) -> OrderAck:
        self.calls.append(("close_long", symbol, qty))
        return self._ack("close")

    def cancel_all(self, symbol: str) -> None:
        self.calls.append(("cancel_all", symbol))

    def cancel_order(self, symbol: str, order_id: str) -> None:
        self.calls.append(("cancel_order", symbol, order_id))
        self.open_orders = [order for order in self.open_orders if order.order_id != order_id]


def _flat() -> PositionSnapshot:
    return PositionSnapshot("HYPEUSDT", "", 0.0, 0.0, 0.0, 80.0, None, 0.0)


def _long() -> PositionSnapshot:
    return PositionSnapshot("HYPEUSDT", "Buy", 0.3, 80.0, 24.0, 79.5, 76.0, -0.15)


def _account(*, equity: float = 150.0, available: float = 120.0) -> AccountSnapshot:
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


def _rules() -> InstrumentRules:
    return InstrumentRules(
        symbol="HYPEUSDT",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.01"),
        min_notional_value=Decimal(5),
        max_market_order_qty=Decimal(100),
    )


def _store() -> EventStore:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    return EventStore(database)


def _service(
    strategy: DcaStrategy,
    exchange: FakeExchange,
    store: EventStore,
    *,
    risk_limits: RiskLimits | None = None,
) -> LiveStrategyService:
    return LiveStrategyService(
        strategy=strategy,
        exchange=exchange,
        store=store,
        rules=_rules(),
        risk_limits=risk_limits or RiskLimits(),
        reentry_delay_seconds=0,
    )


def test_resumed_flat_strategy_submits_initial_entry_without_faking_fill() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    exchange = FakeExchange(_flat(), _account())

    result = _service(strategy, exchange, _store()).sync()

    assert result.status == "entry_submitted"
    assert result.entry_order is not None
    assert strategy.current_cycle is None
    assert ("set_leverage", "HYPEUSDT", 24) in exchange.calls
    assert ("open_long", "HYPEUSDT", 0.3) in exchange.calls


def test_flat_entry_is_blocked_when_account_reserve_would_be_breached() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    exchange = FakeExchange(_flat(), _account(equity=100.0, available=20.5))

    result = _service(strategy, exchange, _store()).sync()

    assert result.status == "entry_blocked"
    assert "reserve floor" in (result.dca_blocked_reason or "")
    assert not any(call[0] == "open_long" for call in exchange.calls)


def test_open_position_waits_when_persisted_fills_have_not_arrived() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    exchange = FakeExchange(_long(), _account())

    result = _service(strategy, exchange, _store()).sync()

    assert result.status == "waiting_for_reconciliation"
    assert not any(call[0] == "cancel_all" for call in exchange.calls)
    assert not any(call[0] == "place_tp_limit" for call in exchange.calls)


def test_open_position_reconciles_then_rebuilds_tp_and_dca() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    store = _store()
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="open-1",
            order_link_id="botdca-open-1",
            execution_id="exec-1",
            side="Buy",
            price=80.0,
            qty=0.3,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=1,
        )
    )
    exchange = FakeExchange(_long(), _account())

    result = _service(strategy, exchange, store).sync()

    assert result.status == "orders_rebuilt"
    assert strategy.current_cycle is not None
    assert strategy.current_cycle.average_entry == 80.0
    assert result.take_profit_order is not None
    assert result.dca_order is not None
    tp_index = next(i for i, call in enumerate(exchange.calls) if call[0] == "place_tp_limit")
    dca_index = next(i for i, call in enumerate(exchange.calls) if call[0] == "place_dca_limit")
    assert tp_index < dca_index
    assert not any(call[0] == "cancel_all" for call in exchange.calls)


def test_paused_open_position_still_rebuilds_protective_orders() -> None:
    strategy = DcaStrategy(StrategyConfig())
    store = _store()
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="open-1",
            order_link_id="botdca-open-1",
            execution_id="exec-1",
            side="Buy",
            price=80.0,
            qty=0.3,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=1,
        )
    )
    exchange = FakeExchange(_long(), _account())

    result = _service(strategy, exchange, store).sync()

    assert result.status == "orders_rebuilt"
    assert strategy.state.value == "paused"
    assert strategy.reentry_enabled is False
    assert result.take_profit_order is not None
    assert result.dca_order is not None


def test_restart_reuses_matching_orders_without_duplicate_submission() -> None:
    store = _store()
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="open-1",
            order_link_id="botdca-open-1",
            execution_id="exec-1",
            side="Buy",
            price=80.0,
            qty=0.3,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=1,
        )
    )
    exchange = FakeExchange(_long(), _account())

    first = _service(DcaStrategy(StrategyConfig()), exchange, store).sync()
    placement_count = sum(1 for call in exchange.calls if call[0].startswith("place_"))
    second = _service(DcaStrategy(StrategyConfig()), exchange, store).sync()

    assert first.status == second.status == "orders_rebuilt"
    assert sum(1 for call in exchange.calls if call[0].startswith("place_")) == placement_count
    assert len(exchange.open_orders) == 2


def test_stale_orders_are_replaced_in_safe_sequence() -> None:
    store = _store()
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="open-1",
            order_link_id="botdca-open-1",
            execution_id="exec-1",
            side="Buy",
            price=80.0,
            qty=0.3,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=1,
        )
    )
    exchange = FakeExchange(_long(), _account())
    exchange.open_orders = [
        OpenOrder(
            "old-tp", "botdca-tp-old", "HYPEUSDT", "Sell", "Limit", 99, 0.3, True, "New"
        ),
        OpenOrder(
            "old-dca", "botdca-dca-old", "HYPEUSDT", "Buy", "Limit", 60, 0.9, False, "New"
        ),
    ]

    _service(DcaStrategy(StrategyConfig()), exchange, store).sync()

    actions = [call[0:3] for call in exchange.calls if call[0] in {"place_tp_limit", "place_dca_limit", "cancel_order"}]
    assert actions[0][0] == "place_tp_limit"
    assert actions[1] == ("cancel_order", "HYPEUSDT", "old-tp")
    assert actions[2] == ("cancel_order", "HYPEUSDT", "old-dca")
    assert actions[3][0] == "place_dca_limit"


def test_partially_filled_dca_is_kept_without_new_entry_liability() -> None:
    store = _store()
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="open-1",
            order_link_id="botdca-open-1",
            execution_id="exec-open",
            side="Buy",
            price=80.0,
            qty=0.3,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=1,
        )
    )
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="dca-1",
            order_link_id="botdca-dca-1",
            execution_id="exec-partial",
            side="Buy",
            price=78.0,
            qty=0.1,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=2,
        )
    )
    position = PositionSnapshot("HYPEUSDT", "Buy", 0.4, 79.5, 24.0, 79.0, 75.0, -0.2)
    exchange = FakeExchange(position, _account())
    exchange.open_orders = [
        OpenOrder(
            "dca-1",
            "botdca-dca-1",
            "HYPEUSDT",
            "Buy",
            "Limit",
            78.0,
            0.4,
            False,
            "PartiallyFilled",
        )
    ]

    result = _service(DcaStrategy(StrategyConfig()), exchange, store).sync()

    assert result.dca_order is not None
    assert result.dca_order.order_id == "dca-1"
    assert not any(call[0] == "place_dca_limit" for call in exchange.calls)
    assert not any(call[0] == "cancel_order" for call in exchange.calls)


# -- protection alert lifecycle ---------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _journaled_service(
    strategy: DcaStrategy, exchange: FakeExchange, store: EventStore, clock: _Clock
) -> LiveStrategyService:
    from botdca.alerts import AlertDispatcher, JournalAlertSink

    return LiveStrategyService(
        strategy=strategy,
        exchange=exchange,
        store=store,
        rules=_rules(),
        risk_limits=RiskLimits(),
        reentry_delay_seconds=0,
        clock=clock,
        alerts=AlertDispatcher([JournalAlertSink(store)]),
    )


def _record_entry_fill(store: EventStore) -> None:
    store.record_execution(
        ExecutionEvent(
            symbol="HYPEUSDT",
            order_id="open-1",
            order_link_id="botdca-open-1",
            execution_id="exec-1",
            side="Buy",
            price=80.0,
            qty=0.3,
            fee=0.0,
            realized_pnl=0.0,
            execution_time_ms=1_789_746_477_792,
        )
    )


def _alert_conditions(store: EventStore) -> list[tuple[str, str]]:
    return [(row["condition"], row["severity"]) for row in store.recent_alerts(["HYPEUSDT"])]


def test_first_take_profit_after_an_entry_is_routine_not_lost_protection() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    store = _store()
    clock = _Clock()
    exchange = FakeExchange(_flat(), _account())
    service = _journaled_service(strategy, exchange, store, clock)

    assert service.sync().status == "entry_submitted"
    _record_entry_fill(store)
    exchange.position = _long()
    clock.now += 2.2

    result = service.sync()

    assert result.take_profit_order is not None
    assert result.protection_status == "protected"
    assert _alert_conditions(store) == []
    assert store.open_alert_conditions(["HYPEUSDT"]) == []


def test_missing_take_profit_after_the_grace_window_is_still_critical() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    store = _store()
    clock = _Clock()
    exchange = FakeExchange(_flat(), _account())
    service = _journaled_service(strategy, exchange, store, clock)

    service.sync()
    _record_entry_fill(store)
    exchange.position = _long()
    clock.now += service.initial_tp_grace_seconds + 1

    service.sync()

    assert ("missing_take_profit", "critical") in _alert_conditions(store)
    assert ("protection_repaired", "warning") in _alert_conditions(store)


def test_missing_take_profit_after_a_restart_is_critical_then_resolves() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    store = _store()
    _record_entry_fill(store)
    exchange = FakeExchange(_long(), _account())
    # A fresh process never submitted this entry, so there is no grace.
    service = _journaled_service(strategy, exchange, store, _Clock())

    service.sync()
    assert store.open_alert_conditions(["HYPEUSDT"]) == ["missing_take_profit"]

    service.sync()  # the TP placed by the repair is now resting and matched

    assert store.open_alert_conditions(["HYPEUSDT"]) == []
    assert ("missing_take_profit", "critical") in _alert_conditions(store)


def test_resizing_a_stale_take_profit_is_not_reported_as_lost_protection() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    store = _store()
    _record_entry_fill(store)
    exchange = FakeExchange(_long(), _account())
    exchange.open_orders.append(
        OpenOrder("tp-old", "botdca-tp-old", "HYPEUSDT", "Sell", "Limit", 81.0, 0.2, True, "New")
    )
    service = _journaled_service(strategy, exchange, store, _Clock())

    result = service.sync()

    assert result.protection_status == "repaired"
    assert _alert_conditions(store) == [("protection_repaired", "info")]
    assert ("cancel_order", "HYPEUSDT", "tp-old") in exchange.calls


def test_a_flat_position_resolves_protection_and_max_dca_alerts() -> None:
    from botdca.alerts import Alert, AlertCondition, AlertSeverity

    strategy = DcaStrategy(StrategyConfig())
    store = _store()
    exchange = FakeExchange(_flat(), _account())
    service = _journaled_service(strategy, exchange, store, _Clock())
    for condition in (
        AlertCondition.MISSING_TAKE_PROFIT,
        AlertCondition.MAX_DCA_REACHED,
        AlertCondition.RECONCILIATION_FAILED,
    ):
        service.alerts.dispatch(
            Alert(condition, AlertSeverity.CRITICAL, "HYPEUSDT", "old", cycle_id="c-old")
        )
    assert len(store.open_alert_conditions(["HYPEUSDT"])) == 3

    assert service.sync().status == "flat_paused"

    assert store.open_alert_conditions(["HYPEUSDT"]) == []


def _closed_basket_service(*, pause_after_cycle: bool):
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    store = _store()
    _record_entry_fill(store)
    exchange = FakeExchange(_long(), _account())
    service = LiveStrategyService(
        strategy=strategy,
        exchange=exchange,
        store=store,
        rules=_rules(),
        risk_limits=RiskLimits(),
        reentry_delay_seconds=0,
        pause_after_cycle=pause_after_cycle,
    )
    service.sync()  # the basket is open and protected
    assert strategy.current_cycle is not None
    exchange.position = _flat()  # the take profit filled
    return strategy, store, service, exchange


def test_trial_pause_after_cycle_stops_re_entry_when_a_basket_closes() -> None:
    from botdca.domain import BotState

    strategy, store, service, exchange = _closed_basket_service(pause_after_cycle=True)

    assert service.sync().status == "flat_paused"
    assert service.sync().status == "flat_paused"

    assert strategy.state == BotState.PAUSED
    assert not any(call[0] == "open_long" for call in exchange.calls)
    events = [row["event_type"] for row in store.recent_events("HYPEUSDT", limit=10)]
    assert events.count("TRIAL_PAUSED_AFTER_CYCLE") == 1


def test_without_pause_after_cycle_the_strategy_re_enters() -> None:
    _strategy, _unused_store, service, exchange = _closed_basket_service(pause_after_cycle=False)

    assert service.sync().status == "entry_submitted"
    assert any(call[0] == "open_long" for call in exchange.calls)


def test_a_dca0_basket_holds_for_its_take_profit_without_manual_intervention() -> None:
    from botdca.domain import BasketStatus

    strategy = DcaStrategy(StrategyConfig(dca_steps=()))
    strategy.resume()
    store = _store()
    _record_entry_fill(store)
    exchange = FakeExchange(_long(), _account())
    service = _journaled_service(strategy, exchange, store, _Clock())

    result = service.sync()

    assert result.status == "take_profit_only"
    assert result.take_profit_order is not None
    assert result.max_dca_reached is False
    assert result.manual_intervention_required is False
    assert not any(call[0] == "place_dca_limit" for call in exchange.calls)
    assert strategy.current_cycle.status == BasketStatus.OPEN
    assert service.exposure().max_dca_reached is False
    assert "max_dca_reached" not in store.open_alert_conditions(["HYPEUSDT"])
