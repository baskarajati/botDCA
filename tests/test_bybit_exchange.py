from __future__ import annotations

import pytest
from pybit.exceptions import InvalidRequestError

from botdca.bybit_events import parse_execution_message, parse_position_message
from botdca.bybit_exchange import BybitApiError, BybitExchange
from botdca.exchange import LiveTradingDisabled, PositionAlreadyClosedError


class FakeSession:
    def __init__(self) -> None:
        self.place_calls: list[dict] = []
        self.leverage_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"orderId": "order-1", "orderLinkId": kwargs["orderLinkId"]},
        }

    def set_leverage(self, **kwargs):
        self.leverage_calls.append(kwargs)
        return {"retCode": 0, "retMsg": "OK", "result": {}}

    def cancel_all_orders(self, **kwargs):
        self.cancel_calls.append(kwargs)
        return {"retCode": 0, "retMsg": "OK", "result": {}}

    def get_positions(self, **kwargs):
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "positionIdx": 0,
                        "symbol": kwargs["symbol"],
                        "side": "Buy",
                        "size": "2.5",
                        "avgPrice": "78.2",
                        "leverage": "24",
                        "markPrice": "78.6",
                        "liqPrice": "72.1",
                        "unrealisedPnl": "1.0",
                    }
                ]
            },
        }

    def get_wallet_balance(self, **kwargs):
        assert kwargs == {"accountType": "UNIFIED"}
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "totalEquity": "150.25",
                        "totalWalletBalance": "151.00",
                        "totalMarginBalance": "149.50",
                        "totalAvailableBalance": "112.75",
                        "totalInitialMargin": "36.75",
                        "totalMaintenanceMargin": "2.25",
                        "totalPerpUPL": "-1.50",
                        "accountIMRate": "0.245",
                        "accountMMRate": "0.015",
                    }
                ]
            },
        }

    def get_api_key_information(self):
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "readOnly": 0,
                "uta": 1,
                "ips": ["203.0.113.10"],
                "permissions": {
                    "ContractTrade": ["Order", "Position"],
                    "Wallet": [],
                },
            },
        }


def test_live_guard_blocks_mutating_orders() -> None:
    session = FakeSession()
    exchange = BybitExchange(session=session, live_trading=False)

    with pytest.raises(LiveTradingDisabled):
        exchange.open_long("HYPEUSDT", 1.0)
    with pytest.raises(LiveTradingDisabled):
        exchange.close_long("HYPEUSDT", 1.0)
    with pytest.raises(LiveTradingDisabled):
        exchange.set_leverage("HYPEUSDT", 24)

    assert session.place_calls == []


def test_open_and_close_order_shapes() -> None:
    session = FakeSession()
    exchange = BybitExchange(session=session, live_trading=True)

    open_ack = exchange.open_long("hypeusdt", 0.1234)
    close_ack = exchange.close_long("hypeusdt", 0.1234)

    assert open_ack.accepted
    assert close_ack.accepted
    assert session.place_calls[0]["side"] == "Buy"
    assert "reduceOnly" not in session.place_calls[0]
    assert session.place_calls[0]["positionIdx"] == 0
    assert session.place_calls[1]["side"] == "Sell"
    assert session.place_calls[1]["reduceOnly"] is True
    assert session.place_calls[1]["positionIdx"] == 0


def test_position_snapshot_comes_from_exchange() -> None:
    exchange = BybitExchange(session=FakeSession(), live_trading=False)
    position = exchange.get_position("hypeusdt")

    assert position.symbol == "HYPEUSDT"
    assert position.side == "Buy"
    assert position.size == 2.5
    assert position.average_entry == 78.2
    assert position.leverage == 24.0
    assert position.liquidation_price == 72.1
    assert position.is_open


def test_account_snapshot_comes_from_unified_wallet() -> None:
    exchange = BybitExchange(session=FakeSession(), live_trading=False)

    account = exchange.get_account_snapshot()

    assert account.total_equity_usd == 150.25
    assert account.total_available_balance_usd == 112.75
    assert account.total_initial_margin_usd == 36.75
    assert account.total_maintenance_margin_usd == 2.25
    assert account.total_perp_upl_usd == -1.5
    assert account.account_im_rate == 0.245
    assert account.account_mm_rate == 0.015


def test_api_key_information_comes_from_exchange() -> None:
    exchange = BybitExchange(session=FakeSession(), live_trading=False)

    info = exchange.get_api_key_information()

    assert info["readOnly"] == 0
    assert info["permissions"]["ContractTrade"] == ["Order", "Position"]
    assert "apiSecret" not in info


