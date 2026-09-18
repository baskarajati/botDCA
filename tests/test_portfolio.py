from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from botdca.exchange import AccountSnapshot
from botdca.portfolio import (
    CandidateOrder,
    PortfolioCoordinator,
    PortfolioGuards,
    SymbolExposure,
)


def _account(*, equity: float = 200.0, available: float = 50.0) -> AccountSnapshot:
    return AccountSnapshot(
        total_equity_usd=equity,
        total_wallet_balance_usd=equity,
        total_margin_balance_usd=equity,
        total_available_balance_usd=available,
        total_initial_margin_usd=equity - available,
        total_maintenance_margin_usd=1.0,
        total_perp_upl_usd=0.0,
        account_im_rate=0.2,
        account_mm_rate=0.01,
    )


def _guards(**overrides) -> PortfolioGuards:
    defaults = {
        "max_total_bot_margin_usdt": 1000.0,
        "max_total_bot_notional_usdt": 100_000.0,
        "min_available_balance_usdt": 0.0,
        "min_available_equity_ratio": 0.0,
        "deep_dca_level": 5,
        "max_simultaneous_deep_baskets": 3,
    }
    return PortfolioGuards(**{**defaults, **overrides})


def _coordinator(exposures=(), *, account=None, guards=None) -> PortfolioCoordinator:
    state = list(exposures)
    return PortfolioCoordinator(
        guards=guards or _guards(),
        account_reader=lambda: account if account is not None else _account(),
        exposure_reader=lambda: tuple(state),
    )


def _candidate(symbol="HYPEUSDT", *, qty=1.0, price=40.0, level=1, kind="dca") -> CandidateOrder:
    return CandidateOrder(
        symbol=symbol, kind=kind, qty=qty, price=price, leverage=1, target_dca_level=level
    )


def test_candidate_reports_notional_and_margin_separately() -> None:
    candidate = CandidateOrder("HYPEUSDT", "dca", qty=0.6, price=80.0, leverage=24)
    assert candidate.notional_usdt == pytest.approx(48.0)
    assert candidate.margin_usdt == pytest.approx(2.0)


def test_exposure_aggregates_across_every_symbol_not_per_symbol_pools() -> None:
    coordinator = _coordinator(
        [
            SymbolExposure("HYPEUSDT", 7, 5.0, margin_usdt=30.0, notional_usdt=720.0),
            SymbolExposure("ONDOUSDT", 6, 900.0, margin_usdt=20.0, notional_usdt=480.0),
            SymbolExposure("DOGEUSDT", 2, 500.0, margin_usdt=5.0, notional_usdt=120.0),
        ]
    )
    snapshot = coordinator.snapshot()
    assert snapshot.total_margin_usdt == pytest.approx(55.0)
    assert snapshot.total_notional_usdt == pytest.approx(1320.0)
    # HYPE at DCA7 and ONDO at DCA6 are not three ordinary shallow baskets.
    assert snapshot.deep_basket_count(5) == 2


def test_total_bot_margin_guard_blocks_the_candidate_that_would_breach_it() -> None:
    coordinator = _coordinator(
        [SymbolExposure("HYPEUSDT", 4, 1.0, margin_usdt=95.0, notional_usdt=95.0)],
        guards=_guards(max_total_bot_margin_usdt=100.0),
    )
    decision = coordinator.evaluate(_candidate(qty=1.0, price=10.0))
    assert not decision.allowed
    assert decision.guard == "max_total_bot_margin"


def test_reserve_floor_uses_the_larger_of_absolute_floor_and_equity_ratio() -> None:
    coordinator = _coordinator(
        account=_account(equity=200.0, available=50.0),
        guards=_guards(min_available_balance_usdt=10.0, min_available_equity_ratio=0.20),
    )
    decision = coordinator.evaluate(_candidate(qty=1.0, price=15.0))
    # floor = max(10, 200 * 0.20) = 40; 50 - 15 = 35 < 40
    assert not decision.allowed
    assert decision.guard == "min_available_balance"
    assert decision.reserve_floor_usdt == pytest.approx(40.0)


def test_deep_basket_guard_counts_the_candidate_itself() -> None:
    coordinator = _coordinator(
        [SymbolExposure("HYPEUSDT", 6, 1.0, margin_usdt=10.0, notional_usdt=10.0)],
        guards=_guards(deep_dca_level=5, max_simultaneous_deep_baskets=1),
    )
    shallow = coordinator.evaluate(_candidate("ONDOUSDT", qty=0.1, price=10.0, level=2))
    assert shallow.allowed

    deep = coordinator.evaluate(_candidate("ONDOUSDT", qty=0.1, price=10.0, level=5))
    assert not deep.allowed
    assert deep.guard == "max_simultaneous_deep_baskets"
    assert deep.projected_deep_basket_count == 2


def test_portfolio_floating_loss_guard_blocks_further_risk() -> None:
    coordinator = _coordinator(
        [
            SymbolExposure(
                "HYPEUSDT", 6, 1.0, margin_usdt=10.0, notional_usdt=10.0,
                unrealized_pnl_usdt=-45.0,
            )
        ],
        guards=_guards(max_total_floating_loss_usdt=30.0),
    )
    decision = coordinator.evaluate(_candidate(qty=0.1, price=10.0))
    assert not decision.allowed
    assert decision.guard == "max_total_floating_loss"


