from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from botdca.domain import BotState
from botdca.exchange import AccountSnapshot, ExchangeExecutor, OrderAck, PositionSnapshot
from botdca.instruments import InstrumentRules
from botdca.order_plan import build_resting_order_plan, initial_market_qty
from botdca.persistence import EventStore
from botdca.reconciliation import ReconciliationError, reconcile_strategy
from botdca.risk import RiskLimits
from botdca.strategy import DcaStrategy


class LiveServiceSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiveSyncResult:
    status: str
    position: PositionSnapshot
    account: AccountSnapshot | None = None
    entry_order: OrderAck | None = None
    take_profit_order: OrderAck | None = None
    dca_order: OrderAck | None = None
    dca_blocked_reason: str | None = None
    reentry_wait_seconds: float = 0.0


class LiveStrategyService:
    """Reconcile exchange truth, then submit only the orders implied by that truth."""

    def __init__(
        self,
        *,
        strategy: DcaStrategy,
        exchange: ExchangeExecutor,
        store: EventStore,
        rules: InstrumentRules,
        risk_limits: RiskLimits,
        reentry_delay_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if reentry_delay_seconds < 0:
            raise ValueError("reentry_delay_seconds cannot be negative")
        self.strategy = strategy
        self.exchange = exchange
        self.store = store
        self.rules = rules
        self.risk_limits = risk_limits
        self.reentry_delay_seconds = reentry_delay_seconds
        self.clock = clock
        self._flat_since: float | None = None

    @property
    def symbol(self) -> str:
        return self.strategy.config.symbol.upper()

    def sync(self) -> LiveSyncResult:
        position = self.exchange.get_position(self.symbol)
        if position.is_open:
            self._flat_since = None
            return self._sync_open_position(position)
        return self._sync_flat_position(position)

    def _sync_flat_position(self, position: PositionSnapshot) -> LiveSyncResult:
        was_paused = self.strategy.state == BotState.PAUSED
        if self.strategy.current_cycle is not None:
            self.strategy.mark_closed(realized_pnl_usdt=0.0)
            if was_paused:
                self.strategy.pause()

        if self.strategy.state == BotState.PAUSED:
            self._flat_since = None
            return LiveSyncResult(status="flat_paused", position=position)

        now = self.clock()
        if self._flat_since is None:
            self._flat_since = now
        elapsed = now - self._flat_since
        remaining = max(0.0, self.reentry_delay_seconds - elapsed)
        if remaining > 0:
            return LiveSyncResult(
                status="reentry_wait",
                position=position,
                reentry_wait_seconds=remaining,
            )

        account = self.exchange.get_account_snapshot()
        reserve_floor = max(
            self.risk_limits.min_available_balance_usdt,
            account.total_equity_usd * self.risk_limits.min_available_equity_ratio,
        )
        projected_available = (
            account.total_available_balance_usd - self.strategy.config.base_margin_usdt
        )
        if self.strategy.config.base_margin_usdt > self.risk_limits.max_strategy_margin_usdt:
            return LiveSyncResult(
                status="entry_blocked",
                position=position,
                account=account,
                dca_blocked_reason="base margin exceeds configured strategy margin cap",
            )
        if projected_available < reserve_floor:
            return LiveSyncResult(
                status="entry_blocked",
                position=position,
                account=account,
                dca_blocked_reason=(
                    f"initial entry would leave {projected_available:.4f} USD available, "
                    f"below reserve floor {reserve_floor:.4f} USD"
                ),
            )

        market_price = self.exchange.get_last_price(self.symbol)
        qty = initial_market_qty(self.strategy, self.rules, market_price)
        self.exchange.set_leverage(self.symbol, self.strategy.config.leverage)
        entry_order = self.exchange.open_long(self.symbol, float(qty))
        self._flat_since = None
        return LiveSyncResult(
            status="entry_submitted",
            position=position,
            account=account,
            entry_order=entry_order,
        )

    def _sync_open_position(self, position: PositionSnapshot) -> LiveSyncResult:
        if position.side != "Buy":
            self.strategy.pause()
            raise LiveServiceSafetyError(
                f"refusing to manage unexpected {position.side or 'unknown'} position"
            )

        try:
            reconcile_strategy(
                strategy=self.strategy,
                exchange=self.exchange,
                store=self.store,
                symbol=self.symbol,
            )
        except ReconciliationError as exc:
            return LiveSyncResult(
                status="waiting_for_reconciliation",
                position=position,
                dca_blocked_reason=str(exc),
            )

        self.exchange.cancel_all(self.symbol)
        account = self.exchange.get_account_snapshot()
        plan = build_resting_order_plan(
            self.strategy,
            self.rules,
            risk_limits=self.risk_limits,
            account_snapshot=account,
        )

        tp = self.exchange.place_tp_limit(
            self.symbol,
            float(plan.take_profit.qty),
            float(plan.take_profit.price),
        )
        dca = None
        if plan.next_dca is not None:
            dca = self.exchange.place_dca_limit(
                self.symbol,
                float(plan.next_dca.qty),
                float(plan.next_dca.price),
            )

        return LiveSyncResult(
            status="orders_rebuilt",
            position=position,
            account=account,
            take_profit_order=tp,
            dca_order=dca,
            dca_blocked_reason=plan.dca_blocked_reason,
        )
