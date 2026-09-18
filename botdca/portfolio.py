"""Account-level portfolio risk authorization.

The dangerous GreenSynergy state is not one coin at DCA8. It is several
correlated coins escalating together, all drawing on one Bybit Unified Account.
Each symbol worker must therefore be authorized against a coherent portfolio
snapshot, not against its own capital pool.

The critical sequence is executed inside one lock shared by every symbol:

    read account -> read bot exposure -> size candidate -> project exposure
    -> evaluate guards -> submit -> persist

Exchange balances do not update instantly after a submission, so an authorized
candidate is also held as a pending reservation until the owning symbol's
observed exposure catches up or the reservation expires. Without that, two
workers a few hundred milliseconds apart would both read the same free equity.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import TypeVar

from botdca.exchange import AccountSnapshot

T = TypeVar("T")

DEFAULT_RESERVATION_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class PortfolioGuards:
    """Operator-configurable account-level limits."""

    max_total_bot_margin_usdt: float = 240.0
    max_total_bot_notional_usdt: float = 6000.0
    min_available_balance_usdt: float = 0.0
    min_available_equity_ratio: float = 0.20
    deep_dca_level: int = 5
    max_simultaneous_deep_baskets: int = 1
    max_total_floating_loss_usdt: float | None = None

    def __post_init__(self) -> None:
        if self.max_total_bot_margin_usdt <= 0:
            raise ValueError("max_total_bot_margin_usdt must be positive")
        if self.max_total_bot_notional_usdt <= 0:
            raise ValueError("max_total_bot_notional_usdt must be positive")
        if self.min_available_balance_usdt < 0:
            raise ValueError("min_available_balance_usdt cannot be negative")
        if not 0 <= self.min_available_equity_ratio < 1:
            raise ValueError("min_available_equity_ratio must be in [0, 1)")
        if self.deep_dca_level < 1:
            raise ValueError("deep_dca_level must be at least 1")
        if self.max_simultaneous_deep_baskets < 0:
            raise ValueError("max_simultaneous_deep_baskets cannot be negative")
        if (
            self.max_total_floating_loss_usdt is not None
            and self.max_total_floating_loss_usdt <= 0
        ):
            raise ValueError("max_total_floating_loss_usdt must be positive when set")


@dataclass(frozen=True, slots=True)
class SymbolExposure:
    """One symbol's current bot-owned exposure."""

    symbol: str
    dca_level: int
    position_qty: float
    margin_usdt: float
    notional_usdt: float
    unrealized_pnl_usdt: float = 0.0
    open_bot_orders: int = 0
    strategy_version_id: str | None = None
    max_dca_reached: bool = False

    def is_deep(self, deep_dca_level: int) -> bool:
        return self.position_qty > 0 and self.dca_level >= deep_dca_level


