from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from pybit.unified_trading import HTTP


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional_value: Decimal
    max_market_order_qty: Decimal | None

    def floor_qty(self, qty: float | Decimal) -> Decimal:
        value = Decimal(str(qty))
        units = (value / self.qty_step).to_integral_value(rounding=ROUND_FLOOR)
        quantized = units * self.qty_step
        if quantized < self.min_order_qty:
            raise ValueError(
                f"quantity {quantized} is below {self.symbol} minimum {self.min_order_qty}"
            )
        if self.max_market_order_qty is not None and quantized > self.max_market_order_qty:
            raise ValueError(
                f"quantity {quantized} exceeds {self.symbol} market maximum "
                f"{self.max_market_order_qty}"
            )
        return quantized

    def floor_price(self, price: float | Decimal) -> Decimal:
        value = Decimal(str(price))
        units = (value / self.tick_size).to_integral_value(rounding=ROUND_FLOOR)
        return units * self.tick_size

    def ceil_price(self, price: float | Decimal) -> Decimal:
        value = Decimal(str(price))
        units = (value / self.tick_size).to_integral_value(rounding=ROUND_CEILING)
        return units * self.tick_size


class BybitInstrumentClient:
    def __init__(self, session: Any | None = None, *, testnet: bool = False) -> None:
        self.session = session or HTTP(testnet=testnet)

    def get_linear_rules(self, symbol: str) -> InstrumentRules:
        symbol = symbol.upper()
        response = self.session.get_instruments_info(category="linear", symbol=symbol)
        if response.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit instrument request failed {response.get('retCode')}: "
                f"{response.get('retMsg', 'unknown')}"
            )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise LookupError(f"Bybit returned no instrument metadata for {symbol}")
        row = rows[0]
        price_filter = row.get("priceFilter", {})
        lot_filter = row.get("lotSizeFilter", {})
        max_market = lot_filter.get("maxMktOrderQty") or lot_filter.get("maxMarketOrderQty")
        return InstrumentRules(
            symbol=symbol,
            tick_size=Decimal(str(price_filter["tickSize"])),
            qty_step=Decimal(str(lot_filter["qtyStep"])),
            min_order_qty=Decimal(str(lot_filter["minOrderQty"])),
            min_notional_value=Decimal(str(lot_filter.get("minNotionalValue") or "0")),
            max_market_order_qty=(
                None if max_market in {None, ""} else Decimal(str(max_market))
            ),
        )

    def list_linear_usdt_symbols(self) -> list[str]:
        symbols: set[str] = set()
        cursor = ""
        for _ in range(10):
            parameters = {"category": "linear", "limit": 1000}
            if cursor:
                parameters["cursor"] = cursor
            response = self.session.get_instruments_info(**parameters)
            if response.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit instrument request failed {response.get('retCode')}: "
                    f"{response.get('retMsg', 'unknown')}"
                )
            result = response.get("result", {})
            for row in result.get("list", []):
                symbol = str(row.get("symbol", "")).upper()
                if (
                    symbol.endswith("USDT")
                    and row.get("contractType") == "LinearPerpetual"
                    and row.get("status") == "Trading"
                ):
                    symbols.add(symbol)
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
        return sorted(symbols)
