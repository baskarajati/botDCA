from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from botdca.alerts import (
    AlertDispatcher,
    AlertSeverity,
    JournalAlertSink,
    MemoryAlertSink,
    WebhookAlertSink,
)
from botdca.bybit_exchange import BybitExchange
from botdca.bybit_stream import BybitPrivateStream
from botdca.config import Settings
from botdca.database import Database, WorkerLease
from botdca.event_processor import ExchangeEventProcessor
from botdca.execution_recovery import ExecutionRecovery
from botdca.instruments import BybitInstrumentClient
from botdca.live_service import LiveStrategyService
from botdca.operations import configuration_snapshot, live_configuration_errors
from botdca.persistence import EventStore
from botdca.portfolio import PortfolioCoordinator, SymbolExposure
from botdca.runtime import BotRuntime
from botdca.worker import LiveWorker


class LiveWorkerConfigurationError(RuntimeError):
    pass


def build_alert_dispatcher(settings: Settings, store: EventStore) -> AlertDispatcher:
    """Journal sink always; an optional vendor-neutral webhook when configured."""
    dispatcher = AlertDispatcher(
        [JournalAlertSink(store), MemoryAlertSink()],
        cooldown_seconds=settings.bot_alert_dedupe_seconds,
    )
    if settings.bot_alert_webhook_url:
        dispatcher.add_sink(
            WebhookAlertSink(
                settings.bot_alert_webhook_url,
                min_severity=AlertSeverity(settings.bot_alert_min_severity),
            )
        )
    return dispatcher


def build_portfolio_coordinator(
    settings: Settings,
    runtime: BotRuntime,
    *,
    account_reader,
) -> PortfolioCoordinator:
    """One coordinator per process, shared by every symbol's live service.

    `services` is filled in after construction because each service needs the
    coordinator's lock, and the coordinator needs each service's exposure.
    """
    services: list = []

    def exposure_reader() -> tuple[SymbolExposure, ...]:
        return tuple(service.exposure() for service in services)

    coordinator = PortfolioCoordinator(
        guards=runtime.portfolio_guards,
        account_reader=account_reader,
        exposure_reader=exposure_reader,
        lock=runtime.lock,
    )
    coordinator.services = services  # type: ignore[attr-defined]
    return coordinator


@dataclass
class LiveWorkerDeployment:
    worker: LiveWorker
    lease: WorkerLease

    def start(self) -> None:
        if not self.lease.acquire():
            raise LiveWorkerConfigurationError(
                "another live worker already holds the lease for this symbol"
            )
        try:
            self.worker.start()
        except Exception:
            self.lease.release()
            raise

    def stop(self) -> None:
        self.worker.stop()
        self.lease.release()


def build_live_worker_deployment(
    settings: Settings,
    runtime: BotRuntime,
    *,
    database_factory: Callable[[str], Database] = Database,
    exchange_factory: Callable[..., Any] = BybitExchange,
    instrument_client_factory: Callable[..., Any] = BybitInstrumentClient,
    stream_factory: Callable[..., Any] = BybitPrivateStream,
    coordinator: PortfolioCoordinator | None = None,
    alerts: AlertDispatcher | None = None,
) -> LiveWorkerDeployment:
    if not settings.bot_live_trading:
        raise LiveWorkerConfigurationError("BOT_LIVE_TRADING must be true")
    if not settings.bot_start_live_worker:
        raise LiveWorkerConfigurationError("BOT_START_LIVE_WORKER must be true")
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        raise LiveWorkerConfigurationError("Bybit API credentials are required")
    errors = live_configuration_errors(settings)
    if runtime.strategy.config.base_margin_usdt > runtime.risk_limits.max_strategy_margin_usdt:
        errors.append(
            f"{runtime.strategy.config.symbol} initial margin exceeds the new-order guard."
        )
    if errors:
        raise LiveWorkerConfigurationError(" ".join(errors))

    database = database_factory(settings.database_url)
    database.create_schema()
    store = EventStore(database)
    exchange = exchange_factory(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        testnet=settings.bybit_testnet,
        live_trading=True,
    )
    symbol = runtime.strategy.config.symbol.upper()
    rules = instrument_client_factory(testnet=settings.bybit_testnet).get_linear_rules(symbol)
    processor = ExchangeEventProcessor(store=store, symbol=symbol)
    stream = stream_factory(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        testnet=settings.bybit_testnet,
        processor=processor,
    )
    dispatcher = alerts or build_alert_dispatcher(settings, store)
    if coordinator is None:
        coordinator = build_portfolio_coordinator(
            settings,
            runtime,
            # Resolved lazily so construction never depends on the adapter being
            # able to read the account.
            account_reader=lambda: exchange.get_account_snapshot(),
        )
    service = LiveStrategyService(
        strategy=runtime.strategy,
        exchange=exchange,
        store=store,
        rules=rules,
        risk_limits=runtime.risk_limits,
        reentry_delay_seconds=settings.bot_reentry_delay_seconds,
        lock=runtime.lock,
        coordinator=coordinator,
        alerts=dispatcher,
        deep_dca_level=settings.bot_deep_dca_level,
    )
    # Register this symbol so portfolio exposure spans every running service.
    registered = getattr(coordinator, "services", None)
    if registered is not None and service not in registered:
        registered.append(service)
    worker = LiveWorker(
        service=service,
        stream=stream,
        store=store,
        interval_seconds=settings.bot_worker_interval_seconds,
        execution_recovery=ExecutionRecovery(exchange, store, symbol),
        alerts=dispatcher,
    )
    store.record_strategy_event(
        event_type="STRATEGY_CONFIG",
        symbol=symbol,
        payload={
            **configuration_snapshot(settings, runtime),
            "instrument_rules": {k: str(v) for k, v in vars(rules).items()},
            "strategy_version": (
                runtime.strategy_version.describe()
                if runtime.strategy_version is not None
                else None
            ),
            "portfolio_guards": asdict(runtime.portfolio_guards),
        },
    )
    return LiveWorkerDeployment(
        worker=worker,
        lease=WorkerLease(database, symbol),
    )
