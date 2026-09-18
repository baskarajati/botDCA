from __future__ import annotations

from typing import Any

from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

from botdca.exchange import (
    AccountSnapshot,
    LiveTradingDisabled,
    OpenOrder,
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

    @staticmethod
    def _row_ack(row: dict[str, Any], order_link_id: str) -> OrderAck:
        return OrderAck(
            order_id=str(row.get("orderId", "")),
            order_link_id=str(row.get("orderLinkId") or order_link_id),
            accepted=True,
        )

    def _find_order(self, symbol: str, order_link_id: str) -> dict[str, Any] | None:
        for method_name in ("get_open_orders", "get_order_history"):
            method = getattr(self.session, method_name, None)
            if method is None:
                continue
            response = self._require_ok(
                method(
                    category="linear",
                    symbol=symbol.upper(),
                    orderLinkId=order_link_id,
                    limit=1,
                )
            )
            rows = response.get("result", {}).get("list", [])
            if rows:
                return rows[0]
        return None

    @staticmethod
    def _retry_link_id(base: str, attempt: int) -> str:
        if attempt == 0:
            return base
        suffix = f"-r{attempt}"
        return f"{base[: 36 - len(suffix)]}{suffix}"

    def _place_idempotent(
        self,
        *,
        symbol: str,
        order_link_id: str,
        request: dict[str, Any],
    ) -> OrderAck:
        active_or_filled = {"New", "PartiallyFilled", "Untriggered", "Filled"}
        for attempt in range(10):
            candidate = self._retry_link_id(order_link_id, attempt)
            existing = self._find_order(symbol, candidate)
            if existing is not None:
                if str(existing.get("orderStatus", "")) in active_or_filled:
                    return self._row_ack(existing, candidate)
                continue

            try:
                response = self._require_ok(
                    self.session.place_order(**request, orderLinkId=candidate)
                )
            except Exception:
                # A timeout/disconnect can happen after Bybit accepted the order.
                recovered = self._find_order(symbol, candidate)
                if (
                    recovered is not None
                    and str(recovered.get("orderStatus", "")) in active_or_filled
                ):
                    return self._row_ack(recovered, candidate)
                raise
            return self._ack(response, candidate)
        raise BybitApiError(f"exhausted idempotent order retries for {order_link_id}")

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._require_live()
        if leverage <= 0:
            raise ValueError("leverage must be positive")
        try:
            response = self.session.set_leverage(
                category="linear",
                symbol=symbol.upper(),
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except InvalidRequestError as exc:
            if exc.status_code == 110043:
                return
            raise
        if response.get("retCode") != 110043:
            self._require_ok(response)

    def get_position(self, symbol: str) -> PositionSnapshot:
        symbol = symbol.upper()
        response = self._require_ok(self.session.get_positions(category="linear", symbol=symbol))
        rows = response.get("result", {}).get("list", [])
        if any(int(item.get("positionIdx", 0)) != 0 for item in rows):
            raise BybitApiError(
                "Only one-way position mode is supported; do not auto-switch account mode."
            )
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
        response = self._require_ok(self.session.get_wallet_balance(accountType="UNIFIED"))
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

    def get_transaction_log(
        self,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int = 100,
        max_pages: int = 10,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read the Unified Account transaction log for funding attribution.

        Returns the rows and whether pagination was truncated. A truncated
        window must never be reported as complete accounting.
        """
        rows: list[dict[str, Any]] = []
        cursor = ""
        truncated = False
        for _ in range(max_pages):
            parameters: dict[str, Any] = {
                "accountType": "UNIFIED",
                "category": "linear",
                "limit": limit,
            }
            if start_time_ms is not None:
                parameters["startTime"] = start_time_ms
            if end_time_ms is not None:
                parameters["endTime"] = end_time_ms
            if cursor:
                parameters["cursor"] = cursor
            response = self._require_ok(self.session.get_transaction_log(**parameters))
            result = response.get("result", {})
            rows.extend(result.get("list", []))
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
        else:
            truncated = bool(cursor)
        return rows, truncated

    def get_api_key_information(self) -> dict[str, Any]:
        response = self._require_ok(self.session.get_api_key_information())
        result = response.get("result")
        if not isinstance(result, dict):
            raise BybitApiError("Bybit API key information response was incomplete")
        return result

    def get_last_price(self, symbol: str) -> float:
        symbol = symbol.upper()
        response = self._require_ok(self.session.get_tickers(category="linear", symbol=symbol))
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise BybitApiError(f"Bybit ticker response did not contain {symbol}")
        price = _as_float(rows[0].get("lastPrice"))
        if price <= 0:
            raise BybitApiError(f"Bybit ticker returned invalid last price for {symbol}")
        return price

    def get_open_orders(self, symbol: str) -> list[OpenOrder]:
        symbol = symbol.upper()
        response = self._require_ok(
            self.session.get_open_orders(category="linear", symbol=symbol, openOnly=0, limit=50)
        )
        rows = response.get("result", {}).get("list", [])
        return [
            OpenOrder(
                order_id=str(row.get("orderId", "")),
                order_link_id=str(row.get("orderLinkId", "")),
                symbol=str(row.get("symbol") or symbol).upper(),
                side=str(row.get("side", "")),
                order_type=str(row.get("orderType", "")),
                price=_as_float(row.get("price")),
                qty=_as_float(row.get("qty")),
                reduce_only=bool(row.get("reduceOnly", False)),
                status=str(row.get("orderStatus", "")),
            )
            for row in rows
            if str(row.get("orderLinkId", "")).startswith("botdca-")
        ]

    def open_long(self, symbol: str, qty: float, *, order_link_id: str | None = None) -> OrderAck:
        return self._place_market_long(
            symbol,
            qty,
            prefix="open",
            order_link_id=order_link_id,
        )

    def add_long(self, symbol: str, qty: float) -> OrderAck:
        return self._place_market_long(symbol, qty, prefix="dca-market")

    def _place_market_long(
        self,
        symbol: str,
        qty: float,
        *,
        prefix: str,
        order_link_id: str | None = None,
    ) -> OrderAck:
        self._require_live()
        order_link_id = order_link_id or new_order_link_id(prefix)
        return self._place_idempotent(
            symbol=symbol,
            order_link_id=order_link_id,
            request={
                "category": "linear",
                "symbol": symbol.upper(),
                "side": "Buy",
                "orderType": "Market",
                "qty": _as_order_number(qty),
                "positionIdx": 0,
            },
        )

    def place_dca_limit(
        self,
        symbol: str,
        qty: float,
        price: float,
        *,
        order_link_id: str | None = None,
    ) -> OrderAck:
        self._require_live()
        order_link_id = order_link_id or new_order_link_id("dca")
        return self._place_idempotent(
            symbol=symbol,
            order_link_id=order_link_id,
            request={
                "category": "linear",
                "symbol": symbol.upper(),
                "side": "Buy",
                "orderType": "Limit",
                "qty": _as_order_number(qty),
                "price": _as_order_number(price),
                "timeInForce": "GTC",
                "positionIdx": 0,
            },
        )

    def place_tp_limit(
        self,
        symbol: str,
        qty: float,
        price: float,
        *,
        order_link_id: str | None = None,
    ) -> OrderAck:
        self._require_live()
        order_link_id = order_link_id or new_order_link_id("tp")
        return self._place_idempotent(
            symbol=symbol,
            order_link_id=order_link_id,
            request={
                "category": "linear",
                "symbol": symbol.upper(),
                "side": "Sell",
                "orderType": "Limit",
                "qty": _as_order_number(qty),
                "price": _as_order_number(price),
                "timeInForce": "GTC",
                "positionIdx": 0,
                "reduceOnly": True,
            },
        )

    def close_long(self, symbol: str, qty: float, *, order_link_id: str | None = None) -> OrderAck:
        self._require_live()
        order_link_id = order_link_id or new_order_link_id("close")
        return self._place_idempotent(
            symbol=symbol,
            order_link_id=order_link_id,
            request={
                "category": "linear",
                "symbol": symbol.upper(),
                "side": "Sell",
                "orderType": "Market",
                "qty": _as_order_number(qty),
                "positionIdx": 0,
                "reduceOnly": True,
            },
        )

    def cancel_all(self, symbol: str) -> None:
        self._require_live()
        self._require_ok(self.session.cancel_all_orders(category="linear", symbol=symbol.upper()))

    def cancel_order(self, symbol: str, order_id: str) -> None:
        self._require_live()
        self._require_ok(
            self.session.cancel_order(
                category="linear",
                symbol=symbol.upper(),
                orderId=order_id,
            )
        )
