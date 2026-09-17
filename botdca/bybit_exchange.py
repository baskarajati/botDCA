from __future__ import annotations

from typing import Any

from pybit.unified_trading import HTTP

from botdca.exchange import (
    AccountSnapshot,
    LiveTradingDisabled,
    OrderAck,
    PositionSnapshot,
    new_order_link_id,
)


class BybitApiError(RuntimeError):
    pass


def _as_order_number(value: float) -> str:
    if value <= 0:
        raise ValueError("order value must be positive")
    return f"{value:.12f}".rstrip("0").rstrip(".")


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

    def get_account_snapshot(self) -> AccountSnapshot:
        response = self._require_ok(
            self.session.get_wallet_balance(accountType="UNIFIED")
        )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise BybitApiError("Bybit wallet balance response did not contain an account row")
        row = rows[0]
        return AccountSnapshot(
            total_equity_usd=_as_float(row.get("totalEquity")),
            total_wallet_balance_usd=_as_float(row.get("totalWalletBalance")),
            total_margin_balance_usd=_as_float(row.get("totalMarginBalance")),
            total_available_balance_usd=_as_float(row.get("totalAvailableBalance")),
            total_initial_margin_usd=_as_float(row.get("totalInitialMargin")),
            total_maintenance_margin_usd=_as_float(row.get("totalMaintenanceMargin")),
            total_perp_upl_usd=_as_float(row.get("totalPerpUPL")),
            account_im_rate=_as_float(row.get("accountIMRate")),
            account_mm_rate=_as_float(row.get("accountMMRate")),
        )

    def get_last_price(self, symbol: str) -> float:
        symbol = symbol.upper()
        response = self._require_ok(
            self.session.get_tickers(category="linear", symbol=symbol)
        )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise BybitApiError(f"Bybit ticker response did not contain {symbol}")
        price = _as_float(rows[0].get("lastPrice"))
        if price <= 0:
            raise BybitApiError(f"Bybit ticker returned invalid last price for {symbol}")
        return price

    def open_long(self, symbol: str, qty: float) -> OrderAck:
        return self._place_market_long(symbol, qty, prefix="open")

    def add_long(self, symbol: str, qty: float) -> OrderAck:
        return self._place_market_long(symbol, qty, prefix="dca-market")

    def _place_market_long(self, symbol: str, qty: float, *, prefix: str) -> OrderAck:
        self._require_live()
        order_link_id = new_order_link_id(prefix)
        response = self._require_ok(
            self.session.place_order(
                category="linear",
                symbol=symbol.upper(),
                side="Buy",
                orderType="Market",
                qty=_as_order_number(qty),
                positionIdx=0,
                orderLinkId=order_link_id,
            )
        )
        return self._ack(response, order_link_id)

    def place_dca_limit(self, symbol: str, qty: float, price: float) -> OrderAck:
        self._require_live()
        order_link_id = new_order_link_id("dca")
        response = self._require_ok(
            self.session.place_order(
                category="linear",
                symbol=symbol.upper(),
                side="Buy",
                orderType="Limit",
                qty=_as_order_number(qty),
                price=_as_order_number(price),
                timeInForce="GTC",
                positionIdx=0,
                orderLinkId=order_link_id,
            )
        )
        return self._ack(response, order_link_id)

    def place_tp_limit(self, symbol: str, qty: float, price: float) -> OrderAck:
        self._require_live()
        order_link_id = new_order_link_id("tp")
        response = self._require_ok(
            self.session.place_order(
                category="linear",
                symbol=symbol.upper(),
                side="Sell",
                orderType="Limit",
                qty=_as_order_number(qty),
                price=_as_order_number(price),
                timeInForce="GTC",
                positionIdx=0,
                reduceOnly=True,
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
                qty=_as_order_number(qty),
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
