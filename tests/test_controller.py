import pytest

from botdca.controller import ControllerSafetyError, TradingController
from botdca.exchange import OrderAck, PositionSnapshot
from botdca.strategy import DcaStrategy, StrategyConfig


class FakeExchange:
    def __init__(self, position: PositionSnapshot) -> None:
        self.position = position
        self.calls: list[tuple] = []

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self.calls.append(("set_leverage", symbol, leverage))

    def get_position(self, symbol: str) -> PositionSnapshot:
        self.calls.append(("get_position", symbol))
        return self.position

    def open_long(self, symbol: str, qty: float) -> OrderAck:
        self.calls.append(("open_long", symbol, qty))
        return OrderAck("open", "link-open")

    def add_long(self, symbol: str, qty: float) -> OrderAck:
        self.calls.append(("add_long", symbol, qty))
        return OrderAck("add", "link-add")

    def place_dca_limit(self, symbol: str, qty: float, price: float) -> OrderAck:
        self.calls.append(("place_dca_limit", symbol, qty, price))
        return OrderAck("dca", "link-dca")

    def place_tp_limit(self, symbol: str, qty: float, price: float) -> OrderAck:
        self.calls.append(("place_tp_limit", symbol, qty, price))
        return OrderAck("tp", "link-tp")

    def close_long(self, symbol: str, qty: float) -> OrderAck:
        self.calls.append(("close_long", symbol, qty))
        return OrderAck("close", "link-close")

    def cancel_all(self, symbol: str) -> None:
        self.calls.append(("cancel_all", symbol))


def _strategy() -> DcaStrategy:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    strategy.begin_cycle(80.0)
    return strategy


def _position(side: str = "Buy", size: float = 0.3) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="HYPEUSDT",
        side=side,
        size=size,
        average_entry=80.0 if size else 0.0,
        leverage=24.0 if size else 0.0,
        mark_price=79.0 if size else 0.0,
        liquidation_price=76.0 if size else None,
        unrealized_pnl=-0.3 if size else 0.0,
    )


def test_manual_close_pauses_cancels_then_submits_reduce_close() -> None:
    strategy = _strategy()
    exchange = FakeExchange(_position())
    controller = TradingController(strategy=strategy, exchange=exchange, symbol="HYPEUSDT")

    result = controller.manual_close_and_pause()

    assert strategy.state.value == "paused"
    assert strategy.current_cycle is not None
    assert result.status == "close_submitted"
    assert result.close_order is not None
    assert exchange.calls == [
        ("cancel_all", "HYPEUSDT"),
        ("get_position", "HYPEUSDT"),
        ("close_long", "HYPEUSDT", 0.3),
    ]


def test_manual_close_clears_local_cycle_only_when_exchange_is_already_flat() -> None:
    strategy = _strategy()
    exchange = FakeExchange(_position(side="", size=0.0))
    controller = TradingController(strategy=strategy, exchange=exchange, symbol="HYPEUSDT")

    result = controller.manual_close_and_pause()

    assert result.status == "already_flat"
    assert result.close_order is None
    assert strategy.current_cycle is None
    assert strategy.state.value == "paused"
    assert exchange.calls == [
        ("cancel_all", "HYPEUSDT"),
        ("get_position", "HYPEUSDT"),
    ]


def test_manual_close_refuses_unexpected_short_and_stays_paused() -> None:
    strategy = _strategy()
    exchange = FakeExchange(_position(side="Sell", size=0.3))
    controller = TradingController(strategy=strategy, exchange=exchange, symbol="HYPEUSDT")

    with pytest.raises(ControllerSafetyError):
        controller.manual_close_and_pause()

    assert strategy.state.value == "paused"
    assert strategy.current_cycle is not None
    assert not any(call[0] == "close_long" for call in exchange.calls)