@dataclass(frozen=True, slots=True)
class CandidateOrder:
    """An order a worker wants to submit, before it is authorized."""

    symbol: str
    kind: str  # "entry" or "dca"
    qty: float
    price: float
    leverage: int
    target_dca_level: int = 0

    @property
    def notional_usdt(self) -> float:
        return self.qty * self.price

    @property
    def margin_usdt(self) -> float:
        return self.notional_usdt / self.leverage


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Aggregated bot exposure at one instant, including pending reservations."""

    account: AccountSnapshot | None
    exposures: tuple[SymbolExposure, ...]
    reserved_margin_usdt: float
    reserved_notional_usdt: float

    @property
    def total_margin_usdt(self) -> float:
        return sum(e.margin_usdt for e in self.exposures) + self.reserved_margin_usdt

    @property
    def total_notional_usdt(self) -> float:
        return sum(e.notional_usdt for e in self.exposures) + self.reserved_notional_usdt

    @property
    def total_unrealized_pnl_usdt(self) -> float:
        return sum(e.unrealized_pnl_usdt for e in self.exposures)

    @property
    def open_bot_orders(self) -> int:
        return sum(e.open_bot_orders for e in self.exposures)

    def deep_basket_count(self, deep_dca_level: int) -> int:
        return sum(1 for e in self.exposures if e.is_deep(deep_dca_level))

    @property
    def available_balance_usdt(self) -> float | None:
        if self.account is None:
            return None
        return self.account.total_available_balance_usd - self.reserved_margin_usdt

    @property
    def available_equity_ratio(self) -> float | None:
        if self.account is None or self.account.total_equity_usd <= 0:
            return None
        available = self.available_balance_usdt or 0.0
        return available / self.account.total_equity_usd

    def describe(self, deep_dca_level: int) -> dict:
        return {
            "total_bot_margin_usdt": self.total_margin_usdt,
            "total_bot_notional_usdt": self.total_notional_usdt,
            "total_unrealized_pnl_usdt": self.total_unrealized_pnl_usdt,
            "reserved_margin_usdt": self.reserved_margin_usdt,
            "open_bot_orders": self.open_bot_orders,
            "deep_dca_level": deep_dca_level,
            "deep_basket_count": self.deep_basket_count(deep_dca_level),
            "account_equity_usdt": self.account.total_equity_usd if self.account else None,
            "available_balance_usdt": self.available_balance_usdt,
            "available_equity_ratio": self.available_equity_ratio,
            "symbols": [
                {
                    "symbol": e.symbol,
                    "dca_level": e.dca_level,
                    "position_qty": e.position_qty,
                    "margin_usdt": e.margin_usdt,
                    "notional_usdt": e.notional_usdt,
                    "unrealized_pnl_usdt": e.unrealized_pnl_usdt,
                    "deep": e.is_deep(deep_dca_level),
                    "max_dca_reached": e.max_dca_reached,
                    "strategy_version_id": e.strategy_version_id,
                }
                for e in self.exposures
            ],
        }


@dataclass(frozen=True, slots=True)
class PortfolioDecision:
    """Why a candidate was allowed or refused, with the numbers behind it."""

    allowed: bool
    guard: str | None
    reason: str | None
    candidate: CandidateOrder
    snapshot: PortfolioSnapshot
    projected_total_margin_usdt: float
    projected_total_notional_usdt: float
    projected_available_balance_usdt: float | None
    reserve_floor_usdt: float | None
    projected_deep_basket_count: int

    def describe(self, deep_dca_level: int) -> dict:
        return {
            "allowed": self.allowed,
            "guard": self.guard,
            "reason": self.reason,
            "candidate": {
                "symbol": self.candidate.symbol,
                "kind": self.candidate.kind,
                "qty": self.candidate.qty,
                "price": self.candidate.price,
                "leverage": self.candidate.leverage,
                "target_dca_level": self.candidate.target_dca_level,
                "margin_usdt": self.candidate.margin_usdt,
                "notional_usdt": self.candidate.notional_usdt,
            },
            "projected_total_bot_margin_usdt": self.projected_total_margin_usdt,
            "projected_total_bot_notional_usdt": self.projected_total_notional_usdt,
            "projected_available_balance_usdt": self.projected_available_balance_usdt,
            "reserve_floor_usdt": self.reserve_floor_usdt,
            "projected_deep_basket_count": self.projected_deep_basket_count,
            "portfolio": self.snapshot.describe(deep_dca_level),
        }


@dataclass
class _Reservation:
    symbol: str
    margin_usdt: float
    notional_usdt: float
    target_dca_level: int
    expires_at: float


class PortfolioAuthorizationError(RuntimeError):
    """Raised when a submission fails after the candidate was authorized."""


@dataclass
class PortfolioCoordinator:
    """Serialize and authorize every capital-committing order across symbols.

    `account_reader` and `exposure_reader` are called inside the lock so the
    snapshot cannot interleave with another symbol's submission.
    """

    guards: PortfolioGuards
    account_reader: Callable[[], AccountSnapshot | None]
    exposure_reader: Callable[[], tuple[SymbolExposure, ...]]
    lock: RLock = field(default_factory=RLock)
    reservation_ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS
    clock: Callable[[], float] = monotonic
    _reservations: dict[str, _Reservation] = field(default_factory=dict, init=False)

    # -- snapshot -------------------------------------------------------

    def _prune_reservations(self, exposures: tuple[SymbolExposure, ...]) -> None:
        now = self.clock()
        observed = {e.symbol: e for e in exposures}
        for symbol, reservation in list(self._reservations.items()):
            if reservation.expires_at <= now:
                del self._reservations[symbol]
                continue
            exposure = observed.get(symbol)
            # The reservation has landed once the symbol's own exposure reaches
            # the level it was taken for.
            landed = (
                exposure is not None
                and exposure.dca_level >= reservation.target_dca_level
                and (reservation.target_dca_level > 0 or exposure.position_qty > 0)
            )
            if landed:
                del self._reservations[symbol]

    def snapshot(self) -> PortfolioSnapshot:
        with self.lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> PortfolioSnapshot:
        exposures = tuple(self.exposure_reader())
        self._prune_reservations(exposures)
        account = self.account_reader()
        return PortfolioSnapshot(
            account=account,
            exposures=exposures,
            reserved_margin_usdt=sum(r.margin_usdt for r in self._reservations.values()),
            reserved_notional_usdt=sum(r.notional_usdt for r in self._reservations.values()),
        )

    # -- evaluation -----------------------------------------------------

    def evaluate(self, candidate: CandidateOrder) -> PortfolioDecision:
        """Evaluate without submitting. Callers that submit must use `authorize`."""
        with self.lock:
            return self._evaluate_locked(candidate, self._snapshot_locked())

    def _evaluate_locked(
        self, candidate: CandidateOrder, snapshot: PortfolioSnapshot
    ) -> PortfolioDecision:
        guards = self.guards
        projected_margin = snapshot.total_margin_usdt + candidate.margin_usdt
        projected_notional = snapshot.total_notional_usdt + candidate.notional_usdt

        reserve_floor: float | None = None
        projected_available: float | None = None
        if snapshot.account is not None:
            reserve_floor = max(
                guards.min_available_balance_usdt,
                snapshot.account.total_equity_usd * guards.min_available_equity_ratio,
            )
            projected_available = (
                snapshot.available_balance_usdt or 0.0
            ) - candidate.margin_usdt

        already_deep = {
            e.symbol for e in snapshot.exposures if e.is_deep(guards.deep_dca_level)
        }
        projected_deep = set(already_deep)
        if candidate.target_dca_level >= guards.deep_dca_level:
            projected_deep.add(candidate.symbol)

        def decision(guard: str | None, reason: str | None) -> PortfolioDecision:
            return PortfolioDecision(
                allowed=guard is None,
                guard=guard,
                reason=reason,
                candidate=candidate,
                snapshot=snapshot,
                projected_total_margin_usdt=projected_margin,
                projected_total_notional_usdt=projected_notional,
                projected_available_balance_usdt=projected_available,
                reserve_floor_usdt=reserve_floor,
                projected_deep_basket_count=len(projected_deep),
            )

        if projected_margin > guards.max_total_bot_margin_usdt:
            return decision(
                "max_total_bot_margin",
                f"projected total bot margin {projected_margin:.4f} USDT exceeds the "
                f"portfolio limit {guards.max_total_bot_margin_usdt:.4f} USDT",
            )
        if projected_notional > guards.max_total_bot_notional_usdt:
            return decision(
                "max_total_bot_notional",
                f"projected total bot notional {projected_notional:.4f} USDT exceeds the "
                f"portfolio limit {guards.max_total_bot_notional_usdt:.4f} USDT",
            )
        if snapshot.account is None:
            return decision(
                "account_unreadable",
                "portfolio authorization requires an account snapshot",
            )
        if (
            reserve_floor is not None
            and projected_available is not None
            and projected_available < reserve_floor
        ):
            return decision(
                "min_available_balance",
                f"projected available balance {projected_available:.4f} USDT would fall "
                f"below the portfolio reserve floor {reserve_floor:.4f} USDT",
            )
        if len(projected_deep) > guards.max_simultaneous_deep_baskets:
            return decision(
                "max_simultaneous_deep_baskets",
                f"{len(projected_deep)} baskets would be at or beyond DCA"
                f"{guards.deep_dca_level}, above the limit of "
                f"{guards.max_simultaneous_deep_baskets}",
            )
        floating_limit = guards.max_total_floating_loss_usdt
        if floating_limit is not None:
            floating = snapshot.total_unrealized_pnl_usdt
            if floating < 0 and abs(floating) > floating_limit:
                return decision(
                    "max_total_floating_loss",
                    f"portfolio floating loss {abs(floating):.4f} USDT exceeds the limit "
                    f"{floating_limit:.4f} USDT",
                )
        return decision(None, None)

    # -- atomic authorization -------------------------------------------

    def authorize(
        self,
        candidate: CandidateOrder,
        submit: Callable[[], T],
    ) -> tuple[PortfolioDecision, T | None]:
        """Evaluate and, if allowed, submit inside the same critical section.

        The reservation is taken before `submit` runs and released if the
        submission raises, so a concurrent worker can never observe the capital
        as free while this order is in flight.
        """
        with self.lock:
            snapshot = self._snapshot_locked()
            decision = self._evaluate_locked(candidate, snapshot)
            if not decision.allowed:
                return decision, None
            self._reserve(candidate)
            try:
                result = submit()
            except Exception:
                self._release(candidate.symbol)
                raise
            return decision, result

    def _reserve(self, candidate: CandidateOrder) -> None:
        self._reservations[candidate.symbol] = _Reservation(
            symbol=candidate.symbol,
            margin_usdt=candidate.margin_usdt,
            notional_usdt=candidate.notional_usdt,
            target_dca_level=candidate.target_dca_level,
            expires_at=self.clock() + self.reservation_ttl_seconds,
        )

    def _release(self, symbol: str) -> None:
        self._reservations.pop(symbol, None)

    def release(self, symbol: str) -> None:
        """Drop a symbol's pending reservation, e.g. after a confirmed flat state."""
        with self.lock:
            self._release(symbol)

    @property
    def pending_symbols(self) -> tuple[str, ...]:
        with self.lock:
            return tuple(sorted(self._reservations))
