from decimal import Decimal

import pytest

from botdca.instruments import BybitInstrumentClient, InstrumentRules


class FakeInstrumentSession:
    def get_instruments_info(self, **kwargs):
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": kwargs["symbol"],
                        "priceFilter": {"tickSize": "0.01"},
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.01",
                            "minNotionalValue": "5",
                            "maxMktOrderQty": "100",
                        },
                    }
                ]
            },
        }


class FakeInstrumentListSession:
    def get_instruments_info(self, **kwargs):
        assert kwargs["category"] == "linear"
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "status": "Trading",
                    },
                    {
                        "symbol": "ETHUSDC",
                        "contractType": "LinearPerpetual",
                        "status": "Trading",
                    },
                    {
                        "symbol": "OLDUSDT",
                        "contractType": "LinearPerpetual",
                        "status": "Settled",
                    },
                ],
                "nextPageCursor": "",
            },
        }


def test_instrument_rules_are_loaded_from_bybit_metadata() -> None:
    rules = BybitInstrumentClient(FakeInstrumentSession()).get_linear_rules("hypeusdt")

    assert rules.symbol == "HYPEUSDT"
    assert rules.tick_size == Decimal("0.01")
    assert rules.qty_step == Decimal("0.001")
    assert rules.min_order_qty == Decimal("0.01")
    assert rules.min_notional_value == Decimal(5)


def test_only_trading_linear_usdt_perpetuals_are_listed() -> None:
    symbols = BybitInstrumentClient(FakeInstrumentListSession()).list_linear_usdt_symbols()

    assert symbols == ["BTCUSDT"]


def test_quantity_and_prices_use_directional_quantization() -> None:
    rules = InstrumentRules(
        symbol="HYPEUSDT",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.01"),
        min_notional_value=Decimal(5),
        max_market_order_qty=Decimal(100),
    )

    assert rules.floor_qty(0.1239) == Decimal("0.123")
    assert rules.floor_price(78.129) == Decimal("78.12")
    assert rules.ceil_price(78.121) == Decimal("78.13")


def test_quantity_below_minimum_is_rejected() -> None:
    rules = InstrumentRules(
        symbol="SOLUSDT",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.01"),
        min_order_qty=Decimal("0.1"),
        min_notional_value=Decimal(5),
        max_market_order_qty=None,
    )

    with pytest.raises(ValueError, match="below"):
        rules.floor_qty(0.099)
