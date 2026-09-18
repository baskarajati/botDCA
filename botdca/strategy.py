from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from botdca.domain import DEFAULT_DCA_STEPS, BotState, DcaStep, Fill, TradingCycle
from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy_version import StrategyVersion, live_dca_steps_for


@dataclass
class StrategyConfig:
    symbol: str = "HYPEUSDT"
    leverage: int = 24
    base_margin_usdt: float = 1.0
    tp_percent: float = 1.09
    dca_steps: tuple[DcaStep, ...] = DEFAULT_DCA_STEPS
    #: Identity of the immutable strategy version these parameters came from.
    #: A basket opened under this config is pinned to it for its whole life.
    strategy_version_id: str | None = None
    #: How this symbol's initial order is sized. Defaults to the historical
    #: fixed-margin behaviour so existing configurations keep working.
    allocation: InitialAllocation | None = None

    @property
    def initial_allocation(self) -> InitialAllocation:
        if self.allocation is not None:
            return self.allocation
        return InitialAllocation(SizingMode.FIXED_MARGIN_USDT, self.base_margin_usdt)


def strategy_config_from_version(
    version: StrategyVersion,
    *,
    symbol: str,
    allocation: InitialAllocation,
) -> StrategyConfig:
    """Build a runtime config from an immutable strategy version.

    Only the version's LIVE ladder is ever installed. Research-only levels are
    not reachable from anything this function returns.
    """
    return StrategyConfig(
        symbol=symbol.upper(),
        leverage=version.leverage,
        base_margin_usdt=(
            allocation.value
            if allocation.mode is SizingMode.FIXED_MARGIN_USDT
            else 0.0
        ),
        tp_percent=version.take_profit_percent,
        dca_steps=live_dca_steps_for(version),
        strategy_version_id=version.version_id,
        allocation=allocation,
    )


class DcaTriggerReference(StrEnum):
    WEIGHTED_AVERAGE = "weighted-average"
    PREVIOUS_FILL = "previous-fill"
    INITIAL_ENTRY = "initial-entry"


