from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from botdca.config import Settings
from botdca.domain import BasketStatus, BotState
from botdca.portfolio import PortfolioGuards
from botdca.risk import RiskLimits, committed_margin_usdt, evaluate_next_dca
from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy import DcaStrategy, StrategyConfig, strategy_config_from_version
from botdca.strategy_version import get_strategy_version


class ManualResumeRequiredError(RuntimeError):
    """A resume was attempted that did not come from the operator.

    BOT_TRIAL_MANUAL_RESUME_AFTER_RESTART promises that a restarted trial bot
    trades again only after a human resumes it. Nothing resumes automatically
    today, so this guard exists to keep that promise true for code written
    later: any resume that does not identify itself as an operator action is
    refused while the requirement stands.
    """


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
    strategy_version_id: str | None = None
    basket_status: str = str(BasketStatus.FLAT)
    max_dca_reached: bool = False
    sizing_mode: str = str(SizingMode.FIXED_MARGIN_USDT)
    sizing_value: float = 0.0
    #: True while the bot still waits for the operator Resume that this
    #: process start requires. False once an operator has resumed, and always
    #: False when the setting is off.
    manual_resume_required: bool = False


class BotRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        symbol: str | None = None,
        base_margin_usdt: float | None = None,
        lock: RLock | None = None,
        allocation: InitialAllocation | None = None,
        strategy_version_id: str | None = None,
    ) -> None:
        self.settings = settings
        configured_symbol = (symbol or settings.bot_symbol).upper()
        configured_base_margin = (
            settings.bot_base_margin_usdt
            if base_margin_usdt is None
            else base_margin_usdt
        )
        configured_allocation = allocation or InitialAllocation(
            SizingMode.FIXED_MARGIN_USDT, configured_base_margin
        )
        version_id = strategy_version_id or settings.bot_strategy_version_id
        try:
            self.strategy_version = get_strategy_version(version_id)
        except KeyError:
            self.strategy_version = None

        if self.strategy_version is not None:
            config = strategy_config_from_version(
                self.strategy_version,
                symbol=configured_symbol,
                allocation=configured_allocation,
            )
            # The live ladder is bounded by the operator's configured maximum,
            # which trial mode may only tighten. Research-only levels are never
            # part of `live_dca_steps`, so they cannot be reached from here.
            config.dca_steps = config.dca_steps[: settings.effective_max_dca_level]
        else:
            config = StrategyConfig(
                symbol=configured_symbol,
                leverage=settings.bot_leverage,
                base_margin_usdt=configured_base_margin,
                tp_percent=settings.bot_tp_percent,
                allocation=configured_allocation,
            )
        self.strategy = DcaStrategy(config)
        self.risk_limits = RiskLimits(
            max_dca_level=settings.effective_max_dca_level,
            max_strategy_margin_usdt=settings.bot_max_strategy_margin_usdt,
            min_available_balance_usdt=settings.bot_min_available_balance_usdt,
            min_available_equity_ratio=settings.bot_min_available_equity_ratio,
        )
        self.portfolio_guards = PortfolioGuards(
            max_total_bot_margin_usdt=settings.effective_max_total_bot_margin_usdt,
            max_total_bot_notional_usdt=settings.bot_max_total_bot_notional_usdt,
            min_available_balance_usdt=settings.bot_min_available_balance_usdt,
            min_available_equity_ratio=settings.bot_min_available_equity_ratio,
            deep_dca_level=settings.bot_deep_dca_level,
            max_simultaneous_deep_baskets=settings.bot_max_simultaneous_deep_baskets,
            max_total_floating_loss_usdt=settings.bot_max_total_floating_loss_usdt,
        )
        self._lock = lock or RLock()
        # A fresh process has not been resumed by anyone yet.
        self._manual_resume_required = settings.effective_manual_resume_after_restart

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
                leverage=self.strategy.config.leverage,
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
                strategy_version_id=(
                    cycle.strategy_version_id
                    if cycle is not None
                    else self.strategy.config.strategy_version_id
                ),
                basket_status=str(cycle.status if cycle else BasketStatus.FLAT),
                max_dca_reached=bool(cycle and cycle.at_max_dca),
                sizing_mode=str(self.strategy.config.initial_allocation.mode),
                sizing_value=self.strategy.config.initial_allocation.value,
                manual_resume_required=self._manual_resume_required,
            )

    @property
    def manual_resume_required(self) -> bool:
        """True while this process still needs an operator Resume before trading."""
        with self._lock:
            return self._manual_resume_required

    def resume(self, *, operator: bool = False) -> RuntimeSnapshot:
        """Resume trading. `operator` must be true for a human-initiated resume.

        The default is the refusing one, so a resume added later somewhere else
        fails loudly instead of silently restarting a funded bot.
        """
        with self._lock:
            if self._manual_resume_required and not operator:
                raise ManualResumeRequiredError(
                    f"{self.strategy.config.symbol} requires an operator Resume after a "
                    "restart because BOT_TRIAL_MANUAL_RESUME_AFTER_RESTART is enabled"
                )
            self._manual_resume_required = False
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
