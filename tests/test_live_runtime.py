from decimal import Decimal
from threading import RLock

import pytest

from botdca.config import Settings
from botdca.database import Database, WorkerLease
from botdca.instruments import InstrumentRules
from botdca.live_runtime import (
    LiveWorkerConfigurationError,
    build_live_worker_deployment,
)
from botdca.runtime import BotRuntime


def _settings(**overrides) -> Settings:
    values = {
        "BOT_LIVE_TRADING": True,
        "BOT_START_LIVE_WORKER": True,
        "BYBIT_API_KEY": "key",
        "BYBIT_API_SECRET": "secret",
        "BYBIT_TESTNET": True,
        "BOT_OPERATOR_TOKEN": "test-only-operator-token-more-than-32-chars",
        "BOT_TRIAL_EQUITY_USDT": 100,
        # The portfolio cap must stay within the trial equity reference; it is
        # the guard that binds when several coins escalate together.
        "BOT_MAX_TOTAL_BOT_MARGIN_USDT": 80,
        "DATABASE_URL": "postgresql+psycopg://unit:unit@localhost/unit",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeInstrumentClient:
    def __init__(self, **kwargs) -> None:
        pass

    def get_linear_rules(self, symbol: str) -> InstrumentRules:
        return InstrumentRules(
            symbol=symbol,
            tick_size=Decimal("0.01"),
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.01"),
            min_notional_value=Decimal(5),
            max_market_order_qty=Decimal(100),
        )


class FakeExchange:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def test_live_worker_construction_requires_both_opt_in_gates() -> None:
    settings = _settings(BOT_LIVE_TRADING=False)

    with pytest.raises(LiveWorkerConfigurationError, match="BOT_LIVE_TRADING"):
        build_live_worker_deployment(settings, BotRuntime(settings))


def test_live_worker_construction_wires_authenticated_dependencies() -> None:
    settings = _settings()

    deployment = build_live_worker_deployment(
        settings,
        BotRuntime(settings),
        database_factory=lambda url: Database("sqlite+pysqlite:///:memory:"),
        exchange_factory=FakeExchange,
        instrument_client_factory=FakeInstrumentClient,
        stream_factory=FakeStream,
    )

    assert deployment.worker.service.exchange.kwargs["live_trading"] is True
    assert deployment.worker.stream.kwargs["api_key"] == "key"
    assert deployment.worker.interval_seconds == 2.0


def test_live_worker_uses_runtime_symbol_and_shared_portfolio_lock() -> None:
    settings = _settings()
    portfolio_lock = RLock()
    runtime = BotRuntime(
        settings,
        symbol="BTCUSDT",
        base_margin_usdt=2.5,
        lock=portfolio_lock,
    )

    deployment = build_live_worker_deployment(
        settings,
        runtime,
        database_factory=lambda url: Database("sqlite+pysqlite:///:memory:"),
        exchange_factory=FakeExchange,
        instrument_client_factory=FakeInstrumentClient,
        stream_factory=FakeStream,
    )

    assert deployment.worker.service.symbol == "BTCUSDT"
    assert deployment.worker.service.strategy.config.base_margin_usdt == 2.5
    assert deployment.worker.service.lock is portfolio_lock
    assert deployment.lease.symbol == "BTCUSDT"


def test_worker_lease_rejects_duplicate_and_can_be_reacquired() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    first = WorkerLease(database, "HYPEUSDT")
    second = WorkerLease(database, "HYPEUSDT")

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