class DcaStrategy:
    def __init__(
        self,
        config: StrategyConfig,
        *,
        trigger_reference: DcaTriggerReference = DcaTriggerReference.WEIGHTED_AVERAGE,
    ) -> None:
        self.config = config
        self.trigger_reference = trigger_reference
        self.state = BotState.PAUSED
        self.current_cycle: TradingCycle | None = None
        self.reentry_enabled = False

    def resume(self) -> None:
        self.reentry_enabled = True
        self.state = BotState.ACTIVE if self.current_cycle is not None else BotState.IDLE

    def pause(self) -> None:
        # Pausing disables the next cycle. An already-open cycle remains
        # manageable: TP/DCA calculations continue while state is PAUSED.
        self.reentry_enabled = False
        self.state = BotState.PAUSED

    def _new_cycle(self, **overrides) -> TradingCycle:
        """Create a basket pinned to the strategy version currently configured."""
        return TradingCycle(
            symbol=self.config.symbol,
            leverage=self.config.leverage,
            base_margin_usdt=self.config.base_margin_usdt,
            tp_percent=self.config.tp_percent,
            strategy_version_id=self.config.strategy_version_id,
            max_dca_level=len(self.config.dca_steps),
            **overrides,
        )

    def begin_cycle(self, fill_price: float, qty: float | None = None) -> TradingCycle:
        if self.state != BotState.IDLE or not self.reentry_enabled:
            raise RuntimeError(f"cannot begin cycle from state={self.state}")
        if fill_price <= 0:
            raise ValueError("fill_price must be positive")

        cycle = self._new_cycle()
        if qty is None:
            allocation = self.config.initial_allocation
            if allocation.mode is SizingMode.FIXED_BASE_QUANTITY:
                qty = allocation.value
            else:
                qty = allocation.value * self.config.leverage / fill_price
        if qty <= 0:
            raise ValueError("initial qty must be positive")
        cycle.fills.append(Fill(price=fill_price, qty=qty, kind="initial"))
        self.current_cycle = cycle
        self.state = BotState.ACTIVE
        return cycle

    def restore_cycle(
        self,
        *,
        average_entry: float,
        total_qty: float,
        dca_level: int,
        last_order_qty: float,
        cycle_id: str | None = None,
        strategy_version_id: str | None = None,
    ) -> TradingCycle:
        if average_entry <= 0 or total_qty <= 0 or last_order_qty <= 0:
            raise ValueError("restored position values must be positive")
        if dca_level < 0 or dca_level > len(self.config.dca_steps):
            raise ValueError("restored dca_level is outside configured ladder")
        if dca_level > 0 and self.trigger_reference != DcaTriggerReference.WEIGHTED_AVERAGE:
            raise ValueError(
                "non-average trigger references require individual restored fill history"
            )

        cycle = self._new_cycle(
            dca_level_override=dca_level,
            last_order_qty_override=last_order_qty,
        )
        if cycle_id is not None:
            cycle.id = cycle_id
        if strategy_version_id is not None:
            # A recovered basket keeps the version it was opened under, even if
            # the configured version has moved on since.
            cycle.strategy_version_id = strategy_version_id
        cycle.fills.append(Fill(price=average_entry, qty=total_qty, kind="restored"))
        self.current_cycle = cycle
        self.state = BotState.ACTIVE if self.reentry_enabled else BotState.PAUSED
        return cycle

    def _cycle_is_manageable(self) -> bool:
        return self.current_cycle is not None and self.state in {BotState.ACTIVE, BotState.PAUSED}

    def next_dca_trigger_price(self) -> float | None:
        cycle = self.current_cycle
        if not self._cycle_is_manageable() or cycle is None:
            return None
        level = cycle.dca_level
        if level >= len(self.config.dca_steps):
            return None
        reference_price = self._next_dca_reference_price(cycle)
        if reference_price is None:
            return None
        step = self.config.dca_steps[level]
        return reference_price * (1 - step.drop_percent_from_average / 100)

    def _next_dca_reference_price(self, cycle: TradingCycle) -> float | None:
        if not cycle.fills:
            return None
        if self.trigger_reference == DcaTriggerReference.WEIGHTED_AVERAGE:
            return cycle.average_entry
        if self.trigger_reference == DcaTriggerReference.PREVIOUS_FILL:
            return cycle.fills[-1].price
        if self.trigger_reference == DcaTriggerReference.INITIAL_ENTRY:
            return cycle.fills[0].price
        raise ValueError(f"unsupported DCA trigger reference: {self.trigger_reference}")

    def next_dca_qty(self) -> float | None:
        cycle = self.current_cycle
        if not self._cycle_is_manageable() or cycle is None:
            return None
        level = cycle.dca_level
        if level >= len(self.config.dca_steps):
            return None
        previous_qty = cycle.last_order_qty
        if previous_qty is None:
            return None
        return previous_qty * self.config.dca_steps[level].size_multiplier_from_previous

    def should_take_profit(self, market_price: float) -> bool:
        cycle = self.current_cycle
        return bool(
            self._cycle_is_manageable()
            and cycle is not None
            and cycle.tp_price is not None
            and market_price >= cycle.tp_price
        )

    def should_dca(self, market_price: float) -> bool:
        trigger = self.next_dca_trigger_price()
        return trigger is not None and market_price <= trigger

    def apply_dca_fill(self, fill_price: float, qty: float | None = None) -> Fill:
        cycle = self.current_cycle
        if not self._cycle_is_manageable() or cycle is None:
            raise RuntimeError("no active cycle")
        expected_qty = self.next_dca_qty()
        if expected_qty is None:
            raise RuntimeError("DCA ladder exhausted")
        fill = Fill(price=fill_price, qty=qty if qty is not None else expected_qty, kind="dca")
        cycle.fills.append(fill)
        if cycle.dca_level_override is not None:
            cycle.dca_level_override += 1
            cycle.last_order_qty_override = fill.qty
        return fill

    def mark_closed(self, realized_pnl_usdt: float = 0.0) -> TradingCycle:
        cycle = self.current_cycle
        if cycle is None:
            raise RuntimeError("no active cycle")
        cycle.realized_pnl_usdt = realized_pnl_usdt
        self.current_cycle = None
        self.state = BotState.IDLE if self.reentry_enabled else BotState.PAUSED
        return cycle
