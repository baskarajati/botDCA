import pytest
from pybit.exceptions import InvalidRequestError

from botdca.bybit_exchange import BybitApiError, BybitExchange


def test_already_set_leverage_response_is_idempotent():
    class Session:
        def set_leverage(self, **kwargs):
            return {"retCode": 110043, "retMsg": "Set leverage has not been modified."}

    BybitExchange(session=Session(), live_trading=True).set_leverage("HYPEUSDT", 24)


def test_already_set_leverage_sdk_exception_is_idempotent():
    class Session:
        def set_leverage(self, **kwargs):
            raise InvalidRequestError("test", "not modified", 110043, "now", None)

    BybitExchange(session=Session(), live_trading=True).set_leverage("HYPEUSDT", 24)


def test_hedge_mode_is_not_reported_flat():
    class Session:
        def get_positions(self, **kwargs):
            return {
                "retCode": 0,
                "result": {"list": [{"positionIdx": 1, "size": "1", "side": "Buy"}]},
            }

    with pytest.raises(BybitApiError, match="one-way"):
        BybitExchange(session=Session()).get_position("HYPEUSDT")