def test_authorization_is_refused_when_the_account_cannot_be_read() -> None:
    coordinator = PortfolioCoordinator(
        guards=_guards(),
        account_reader=lambda: None,
        exposure_reader=tuple,
    )
    decision, ack = coordinator.authorize(_candidate(), lambda: "submitted")
    assert not decision.allowed
    assert decision.guard == "account_unreadable"
    assert ack is None


def test_blocked_candidate_is_never_submitted() -> None:
    coordinator = _coordinator(guards=_guards(max_total_bot_margin_usdt=1.0))
    submitted: list[str] = []

    decision, ack = coordinator.authorize(
        _candidate(qty=1.0, price=40.0), lambda: submitted.append("x")
    )
    assert not decision.allowed
    assert ack is None
    assert submitted == []


def test_two_workers_cannot_spend_the_same_apparent_free_equity() -> None:
    """Worker A and B both see $50 free and both want $40. Exactly one wins."""
    coordinator = _coordinator(
        account=_account(equity=200.0, available=50.0),
        guards=_guards(min_available_balance_usdt=0.0, min_available_equity_ratio=0.0),
    )
    barrier = Barrier(2)
    submitted: list[str] = []

    def worker(symbol: str):
        candidate = CandidateOrder(
            symbol=symbol, kind="dca", qty=40.0, price=1.0, leverage=1, target_dca_level=1
        )

        def submit() -> str:
            submitted.append(symbol)
            return symbol

        barrier.wait(timeout=5)
        return coordinator.authorize(candidate, submit)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [
            pool.submit(worker, "HYPEUSDT"), pool.submit(worker, "ONDOUSDT")
        ]]

    allowed = [decision for decision, _ in results if decision.allowed]
    assert len(allowed) == 1, "both workers spent the same free equity"
    assert len(submitted) == 1
    blocked = next(decision for decision, _ in results if not decision.allowed)
    assert blocked.guard == "min_available_balance"


def test_many_concurrent_workers_never_exceed_the_portfolio_margin_budget() -> None:
    coordinator = _coordinator(
        account=_account(equity=1000.0, available=1000.0),
        guards=_guards(max_total_bot_margin_usdt=100.0),
    )
    barrier = Barrier(10)

    def worker(index: int):
        candidate = CandidateOrder(
            symbol=f"SYM{index}USDT",
            kind="dca",
            qty=30.0,
            price=1.0,
            leverage=1,
            target_dca_level=1,
        )
        barrier.wait(timeout=5)
        return coordinator.authorize(candidate, lambda: index)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = [f.result() for f in [pool.submit(worker, i) for i in range(10)]]

    allowed = [d for d, _ in results if d.allowed]
    # Budget 100 with 30 per order admits exactly three.
    assert len(allowed) == 3
    assert sum(d.candidate.margin_usdt for d in allowed) <= 100.0


def test_reservation_is_released_when_submission_raises() -> None:
    coordinator = _coordinator(
        account=_account(equity=200.0, available=50.0),
        guards=_guards(),
    )

    def explode() -> None:
        raise RuntimeError("exchange rejected the order")

    with pytest.raises(RuntimeError):
        coordinator.authorize(_candidate(qty=40.0, price=1.0), explode)

    assert coordinator.pending_symbols == ()
    # The capital is free again for the next attempt.
    decision, _ = coordinator.authorize(_candidate(qty=40.0, price=1.0), lambda: "ok")
    assert decision.allowed


def test_reservation_clears_once_the_symbol_exposure_catches_up() -> None:
    exposures: list[SymbolExposure] = []
    coordinator = PortfolioCoordinator(
        guards=_guards(),
        account_reader=lambda: _account(equity=200.0, available=50.0),
        exposure_reader=lambda: tuple(exposures),
    )
    coordinator.authorize(_candidate(qty=10.0, price=1.0, level=1), lambda: "ok")
    assert coordinator.pending_symbols == ("HYPEUSDT",)
    assert coordinator.snapshot().reserved_margin_usdt == pytest.approx(10.0)

    # The fill lands and reconciliation reports the new depth.
    exposures.append(
        SymbolExposure("HYPEUSDT", 1, 10.0, margin_usdt=10.0, notional_usdt=10.0)
    )
    snapshot = coordinator.snapshot()
    assert coordinator.pending_symbols == ()
    # Counted once, from real exposure, not twice.
    assert snapshot.total_margin_usdt == pytest.approx(10.0)


def test_reservation_expires_so_a_lost_order_cannot_block_capital_forever() -> None:
    now = [0.0]
    coordinator = PortfolioCoordinator(
        guards=_guards(),
        account_reader=lambda: _account(equity=200.0, available=50.0),
        exposure_reader=tuple,
        reservation_ttl_seconds=30.0,
        clock=lambda: now[0],
    )
    coordinator.authorize(_candidate(qty=10.0, price=1.0), lambda: "ok")
    assert coordinator.snapshot().reserved_margin_usdt == pytest.approx(10.0)

    now[0] = 31.0
    assert coordinator.snapshot().reserved_margin_usdt == pytest.approx(0.0)


def test_guards_reject_incoherent_configuration() -> None:
    with pytest.raises(ValueError, match="max_total_bot_margin_usdt must be positive"):
        PortfolioGuards(max_total_bot_margin_usdt=0)
    with pytest.raises(ValueError, match="min_available_equity_ratio"):
        PortfolioGuards(min_available_equity_ratio=1.0)
    with pytest.raises(ValueError, match="deep_dca_level"):
        PortfolioGuards(deep_dca_level=0)
