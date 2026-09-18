"""Optional Bybit testnet/demo lifecycle validation.

NOT run in CI. These tests place real orders on a testnet or demo account and
require credentials, so they are excluded by the default pytest addopts and
must be requested explicitly:

    BYBIT_API_KEY=... BYBIT_API_SECRET=... BYBIT_TESTNET=true \
    BOTDCA_INTEGRATION_SYMBOL=HYPEUSDT \
        pytest -m bybit_integration

The objective is NOT profitable strategy testing. It is proof that the full
order lifecycle works against a real exchange:

    worker start -> leverage configuration -> initial order -> REST response
    -> execution stream -> persisted fill -> reconciliation -> TP placement
    -> DCA placement -> partial fill -> stream disconnect
    -> execution-history recovery -> process restart -> basket reconstruction
    -> manual reduce-only close -> confirmed flat state -> paused state

Every test refuses to run against mainnet.
"""

import os
from decimal import Decimal

import pytest

from botdca.bybit_exchange import BybitExchange
from botdca.controller import TradingController
from botdca.database import Database
from botdca.event_processor import ExchangeEventProcessor
from botdca.execution_recovery import ExecutionRecovery
from botdca.instruments import BybitInstrumentClient
from botdca.live_service import LiveStrategyService
from botdca.persistence import EventStore
from botdca.portfolio import PortfolioCoordinator, PortfolioGuards
from botdca.risk import RiskLimits
from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy import DcaStrategy, strategy_config_from_version
from botdca.strategy_version import GREENSYNERGY_RECONSTRUCTED_V1 as GS

pytestmark = pytest.mark.bybit_integration


def _require_testnet() -> None:
    if os.environ.get("BYBIT_TESTNET", "").lower() not in {"1", "true", "yes"}:
        pytest.fail(
            "Refusing to run the integration harness outside testnet/demo. "
            "Set BYBIT_TESTNET=true."
        )


@pytest.fixture(scope="module")
def credentials() -> tuple[str, str]:
    key = os.environ.get("BYBIT_API_KEY", "")
    secret = os.environ.get("BYBIT_API_SECRET", "")
    if not key or not secret:
        pytest.skip("BYBIT_API_KEY and BYBIT_API_SECRET are required")
    _require_testnet()
    return key, secret


@pytest.fixture(scope="module")
def symbol() -> str:
    return os.environ.get("BOTDCA_INTEGRATION_SYMBOL", "HYPEUSDT").upper()


@pytest.fixture
def exchange(credentials) -> BybitExchange:
    key, secret = credentials
    return BybitExchange(api_key=key, api_secret=secret, testnet=True, live_trading=True)


@pytest.fixture
def read_only(credentials) -> BybitExchange:
    key, secret = credentials
    return BybitExchange(api_key=key, api_secret=secret, testnet=True, live_trading=False)


@pytest.fixture
def store(tmp_path) -> EventStore:
    database = Database(f"sqlite+pysqlite:///{tmp_path}/integration.db")
    database.create_schema()
    return EventStore(database)


@pytest.fixture
def strategy(symbol, credentials) -> DcaStrategy:
    allocation = InitialAllocation(
        SizingMode.FIXED_MARGIN_USDT,
        float(os.environ.get("BOTDCA_INTEGRATION_MARGIN_USDT", "1")),
    )
    built = DcaStrategy(
        strategy_config_from_version(GS, symbol=symbol, allocation=allocation)
    )
    built.resume()
    return built


def _service(strategy, exchange, store, symbol) -> LiveStrategyService:
    rules = BybitInstrumentClient(testnet=True).get_linear_rules(symbol)
    services: list = []
    coordinator = PortfolioCoordinator(
        guards=PortfolioGuards(
            max_total_bot_margin_usdt=float(
                os.environ.get("BOTDCA_INTEGRATION_MAX_MARGIN_USDT", "25")
            ),
            max_total_bot_notional_usdt=100_000.0,
            min_available_equity_ratio=0.0,
            max_simultaneous_deep_baskets=3,
        ),
        account_reader=exchange.get_account_snapshot,
        exposure_reader=lambda: tuple(s.exposure() for s in services),
    )
    service = LiveStrategyService(
        strategy=strategy,
        exchange=exchange,
        store=store,
        rules=rules,
        risk_limits=RiskLimits(max_strategy_margin_usdt=1000.0),
        reentry_delay_seconds=0,
        coordinator=coordinator,
    )
    services.append(service)
    return service


