from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
from botdca.runtime import BotRuntime
from botdca.worker import LiveWorker


class LiveWorkerConfigurationError(RuntimeError):
    pass


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
    service = LiveStrategyService(
        strategy=runtime.strategy,
        exchange=exchange,
        store=store,
        rules=rules,
        risk_limits=runtime.risk_limits,
        reentry_delay_seconds=settings.bot_reentry_delay_seconds,
        lock=runtime.lock,
    )
    worker = LiveWorker(
        service=service,
        stream=stream,
        store=store,
        interval_seconds=settings.bot_worker_interval_seconds,
        execution_recovery=ExecutionRecovery(exchange, store, symbol),
    )
    store.record_strategy_event(
        event_type="STRATEGY_CONFIG",
        symbol=symbol,
        payload={
            **configuration_snapshot(settings, runtime),
            "instrument_rules": {k: str(v) for k, v in vars(rules).items()},
        },
    )
    return LiveWorkerDeployment(
        worker=worker,
        lease=WorkerLease(database, symbol),
    )
