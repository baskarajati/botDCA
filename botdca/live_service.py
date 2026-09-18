from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic

from botdca.alerts import Alert, AlertCondition, AlertDispatcher, AlertSeverity
from botdca.domain import BotState
from botdca.exchange import AccountSnapshot, ExchangeExecutor, OpenOrder, OrderAck, PositionSnapshot
from botdca.instruments import InstrumentRules
from botdca.order_identity import deterministic_order_link_id
from botdca.order_plan import build_resting_order_plan, initial_market_qty
from botdca.persistence import EventStore
from botdca.portfolio import CandidateOrder, PortfolioCoordinator, SymbolExposure
from botdca.protection import (
    ProtectionAssessment,
    ProtectionStatus,
    assess_protection,
    installed,
    repaired,
)
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
    #: Basket reached its pinned live maximum; TP and reconciliation stay active.
    max_dca_reached: bool = False
    #: Something a human must resolve before the basket is healthy again.
    manual_intervention_required: bool = False
    protection_status: str = str(ProtectionStatus.NOT_APPLICABLE)
    protection_detail: str | None = None
    portfolio_decision: dict | None = None
    strategy_version_id: str | None = None


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
        lock: RLock | None = None,
        coordinator: PortfolioCoordinator | None = None,
        alerts: AlertDispatcher | None = None,
        deep_dca_level: int = 5,
        initial_tp_grace_seconds: float = 60.0,
    ) -> None:
        if reentry_delay_seconds < 0:
            raise ValueError("reentry_delay_seconds cannot be negative")
        if initial_tp_grace_seconds < 0:
            raise ValueError("initial_tp_grace_seconds cannot be negative")
        self.strategy = strategy
        self.exchange = exchange
        self.store = store
        self.rules = rules
        self.risk_limits = risk_limits
        self.reentry_delay_seconds = reentry_delay_seconds
        self.clock = clock
        # When a portfolio coordinator is supplied its lock is the shared
        # critical section, so account reads and submissions across every symbol
        # are serialized by the same object that authorizes them.
        self.coordinator = coordinator
        self.lock = lock or (coordinator.lock if coordinator is not None else RLock())
        self.alerts = alerts or AlertDispatcher()
        self.deep_dca_level = deep_dca_level
        self._flat_since: float | None = None
        self._last_alerted_depth: int = 0
        # The take profit is placed on the first sync that sees the filled entry,
        # so a missing TP right after this process submitted an entry is the
        # normal install step, not lost protection. Held in memory on purpose:
        # after a restart a missing TP is always treated as lost protection.
        self.initial_tp_grace_seconds = initial_tp_grace_seconds
        self._entry_submitted_at: float | None = None

    @property
    def symbol(self) -> str:
        return self.strategy.config.symbol.upper()

    @property
    def strategy_version_id(self) -> str | None:
        cycle = self.strategy.current_cycle
        if cycle is not None and cycle.strategy_version_id is not None:
            # A live basket reports the version it was PINNED to, which may
            # differ from the version configured for future baskets.
            return cycle.strategy_version_id
        return self.strategy.config.strategy_version_id

    # -- exposure and alerting -----------------------------------------

    def exposure(self) -> SymbolExposure:
        """This symbol's bot-owned exposure, for the portfolio coordinator."""
        cycle = self.strategy.current_cycle
        if cycle is None or cycle.average_entry is None:
            return SymbolExposure(
                symbol=self.symbol,
                dca_level=0,
                position_qty=0.0,
                margin_usdt=0.0,
                notional_usdt=0.0,
                strategy_version_id=self.strategy_version_id,
            )
        notional = sum(fill.price * fill.qty for fill in cycle.fills)
        return SymbolExposure(
            symbol=self.symbol,
            dca_level=cycle.dca_level,
            position_qty=cycle.total_qty,
            margin_usdt=notional / cycle.leverage,
            notional_usdt=notional,
            strategy_version_id=self.strategy_version_id,
            max_dca_reached=cycle.at_max_dca,
        )

    def _alert(
        self,
        condition: AlertCondition,
        severity: AlertSeverity,
        message: str,
        **context: object,
    ) -> None:
        cycle = self.strategy.current_cycle
        self.alerts.dispatch(
            Alert(
                condition=condition,
                severity=severity,
                symbol=self.symbol,
                message=message,
                cycle_id=cycle.id if cycle is not None else None,
                context={"strategy_version_id": self.strategy_version_id, **context},
            )
        )

    def _initial_tp_pending(self, assessment: ProtectionAssessment) -> bool:
        """True while the TP for an entry this process just submitted is not yet placed."""
        if assessment.status is not ProtectionStatus.MISSING or assessment.resting_tp_orders:
            return False
        if self._entry_submitted_at is None:
            return False
        return self.clock() - self._entry_submitted_at <= self.initial_tp_grace_seconds

    def _alert_depth(self) -> None:
        """Alert once as a basket crosses each deep DCA threshold."""
        cycle = self.strategy.current_cycle
        if cycle is None:
            self._last_alerted_depth = 0
            return
        level = cycle.dca_level
        if level >= self.deep_dca_level and level > self._last_alerted_depth:
            self._last_alerted_depth = level
            self._alert(
                AlertCondition.DCA_DEPTH_REACHED,
                AlertSeverity.WARNING,
                f"{self.symbol} basket reached DCA{level}",
                dca_level=level,
            )

    def sync(self) -> LiveSyncResult:
        with self.lock:
            return self._sync_locked()

    def _sync_locked(self) -> LiveSyncResult:
        position = self.exchange.get_position(self.symbol)
        if position.is_open:
            self._flat_since = None
            return self._sync_open_position(position)
        return self._sync_flat_position(position)

    def _sync_flat_position(self, position: PositionSnapshot) -> LiveSyncResult:
        # None of these conditions can hold without an open position.
        for condition in (
            AlertCondition.MISSING_TAKE_PROFIT,
            AlertCondition.MAX_DCA_REACHED,
            AlertCondition.RECONCILIATION_FAILED,
        ):
            self.alerts.clear(condition, self.symbol)
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
        market_price = self.exchange.get_last_price(self.symbol)
        try:
            qty = initial_market_qty(self.strategy, self.rules, market_price)
        except ValueError as exc:
            self._alert(
                AlertCondition.RISK_BLOCKED_ENTRY,
                AlertSeverity.WARNING,
                f"{self.symbol} initial allocation is not exchange-legal: {exc}",
            )
            return LiveSyncResult(
                status="entry_blocked",
                position=position,
                account=account,
                dca_blocked_reason=str(exc),
                strategy_version_id=self.strategy_version_id,
            )

        candidate = CandidateOrder(
            symbol=self.symbol,
            kind="entry",
            qty=float(qty),
            price=market_price,
            leverage=self.strategy.config.leverage,
            target_dca_level=0,
        )

        blocked = self._strategy_entry_block(candidate, account)
        if blocked is not None:
            self._alert(
                AlertCondition.RISK_BLOCKED_ENTRY,
                AlertSeverity.WARNING,
                f"{self.symbol} entry blocked: {blocked}",
            )
            return LiveSyncResult(
                status="entry_blocked",
                position=position,
                account=account,
                dca_blocked_reason=blocked,
                strategy_version_id=self.strategy_version_id,
            )

        def submit() -> OrderAck:
            self.exchange.set_leverage(self.symbol, self.strategy.config.leverage)
            entry_link_id = deterministic_order_link_id(
                "open",
                self.symbol,
                self.store.entry_generation(self.symbol),
                qty,
            )
            return self.exchange.open_long(
                self.symbol,
                float(qty),
                order_link_id=entry_link_id,
            )

        decision, entry_order = self._authorize(candidate, submit)
        if entry_order is None:
            self._alert(
                AlertCondition.RISK_BLOCKED_ENTRY,
                AlertSeverity.WARNING,
                f"{self.symbol} entry blocked by portfolio risk: {decision.get('reason')}",
                portfolio_decision=decision,
            )
            return LiveSyncResult(
                status="entry_blocked",
                position=position,
                account=account,
                dca_blocked_reason=decision.get("reason"),
                portfolio_decision=decision,
                strategy_version_id=self.strategy_version_id,
            )

        self._flat_since = None
        self._entry_submitted_at = self.clock()
        return LiveSyncResult(
            status="entry_submitted",
            position=position,
            account=account,
            entry_order=entry_order,
            portfolio_decision=decision,
            strategy_version_id=self.strategy_version_id,
        )

    def _strategy_entry_block(
        self, candidate: CandidateOrder, account: AccountSnapshot
    ) -> str | None:
        """Per-symbol guards that apply before portfolio authorization."""
        if candidate.margin_usdt > self.risk_limits.max_strategy_margin_usdt:
            return (
                f"initial margin {candidate.margin_usdt:.4f} USDT exceeds the configured "
                f"strategy margin cap {self.risk_limits.max_strategy_margin_usdt:.4f} USDT"
            )
        if self.coordinator is not None:
            return None
        # No coordinator configured: fall back to the single-symbol reserve check.
        reserve_floor = max(
            self.risk_limits.min_available_balance_usdt,
            account.total_equity_usd * self.risk_limits.min_available_equity_ratio,
        )
        projected_available = account.total_available_balance_usd - candidate.margin_usdt
        if projected_available < reserve_floor:
            return (
                f"initial entry would leave {projected_available:.4f} USD available, "
                f"below reserve floor {reserve_floor:.4f} USD"
            )
        return None

    def _authorize(
        self, candidate: CandidateOrder, submit: Callable[[], OrderAck]
    ) -> tuple[dict, OrderAck | None]:
        """Submit inside the portfolio critical section when one is configured."""
        if self.coordinator is None:
            return {"allowed": True, "guard": None, "reason": None}, submit()
        decision, ack = self.coordinator.authorize(candidate, submit)
        return decision.describe(self.coordinator.guards.deep_dca_level), ack

    def _sync_open_position(self, position: PositionSnapshot) -> LiveSyncResult:
        if position.side != "Buy":
            self.strategy.pause()
            self._alert(
                AlertCondition.MANUAL_INTERVENTION_REQUIRED,
                AlertSeverity.CRITICAL,
                f"refusing to manage unexpected {position.side or 'unknown'} "
                f"{self.symbol} position",
                position_side=position.side,
            )
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
            self._alert(
                AlertCondition.RECONCILIATION_FAILED,
                AlertSeverity.CRITICAL,
                f"{self.symbol} reconciliation failed: {exc}",
            )
            return LiveSyncResult(
                status="waiting_for_reconciliation",
                position=position,
                dca_blocked_reason=str(exc),
                manual_intervention_required=True,
                strategy_version_id=self.strategy_version_id,
            )

        self.alerts.clear(AlertCondition.RECONCILIATION_FAILED, self.symbol)
        account = self.exchange.get_account_snapshot()
        plan = build_resting_order_plan(
            self.strategy,
            self.rules,
            risk_limits=self.risk_limits,
            # With a coordinator configured, account-level reserve is evaluated
            # once, across every symbol, during portfolio authorization. Doing it
            # per symbol here as well would double-count the same capital and
            # would not see other symbols' pending reservations.
            account_snapshot=None if self.coordinator is not None else account,
        )

        cycle = self.strategy.current_cycle
        if cycle is None:
            raise LiveServiceSafetyError("reconciliation did not restore the open cycle")
        self._alert_depth()
        open_orders = self.exchange.get_open_orders(self.symbol)

        # -- protection first ------------------------------------------
        # An open position must hold a valid exchange-hosted TP or be reported
        # as explicitly unhealthy. Protection is assessed before the TP is
        # rebuilt so a genuinely unprotected basket is always journaled.
        assessment = assess_protection(
            symbol=self.symbol,
            position_qty=position.size,
            open_orders=open_orders,
            expected_qty=plan.take_profit.qty,
        )

        if assessment.status is ProtectionStatus.AMBIGUOUS:
            # Several reduce-only exits are resting. Placing another would
            # create a third; cancelling blind could strip protection. Hold.
            self._alert(
                AlertCondition.MISSING_TAKE_PROFIT,
                AlertSeverity.CRITICAL,
                assessment.detail,
                **assessment.describe(),
            )
            return LiveSyncResult(
                status="protection_ambiguous",
                position=position,
                account=account,
                dca_blocked_reason=assessment.detail,
                manual_intervention_required=True,
                protection_status=str(assessment.status),
                protection_detail=assessment.detail,
                strategy_version_id=self.strategy_version_id,
            )

        installing_initial_tp = self._initial_tp_pending(assessment)
        resizing_tp = (
            assessment.status is ProtectionStatus.MISSING and assessment.resting_tp_orders == 1
        )
        if (
            assessment.status is ProtectionStatus.MISSING
            and not installing_initial_tp
            and not resizing_tp
        ):
            self._alert(
                AlertCondition.MISSING_TAKE_PROFIT,
                AlertSeverity.CRITICAL,
                assessment.detail,
                **assessment.describe(),
            )

        tp_orders = [order for order in open_orders if _is_bot_order(order, "tp")]
        tp_match = _matching_limit(
            tp_orders,
            side="Sell",
            qty=float(plan.take_profit.qty),
            price=float(plan.take_profit.price),
            reduce_only=True,
        )
        if tp_match is None:
            tp = self.exchange.place_tp_limit(
                self.symbol,
                float(plan.take_profit.qty),
                float(plan.take_profit.price),
                order_link_id=deterministic_order_link_id(
                    "tp",
                    cycle.id,
                    plan.take_profit.qty,
                    plan.take_profit.price,
                ),
            )
            if installing_initial_tp:
                self._entry_submitted_at = None
                assessment = installed(
                    assessment, "initial take profit installed after the entry filled"
                )
            elif resizing_tp:
                # A DCA fill grew the position; the resting TP still covers the
                # old quantity until this replacement lands.
                assessment = repaired(
                    assessment,
                    f"take profit resized from {assessment.protected_qty} to "
                    f"{plan.take_profit.qty} {self.symbol} after the position changed",
                )
                self._alert(
                    AlertCondition.PROTECTION_REPAIRED,
                    AlertSeverity.INFO,
                    assessment.detail,
                    **assessment.describe(),
                )
            elif assessment.status is ProtectionStatus.MISSING:
                assessment = repaired(
                    assessment,
                    "protection was rebuilt after an open position was found without a "
                    "valid take profit",
                )
                self._alert(
                    AlertCondition.PROTECTION_REPAIRED,
                    AlertSeverity.WARNING,
                    assessment.detail,
                    **assessment.describe(),
                )
        else:
            tp = _ack(tp_match)
            if assessment.healthy:
                self._entry_submitted_at = None
                self.alerts.clear(AlertCondition.MISSING_TAKE_PROFIT, self.symbol, cycle.id)
        # Keep protection installed while replacing stale reduce-only exits.
        for order in tp_orders:
            if tp_match is None or order.order_id != tp_match.order_id:
                self.exchange.cancel_order(self.symbol, order.order_id)

        # -- max DCA -----------------------------------------------------
        # Reaching the ladder maximum is an explicit held state, not an absence
        # of code. TP stays live, reconciliation continues, manual reduce-only
        # close remains possible, and no DCA9 is ever invented.
        dca_orders = [order for order in open_orders if _is_bot_order(order, "dca")]
        if plan.max_dca_reached:
            for order in dca_orders:
                self.exchange.cancel_order(self.symbol, order.order_id)
            self._alert(
                AlertCondition.MAX_DCA_REACHED,
                AlertSeverity.CRITICAL,
                f"{self.symbol} basket reached its live maximum DCA{cycle.dca_level}; "
                "holding with take profit active and manual intervention required",
                dca_level=cycle.dca_level,
                max_dca_level=cycle.max_dca_level,
            )
            return LiveSyncResult(
                status="max_dca_reached",
                position=position,
                account=account,
                take_profit_order=tp,
                dca_blocked_reason=plan.dca_blocked_reason,
                max_dca_reached=True,
                manual_intervention_required=True,
                protection_status=str(assessment.status),
                protection_detail=assessment.detail,
                strategy_version_id=self.strategy_version_id,
            )

        dca = None
        dca_match = None
        portfolio_decision: dict | None = None
        dca_blocked_reason = plan.dca_blocked_reason
        partial_dca = next(
            (order for order in dca_orders if order.status == "PartiallyFilled"),
            None,
        )
        if partial_dca is not None:
            dca_match = partial_dca
        elif plan.next_dca is not None:
            dca_match = _matching_limit(
                dca_orders,
                side="Buy",
                qty=float(plan.next_dca.qty),
                price=float(plan.next_dca.price),
                reduce_only=False,
            )
        # Remove stale entry liabilities before creating a replacement.
        for order in dca_orders:
            if dca_match is None or order.order_id != dca_match.order_id:
                self.exchange.cancel_order(self.symbol, order.order_id)
        if partial_dca is not None:
            dca = _ack(partial_dca)
        elif plan.next_dca is not None:
            if dca_match is None:
                candidate = CandidateOrder(
                    symbol=self.symbol,
                    kind="dca",
                    qty=float(plan.next_dca.qty),
                    price=float(plan.next_dca.price),
                    leverage=cycle.leverage,
                    target_dca_level=cycle.dca_level + 1,
                )

                def submit() -> OrderAck:
                    return self.exchange.place_dca_limit(
                        self.symbol,
                        float(plan.next_dca.qty),
                        float(plan.next_dca.price),
                        order_link_id=deterministic_order_link_id(
                            "dca",
                            cycle.id,
                            cycle.dca_level,
                            plan.next_dca.qty,
                            plan.next_dca.price,
                        ),
                    )

                portfolio_decision, dca = self._authorize(candidate, submit)
                if dca is None:
                    dca_blocked_reason = portfolio_decision.get("reason")
                    self._alert(
                        AlertCondition.RISK_BLOCKED_DCA,
                        AlertSeverity.WARNING,
                        f"{self.symbol} DCA{candidate.target_dca_level} blocked by "
                        f"portfolio risk: {dca_blocked_reason}",
                        portfolio_decision=portfolio_decision,
                    )
            else:
                dca = _ack(dca_match)
        elif dca_blocked_reason:
            self._alert(
                AlertCondition.RISK_BLOCKED_DCA,
                AlertSeverity.WARNING,
                f"{self.symbol} DCA blocked: {dca_blocked_reason}",
            )

        return LiveSyncResult(
            status="orders_rebuilt",
            position=position,
            account=account,
            take_profit_order=tp,
            dca_order=dca,
            dca_blocked_reason=dca_blocked_reason,
            protection_status=str(assessment.status),
            protection_detail=assessment.detail,
            manual_intervention_required=assessment.requires_intervention,
            portfolio_decision=portfolio_decision,
            strategy_version_id=self.strategy_version_id,
        )


def _is_bot_order(order: OpenOrder, role: str) -> bool:
    return order.order_link_id.startswith(f"botdca-{role}-")


def _matching_limit(
    orders: list[OpenOrder],
    *,
    side: str,
    qty: float,
    price: float,
    reduce_only: bool,
) -> OpenOrder | None:
    tolerance = 1e-9
    return next(
        (
            order
            for order in orders
            if order.side == side
            and order.order_type == "Limit"
            and abs(order.qty - qty) <= tolerance
            and abs(order.price - price) <= tolerance
            and order.reduce_only is reduce_only
        ),
        None,
    )


def _ack(order: OpenOrder) -> OrderAck:
    return OrderAck(order_id=order.order_id, order_link_id=order.order_link_id)
