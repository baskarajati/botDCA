from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from botdca.config import Settings
from botdca.domain import BotState
from botdca.strategy import DcaStrategy, StrategyConfig


@dataclass
class RuntimeSnapshot:
    state: BotState
    symbol: str
    leverage: int
    live_trading: bool
    cycle_id: str | None
    average_entry: float | None
    position_qty: float
    dca_level: int
    next_dca_price: float | None
    tp_price: float | None


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.strategy = DcaStrategy(
            StrategyConfig(
                symbol=settings.bot_symbol,
                leverage=settings.bot_leverage,
                base_margin_usdt=settings.bot_base_margin_usdt,
                tp_percent=settings.bot_tp_percent,
            )
        )
        self._lock = RLock()

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            cycle = self.strategy.current_cycle
            return RuntimeSnapshot(
                state=self.strategy.state,
                symbol=self.settings.bot_symbol,
                leverage=self.settings.bot_leverage,
                live_trading=self.settings.bot_live_trading,
                cycle_id=cycle.id if cycle else None,
                average_entry=cycle.average_entry if cycle else None,
                position_qty=cycle.total_qty if cycle else 0.0,
                dca_level=cycle.dca_level if cycle else 0,
                next_dca_price=self.strategy.next_dca_trigger_price(),
                tp_price=cycle.tp_price if cycle else None,
            )

    def resume(self) -> RuntimeSnapshot:
        with self._lock:
            self.strategy.resume()
            return self.snapshot()

    def pause(self) -> RuntimeSnapshot:
        with self._lock:
            self.strategy.pause()
            return self.snapshot()

    def manual_close_and_pause(self) -> RuntimeSnapshot:
        with self._lock:
            # Exchange cancellation / reduce-only close will be wired in the live adapter milestone.
            if self.strategy.current_cycle is not None:
                self.strategy.mark_closed(realized_pnl_usdt=0.0)
            self.strategy.pause()
            return self.snapshot()