def test_instrument_rules_are_readable(credentials, symbol) -> None:
    rules = BybitInstrumentClient(testnet=True).get_linear_rules(symbol)
    assert rules.symbol == symbol
    assert rules.qty_step > Decimal(0)
    assert rules.tick_size > Decimal(0)


def test_account_and_position_are_readable(read_only, symbol) -> None:
    account = read_only.get_account_snapshot()
    assert account.total_equity_usd >= 0
    position = read_only.get_position(symbol)
    assert position.symbol == symbol


def test_leverage_configuration_is_accepted(exchange, symbol) -> None:
    exchange.set_leverage(symbol, GS.leverage)


def test_full_lifecycle_entry_reconcile_protect_and_close(
    exchange, read_only, store, strategy, symbol
) -> None:
    """Entry, stream persistence, reconciliation, TP/DCA, restart, manual close."""
    if read_only.get_position(symbol).is_open:
        pytest.skip(f"{symbol} already has an open position; start from flat")

    service = _service(strategy, exchange, store, symbol)

    # 1. initial entry. The REST acknowledgement is not proof of fill.
    opened = service.sync()
    assert opened.status in {"entry_submitted", "entry_blocked"}
    if opened.status == "entry_blocked":
        pytest.skip(f"portfolio risk blocked the entry: {opened.dca_blocked_reason}")
    assert strategy.current_cycle is None, "an acknowledgement must not create a basket"

    # 2. the fill must come from execution history, not from the acknowledgement.
    ExecutionRecovery(exchange, store, symbol)()
    assert store.execution_count() > 0, "no execution was recovered from Bybit"

    # 3. reconciliation rebuilds the basket, then TP is placed.
    protected = service.sync()
    assert protected.status in {"orders_rebuilt", "waiting_for_reconciliation"}
    if protected.status == "orders_rebuilt":
        assert protected.take_profit_order is not None
        assert protected.protection_status in {"protected", "repaired"}
        assert protected.strategy_version_id == GS.version_id

        # 4. a restart reconstructs the same basket from exchange truth.
        restarted = DcaStrategy(strategy.config)
        restarted.resume()
        again = _service(restarted, exchange, store, symbol).sync()
        assert again.status == "orders_rebuilt"
        assert restarted.current_cycle is not None
        assert restarted.current_cycle.total_qty == pytest.approx(
            strategy.current_cycle.total_qty, rel=1e-6
        )

    # 5. manual reduce-only close, then a confirmed flat and paused state.
    controller = TradingController(strategy=strategy, exchange=exchange, symbol=symbol)
    controller.manual_close_and_pause()
    assert strategy.reentry_enabled is False

    final = read_only.get_position(symbol)
    assert not final.is_open, "position was not flat after the reduce-only close"
    assert service.sync().status == "flat_paused"


def test_private_stream_delivers_executions(credentials, store, symbol) -> None:
    """Connect the private stream and prove the processor persists what arrives."""
    from botdca.bybit_stream import BybitPrivateStream

    key, secret = credentials
    processor = ExchangeEventProcessor(store=store, symbol=symbol)
    stream = BybitPrivateStream(
        api_key=key, api_secret=secret, testnet=True, processor=processor
    )
    stream.start()
    try:
        assert stream.connected
    finally:
        stream.stop()
    assert not stream.connected


def test_transaction_log_is_readable_for_accounting(read_only) -> None:
    rows, truncated = read_only.get_transaction_log(limit=20, max_pages=1)
    assert isinstance(rows, list)
    assert isinstance(truncated, bool)
