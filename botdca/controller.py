from __future__ import annotations

from dataclasses import dataclass

from botdca.exchange import ExchangeExecutor, OrderAck, PositionSnapshot
from botdca.order_identity import deterministic_order_link_id
from botdca.strategy import DcaStrategy


class ControllerSafetyError(RuntimeError):
    """Raised when exchange state is unsafe for an automated control action."""


@dataclass(frozen=True, slots=True)
class ManualCloseResult:
    status: str
    position_before_close: PositionSnapshot
    close_order: OrderAck | None


class TradingController:
    """Coordinate strategy state with consequential exchange actions.

    REST acknowledgements are not treated as fills. A submitted close therefore
    leaves the local cycle intact and the strategy paused until private execution
    and position events are reconciled.
    """

    def __init__(
        self,
        *,
        strategy: DcaStrategy,
        exchange: ExchangeExecutor,
        symbol: str,
    ) -> None:
        self.strategy = strategy
        self.exchange = exchange
        self.symbol = symbol.upper()

    def manual_close_and_pause(self) -> ManualCloseResult:
        # Pause first so a flat-position observation cannot trigger a re-entry
        # while cancellation/close requests are in flight.
        self.strategy.pause()
        for order in self.exchange.get_open_orders(self.symbol):
            if order.order_link_id.startswith("botdca-"):
                self.exchange.cancel_order(self.symbol, order.order_id)

        position = self.exchange.get_position(self.symbol)
        if not position.is_open:
            if self.strategy.current_cycle is not None:
                self.strategy.mark_closed(realized_pnl_usdt=0.0)
                self.strategy.pause()
            return ManualCloseResult(
                status="already_flat",
                position_before_close=position,
                close_order=None,
            )

        if position.side != "Buy":
            raise ControllerSafetyError(
                f"refusing manual long close because exchange position side is "
                f"{position.side or 'unknown'}"
            )

        cycle_identity = (
            self.strategy.current_cycle.id
            if self.strategy.current_cycle is not None
            else f"{position.average_entry}:{position.size}"
        )
        close_order = self.exchange.close_long(
            self.symbol,
            position.size,
            order_link_id=deterministic_order_link_id(
                "close",
                self.symbol,
                cycle_identity,
                position.size,
            ),
        )
        return ManualCloseResult(
            status="close_submitted",
            position_before_close=position,
            close_order=close_order,
        )
