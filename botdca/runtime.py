from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from botdca.config import Settings
from botdca.domain import BotState
from botdca.risk import RiskLimits, committed_margin_usdt, evaluate_next_dca
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
    committed_margin_usdt: float
    max_strategy_margin_usdt: float
    max_dca_level: int
    min_available_balance_usdt: float
    min_available_equity_ratio: float
    next_dca_allowed: bool
    projected_margin_after_next_dca_usdt: float
    dca_blocked_reason: str | None


class BotRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        symbol: str | None = None,
        base_margin_usdt: float | None = None,
        lock: RLock | None = None,
    ) -> None:
        self.settings = settings
        configured_symbol = (symbol or settings.bot_symbol).upper()
        configured_base_margin = (
            settings.bot_base_margin_usdt
            if base_margin_usdt is None
            else base_margin_usdt
        )
        self.strategy = DcaStrategy(
            StrategyConfig(
                symbol=configured_symbol,
                leverage=settings.bot_leverage,
                base_margin_usdt=configured_base_margin,
                tp_percent=settings.bot_tp_percent,
            )
        )
        self.risk_limits = RiskLimits(
            max_dca_level=settings.bot_max_dca_level,
            max_strategy_margin_usdt=settings.bot_max_strategy_margin_usdt,
            min_available_balance_usdt=settings.bot_min_available_balance_usdt,
            min_available_equity_ratio=settings.bot_min_available_equity_ratio,
        )
        self._lock = lock or RLock()

    @property
    def lock(self) -> RLock:
        return self._lock

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            cycle = self.strategy.current_cycle
            margin = committed_margin_usdt(self.strategy)
            decision = evaluate_next_dca(self.strategy, self.risk_limits)
            return RuntimeSnapshot(
                state=self.strategy.state,
                symbol=self.strategy.config.symbol,
                leverage=self.settings.bot_leverage,
                live_trading=self.settings.bot_live_trading,
                cycle_id=cycle.id if cycle else None,
                average_entry=cycle.average_entry if cycle else None,
                position_qty=cycle.total_qty if cycle else 0.0,
                dca_level=cycle.dca_level if cycle else 0,
                next_dca_price=self.strategy.next_dca_trigger_price(),
                tp_price=cycle.tp_price if cycle else None,
                committed_margin_usdt=margin,
                max_strategy_margin_usdt=self.risk_limits.max_strategy_margin_usdt,
                max_dca_level=self.risk_limits.max_dca_level,
                min_available_balance_usdt=self.risk_limits.min_available_balance_usdt,
                min_available_equity_ratio=self.risk_limits.min_available_equity_ratio,
                next_dca_allowed=decision.allowed,
                projected_margin_after_next_dca_usdt=decision.projected_margin_usdt,
                dca_blocked_reason=decision.reason,
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
            if self.strategy.current_cycle is not None:
                self.strategy.mark_closed(realized_pnl_usdt=0.0)
            self.strategy.pause()
            return self.snapshot()
