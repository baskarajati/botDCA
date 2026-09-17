from __future__ import annotations

from dataclasses import dataclass

from botdca.exchange import ExchangeExecutor, PositionSnapshot
from botdca.persistence import EventStore
from botdca.strategy import DcaStrategy


class ReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    position: PositionSnapshot
    restored_dca_level: int | None


def reconcile_strategy(
    *,
    strategy: DcaStrategy,
    exchange: ExchangeExecutor,
    store: EventStore,
    symbol: str,
    quantity_relative_tolerance: float = 1e-6,
) -> ReconciliationResult:
    """Restore local strategy state only when exchange and persisted fills agree."""
    position = exchange.get_position(symbol)

    if not position.is_open:
        if strategy.current_cycle is not None:
            strategy.mark_closed(realized_pnl_usdt=0.0)
        return ReconciliationResult("flat", position, None)

    if position.side != "Buy":
        raise ReconciliationError(
            f"refusing to manage unexpected {position.side or 'unknown'} position for {symbol}"
        )

    summary = store.open_cycle_execution_summary(symbol)
    if summary is None:
        raise ReconciliationError(
            f"open {symbol} position exists but no persisted bot Buy executions were found"
        )

    tolerance = max(1e-9, position.size * quantity_relative_tolerance)
    if abs(summary.total_buy_qty - position.size) > tolerance:
        raise ReconciliationError(
            "exchange position size does not match persisted open-cycle executions: "
            f"exchange={position.size}, persisted={summary.total_buy_qty}"
        )

    strategy.restore_cycle(
        average_entry=position.average_entry,
        total_qty=position.size,
        dca_level=summary.dca_level,
        last_order_qty=summary.last_order_qty,
    )
    return ReconciliationResult("restored", position, summary.dca_level)