def test_execution_message_supports_multiple_fills_and_symbol_filter() -> None:
    message = {
        "creationTime": 1000,
        "data": [
            {
                "category": "linear",
                "symbol": "HYPEUSDT",
                "orderId": "o1",
                "orderLinkId": "botdca-open-1",
                "execId": "e1",
                "side": "Buy",
                "execPrice": "78.1",
                "execQty": "0.1",
                "execFee": "0.004",
                "execPnl": "0",
                "execTime": "999",
            },
            {
                "category": "linear",
                "symbol": "SOLUSDT",
                "orderId": "o2",
                "execId": "e2",
                "side": "Buy",
                "execPrice": "200",
                "execQty": "0.1",
            },
        ],
    }

    events = parse_execution_message(message, symbol="HYPEUSDT")

    assert len(events) == 1
    assert events[0].execution_id == "e1"
    assert events[0].price == 78.1
    assert events[0].qty == 0.1
    assert events[0].execution_time_ms == 999


def test_position_message_parses_flat_position() -> None:
    message = {
        "creationTime": 1234,
        "data": [
            {
                "category": "linear",
                "symbol": "HYPEUSDT",
                "positionIdx": 0,
                "side": "",
                "size": "0",
                "entryPrice": "0",
                "leverage": "24",
                "markPrice": "79.0",
                "liqPrice": "",
                "unrealisedPnl": "0",
            }
        ],
    }

    events = parse_position_message(message, symbol="HYPEUSDT")

    assert len(events) == 1
    assert events[0].size == 0.0
    assert events[0].liquidation_price is None
    assert events[0].creation_time_ms == 1234


class UncertainSubmitSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.accepted: dict | None = None

    def get_open_orders(self, **kwargs):
        rows = []
        if self.accepted is not None and self.accepted["orderLinkId"] == kwargs.get("orderLinkId"):
            rows = [self.accepted]
        return {"retCode": 0, "retMsg": "OK", "result": {"list": rows}}

    def get_order_history(self, **kwargs):
        return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        self.accepted = {
            **kwargs,
            "orderId": "accepted-before-timeout",
            "orderStatus": "New",
        }
        raise TimeoutError("response lost after exchange acceptance")


def test_uncertain_submit_recovers_by_stable_order_link_id() -> None:
    session = UncertainSubmitSession()
    exchange = BybitExchange(session=session, live_trading=True)

    first = exchange.open_long(
        "HYPEUSDT",
        0.3,
        order_link_id="botdca-open-stable",
    )
    second = exchange.open_long(
        "HYPEUSDT",
        0.3,
        order_link_id="botdca-open-stable",
    )

    assert first == second
    assert first.order_id == "accepted-before-timeout"
    assert len(session.place_calls) == 1


class PositionZeroSession(UncertainSubmitSession):
    """Bybit raises for a reduce-only order against a position that just closed."""

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        raise InvalidRequestError(
            request="POST /v5/order/create",
            message="current position is zero, cannot fix reduce-only order qty",
            status_code=110017,
            time="17:29:18",
            resp_headers={},
        )


class PositionZeroCodeSession(UncertainSubmitSession):
    """Bybit answers 200 and reports the same refusal in retCode."""

    def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {
            "retCode": 110017,
            "retMsg": "current position is zero, cannot fix reduce-only order qty",
            "result": {},
        }


def test_a_reduce_only_rejection_for_a_flat_position_is_its_own_error() -> None:
    exchange = BybitExchange(session=PositionZeroSession(), live_trading=True)

    with pytest.raises(PositionAlreadyClosedError):
        exchange.place_tp_limit("HYPEUSDT", 0.06, 92.19, order_link_id="botdca-tp-race")


def test_a_reduce_only_refusal_reported_in_retcode_is_the_same_error() -> None:
    exchange = BybitExchange(session=PositionZeroCodeSession(), live_trading=True)

    with pytest.raises(PositionAlreadyClosedError):
        exchange.place_tp_limit("HYPEUSDT", 0.06, 92.19, order_link_id="botdca-tp-code")


def test_another_rejection_keeps_its_own_type() -> None:
    class InsufficientBalanceSession(UncertainSubmitSession):
        def place_order(self, **kwargs):
            self.place_calls.append(kwargs)
            return {"retCode": 110007, "retMsg": "insufficient available balance", "result": {}}

    exchange = BybitExchange(session=InsufficientBalanceSession(), live_trading=True)

    with pytest.raises(BybitApiError):
        exchange.place_tp_limit("HYPEUSDT", 0.06, 92.19, order_link_id="botdca-tp-poor")
