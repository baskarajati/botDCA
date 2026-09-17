from __future__ import annotations

from typing import Any

from pybit.unified_trading import HTTP

from botdca.exchange import LiveTradingDisabled, OrderAck, PositionSnapshot, new_order_link_id


class BybitApiError(RuntimeError):
    pass


def _as_order_qty(qty: float) -> str:
    if qty <= 0:
        raise ValueError("qty must be positive")
    return f"{qty:.12f}".rstrip("0").rstrip(".")


def _as_float(value: Any, default: float = 0.0) -> float:
    if value in {None, ""}:
        return default
    return float(value)


class BybitExchange:
    """Authenticated Bybit V5 linear-perpetual adapter.

    REST order responses are acknowledgements only. This adapter deliberately
    does not claim an order is filled; fills must be reconciled from private
    execution/position events.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        live_trading: bool = False,
        session: Any | None = None,
    ) -> None:
        if session is None and (not api_key or not api_secret):
            raise ValueError("Bybit API credentials are required")
        self.live_trading = live_trading
        self.session = session or HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
        )

    def _require_live(self) -> None:
        if not self.live_trading:
            raise LiveTradingDisabled("BOT_LIVE_TRADING is false")

    @staticmethod
    def _require_ok(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("retCode") != 0:
            raise BybitApiError(
                f"Bybit API error {response.get('retCode')}: {response.get('retMsg', 'unknown')}"
            )
        return response

    @staticmethod
    def _ack(response: dict[str, Any], order_link_id: str) -> OrderAck:
        result = response.get("result", {})
        return OrderAck(
            order_id=str(result.get("orderId", "")),
            order_link_id=str(result.get("orderLinkId") or order_link_id),
            accepted=True,
        )

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._require_live()
        if leverage <= 0:
            raise ValueError("leverage must be positive")
        self._require_ok(
            self.session.set_leverage(
                category="linear",
                symbol=symbol.upper(),
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        )

    def get_position(self, symbol: str) -> PositionSnapshot:
        symbol = symbol.upper()
        response = self._require_ok(
            self.session.get_positions(category="linear", symbol=symbol)
        )
        rows = response.get("result", {}).get("list", [])
        row = next((item for item in rows if int(item.get("positionIdx", 0)) == 0), None)
        if row is None:
            return PositionSnapshot(symbol, "", 0.0, 0.0, 0.0, 0.0, None, 0.0)

        liquidation = row.get("liqPrice")
        return PositionSnapshot(
            symbol=symbol,
            side=str(row.get("side", "")),
            size=_as_float(row.get("size")),
            average_entry=_as_float(row.get("avgPrice")),
            leverage=_as_float(row.get("leverage")),
            mark_price=_as_float(row.get("markPrice")),
            liquidation_price=None if liquidation in {None, ""} else float(liquidation),
            unrealized_pnl=_as_float(row.get("unrealisedPnl")),
        )

    def open_long(self, symbol: str, qty: float) -> OrderAck:
        return self._place_long(symbol, qty, prefix="open")

    def add_long(self, symbol: str, qty: float) -> OrderAck:
        return self._place_long(symbol, qty, prefix="dca")

    def _place_long(self, symbol: str, qty: float, *, prefix: str) -> OrderAck:
        self._require_live()
        order_link_id = new_order_link_id(prefix)
        response = self._require_ok(
            self.session.place_order(
                category="linear",
                symbol=symbol.upper(),
                side="Buy",
                orderType="Market",
                qty=_as_order_qty(qty),
                positionIdx=0,
                orderLinkId=order_link_id,
            )
        )
        return self._ack(response, order_link_id)

    def close_long(self, symbol: str, qty: float) -> OrderAck:
        self._require_live()
        order_link_id = new_order_link_id("close")
        response = self._require_ok(
            self.session.place_order(
                category="linear",
                symbol=symbol.upper(),
                side="Sell",
                orderType="Market",
                qty=_as_order_qty(qty),
                positionIdx=0,
                reduceOnly=True,
                orderLinkId=order_link_id,
            )
        )
        return self._ack(response, order_link_id)

    def cancel_all(self, symbol: str) -> None:
        self._require_live()
        self._require_ok(
            self.session.cancel_all_orders(category="linear", symbol=symbol.upper())
        )
