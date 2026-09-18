import csv
import io
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from threading import Lock, RLock
from time import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from botdca.accounting import build_accounting
from botdca.activation import (
    ActivationGate,
    ActivationStatus,
    require_transition,
)
from botdca.activation import rank as activation_rank
from botdca.alerts import AlertCondition
from botdca.bybit_exchange import BybitApiError, BybitExchange
from botdca.config import get_settings
from botdca.controller import ControllerSafetyError, TradingController
from botdca.credentials import (
    BybitCredentials,
    CredentialValidationError,
    CredentialVaultError,
    EncryptedCredentialVault,
    validate_mainnet_credentials,
)
from botdca.dashboard import DASHBOARD_HTML
from botdca.database import Database
from botdca.forecast import (
    forecast_symbol,
    portfolio_scenarios,
    research_expansion,
    worst_case_scenario,
)
from botdca.instruments import BybitInstrumentClient
from botdca.live_runtime import LiveWorkerDeployment, build_live_worker_deployment
from botdca.operations import configuration_snapshot, live_configuration_errors
from botdca.persistence import EventStore
from botdca.portfolio import PortfolioSnapshot, SymbolExposure
from botdca.risk import evaluate_next_dca
from botdca.runtime import BotRuntime
from botdca.sizing import SizingMode
from botdca.strategy_slots import (
    StrategySlot,
    StrategySlotStore,
    default_strategy_slots,
    enabled_strategy_slots,
    serialized_slot,
    validate_strategy_slots,
)
from botdca.strategy_version import STRATEGY_VERSIONS

settings = get_settings()
runtime = BotRuntime(settings)
journal_initialization_lock = Lock()


class BybitCredentialSubmission(BaseModel):
    api_key: SecretStr = Field(min_length=5, max_length=256)
    api_secret: SecretStr = Field(min_length=8, max_length=512)
    confirm_mainnet: str = Field(min_length=1, max_length=32)


class StrategySlotSubmission(BaseModel):
    slot: int = Field(ge=1, le=3)
    enabled: bool
    symbol: str = Field(min_length=6, max_length=32)
    initial_margin_usdt: float = Field(gt=0, le=1_000_000)
    #: Optional. Defaults preserve the previous fixed-margin behaviour.
    sizing_mode: str = Field(default=str(SizingMode.FIXED_MARGIN_USDT), max_length=32)
    sizing_value: float | None = Field(default=None, gt=0, le=1_000_000)
    strategy_version_id: str | None = Field(default=None, max_length=64)


class StrategySlotsSubmission(BaseModel):
    slots: list[StrategySlotSubmission] = Field(min_length=3, max_length=3)


class ActivationSubmission(BaseModel):
    status: str = Field(max_length=32)


class AlertAcknowledgement(BaseModel):
    #: Limit to one configured symbol; omitted means every configured symbol.
    symbol: str | None = Field(default=None, max_length=32)
    #: Limit to these conditions; omitted means every condition.
    conditions: list[str] | None = Field(default=None, max_length=32)


def _make_runtimes(slots: tuple[StrategySlot, ...]) -> dict[str, BotRuntime]:
    # One lock shared by every symbol. It is the same critical section the
    # portfolio coordinator authorizes inside, so no two symbols can read the
    # account and submit against it concurrently.
    portfolio_lock = RLock()
    return {
        slot.symbol: BotRuntime(
            settings,
            symbol=slot.symbol,
            base_margin_usdt=slot.base_margin_usdt,
            lock=portfolio_lock,
            allocation=slot.allocation,
            strategy_version_id=slot.strategy_version_id,
        )
        for slot in enabled_strategy_slots(slots)
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    deployments: list[LiveWorkerDeployment] = []
    app.state.live_worker = None
    app.state.live_workers = {}
    app.state.portfolio_coordinator = None
    app.state.alert_dispatcher = None
    app.state.event_store = None
    app.state.journal_database = None
    app.state.strategy_slot_store = None
    slots = default_strategy_slots(settings.bot_symbol, settings.bot_base_margin_usdt)
    vault = EncryptedCredentialVault(
        settings.bot_credential_key_file,
        settings.bot_credential_store_path,
    )
    app.state.credential_vault = vault
    if vault.configured:
        credentials, _ = vault.load()
        settings.bybit_api_key = credentials.api_key
        settings.bybit_api_secret = credentials.api_secret
    try:
        database = Database(settings.database_url)
        database.create_schema()
        app.state.journal_database = database
        app.state.event_store = EventStore(database)
        slot_store = StrategySlotStore(database)
        app.state.strategy_slot_store = slot_store
        slots = slot_store.load(
            default_symbol=settings.bot_symbol,
            default_base_margin_usdt=settings.bot_base_margin_usdt,
        )
    except Exception:  # noqa: BLE001 - preview can show environment defaults if DB is offline
        slots = default_strategy_slots(settings.bot_symbol, settings.bot_base_margin_usdt)
    app.state.strategy_slots = slots
    app.state.strategy_runtimes = _make_runtimes(slots)
    if settings.bot_live_trading:
        errors = live_configuration_errors(settings)
        if errors:
            raise RuntimeError("Live configuration blocked: " + " ".join(errors))
    if settings.bot_start_live_worker:
        # A configuration must be explicitly approved for this exchange
        # environment before any worker starts. Saving a strategy is never
        # enough, and mainnet needs the operator preflight as well.
        gate = ActivationGate(
            status=min(
                (slot.activation_status for slot in enabled_strategy_slots(slots)),
                key=activation_rank,
                default=ActivationStatus.DRAFT,
            ),
            testnet=settings.bybit_testnet,
            mainnet_preflight_approved=settings.bot_mainnet_preflight_approved,
        )
        if not gate.allowed:
            raise RuntimeError("Live worker startup blocked: " + " ".join(gate.errors()))
        try:
            # ONE coordinator and ONE alert dispatcher for the whole process.
            # A coordinator per symbol would only ever see its own exposure, so
            # the portfolio guards would never bound the portfolio.
            runtimes = app.state.strategy_runtimes
            coordinator = None
            dispatcher = None
            for symbol, configured_runtime in runtimes.items():
                deployment = build_live_worker_deployment(
                    settings,
                    configured_runtime,
                    coordinator=coordinator,
                    alerts=dispatcher,
                )
                service = getattr(deployment.worker, "service", None)
                coordinator = coordinator or getattr(service, "coordinator", None)
                dispatcher = dispatcher or getattr(service, "alerts", None)
                deployment.start()
                deployments.append(deployment)
                app.state.live_workers[symbol] = deployment.worker
            app.state.portfolio_coordinator = coordinator
            app.state.alert_dispatcher = dispatcher
            app.state.live_worker = next(iter(app.state.live_workers.values()), None)
            if app.state.live_worker is not None:
                primary = getattr(app.state.live_worker, "service", None)
                app.state.event_store = getattr(primary, "store", app.state.event_store)
        except Exception:
            for deployment in reversed(deployments):
                deployment.stop()
            raise
    try:
        yield
    finally:
        for deployment in reversed(deployments):
            deployment.stop()
        app.state.live_worker = None
        app.state.live_workers = {}
        app.state.portfolio_coordinator = None
        app.state.alert_dispatcher = None
        database = getattr(app.state, "journal_database", None)
        if database is not None:
            database.engine.dispose()
        app.state.event_store = None
        app.state.credential_vault = None
        app.state.strategy_slot_store = None


app = FastAPI(title="botDCA", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.bot_allowed_hosts)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")


@app.exception_handler(RequestValidationError)
async def redacted_validation_error(_request: Request, _exc: RequestValidationError):
    return JSONResponse(
        {"detail": "Request validation failed. Check the submitted fields."},
        status_code=422,
    )


@app.middleware("http")
async def operator_boundary(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/v1/") and path != "/api/v1/session":
        token = settings.bot_operator_token
        if not token and (
            settings.bot_live_trading or settings.bybit_api_key or settings.bybit_api_secret
        ):
            return JSONResponse(
                {
                    "detail": "Configure BOT_OPERATOR_TOKEN before connecting a credentialed account."
                },
                status_code=503,
            )
        if token and not compare_digest(
            request.headers.get("X-Operator-Token", "").encode(), token.encode()
        ):
            return JSONResponse({"detail": "Enter the operator token to connect."}, status_code=401)
        if not token and request.method not in {"GET", "HEAD"}:
            return JSONResponse(
                {"detail": "Configure BOT_OPERATOR_TOKEN before using controls."}, status_code=403
            )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.get("/api/v1/session")
def session_status():
    return {
        "authentication_required": bool(settings.bot_operator_token),
        "controls_configured": bool(settings.bot_operator_token),
    }


def _event_store():
    with journal_initialization_lock:
        store = getattr(app.state, "event_store", None)
        if store is not None:
            return store
        database = None
        try:
            database = Database(settings.database_url)
            database.create_schema()
        except Exception as exc:
            if database is not None:
                database.engine.dispose()
            raise HTTPException(
                503, "Execution journal unavailable. Check DATABASE_URL and database service."
            ) from exc
        app.state.journal_database = database
        app.state.event_store = EventStore(database)
        return app.state.event_store


def _strategy_slots() -> tuple[StrategySlot, ...]:
    return getattr(
        app.state,
        "strategy_slots",
        default_strategy_slots(settings.bot_symbol, settings.bot_base_margin_usdt),
    )


def _strategy_runtimes() -> dict[str, BotRuntime]:
    configured = getattr(app.state, "strategy_runtimes", None)
    if configured:
        return configured
    return {runtime.strategy.config.symbol.upper(): runtime}


def _primary_runtime() -> BotRuntime:
    return next(iter(_strategy_runtimes().values()))


def _slot_configuration() -> list[dict]:
    primary = _primary_runtime()
    return [
        serialized_slot(
            slot,
            leverage=settings.bot_leverage,
            dca_steps=primary.strategy.config.dca_steps,
            max_dca_level=primary.risk_limits.max_dca_level,
        )
        for slot in _strategy_slots()
    ]


def _credential_vault() -> EncryptedCredentialVault:
    vault = getattr(app.state, "credential_vault", None)
    if vault is None:
        vault = EncryptedCredentialVault(
            settings.bot_credential_key_file,
            settings.bot_credential_store_path,
        )
        app.state.credential_vault = vault
    return vault


def _credential_vault_status() -> dict:
    vault = _credential_vault()
    status = vault.status()
    status["source"] = (
        "encrypted-vault"
        if status["persisted"]
        else "environment"
        if settings.bybit_api_key and settings.bybit_api_secret
        else "none"
    )
    return status


def _configuration_snapshot():
    return configuration_snapshot(
        settings,
        _primary_runtime(),
        credential_vault=_credential_vault_status(),
        strategy_slots=_slot_configuration(),
    )


def _audit_action(name, *, required=False, symbol: str | None = None):
    selected_runtime = _runtime_for_symbol(symbol)
    try:
        _event_store().record_strategy_event(
            event_type=name,
            symbol=selected_runtime.strategy.config.symbol,
            cycle_id=selected_runtime.snapshot().cycle_id,
            payload={
                "configuration": _configuration_snapshot(),
                "local_state": asdict(selected_runtime.snapshot()),
            },
        )
    except Exception as exc:
        if required:
            raise HTTPException(
                503, "Resume blocked: the execution journal must be available."
            ) from exc


def _runtime_for_symbol(symbol: str | None) -> BotRuntime:
    runtimes = _strategy_runtimes()
    if symbol is None:
        return next(iter(runtimes.values()))
    selected = runtimes.get(symbol.upper())
    if selected is None:
        raise HTTPException(404, f"Configured symbol {symbol.upper()} was not found.")
    return selected


def _worker_status(symbol: str | None = None):
    workers = getattr(app.state, "live_workers", {})
    worker = workers.get(symbol.upper()) if symbol and workers else None
    if symbol is None:
        worker = getattr(app.state, "live_worker", None)
    last_success = getattr(worker, "last_success_at_ms", None)
    fresh = last_success is not None and time() * 1000 - last_success < max(
        15000, settings.bot_worker_interval_seconds * 3000
    )
    result = getattr(worker, "last_result", None)
    configured_version = None
    try:
        configured_version = _runtime_for_symbol(symbol).strategy.config.strategy_version_id
    except Exception:  # noqa: BLE001 - status must render without a configured runtime
        configured_version = None
    return {
        "running": bool(worker is not None and worker.running),
        "stream_connected": bool(getattr(getattr(worker, "stream", None), "connected", False)),
        "last_success_at_ms": last_success,
        "fresh": fresh,
        "last_error": getattr(worker, "last_error", None),
        "last_sync_status": getattr(result, "status", None),
        "max_dca_reached": bool(getattr(result, "max_dca_reached", False)),
        "manual_intervention_required": bool(
            getattr(result, "manual_intervention_required", False)
        ),
        "protection_status": getattr(result, "protection_status", None),
        # Before the first sync there is no result yet; report the configured
        # version rather than None.
        "strategy_version_id": getattr(result, "strategy_version_id", None) or configured_version,
    }


def _all_worker_statuses() -> dict[str, dict]:
    return {symbol: _worker_status(symbol) for symbol in _strategy_runtimes()}


def _workers_running() -> bool:
    return any(status["running"] for status in _all_worker_statuses().values())


def _portfolio_live_configuration_errors() -> list[str]:
    errors = live_configuration_errors(settings)
    for configured_runtime in _strategy_runtimes().values():
        if (
            configured_runtime.strategy.config.base_margin_usdt
            > configured_runtime.risk_limits.max_strategy_margin_usdt
        ):
            errors.append(
                f"{configured_runtime.strategy.config.symbol} initial margin exceeds the "
                "new-order guard."
            )
    return errors


@app.get("/api/v1/operations")
def operations_status():
    runtimes = _strategy_runtimes()
    symbols = tuple(runtimes)
    journal = {"available": False, "executions": [], "events": []}
    try:
        store = _event_store()
        journal = {
            "available": True,
            "execution_count": store.execution_count_for_symbols(symbols),
            "executions": store.recent_executions_for_symbols(symbols),
            "events": store.recent_events_for_symbols(symbols),
            "alerts": store.recent_alerts(symbols, limit=25),
        }
    except Exception:  # noqa: BLE001 - isolate journal failures from other status sources
        journal["error"] = "Execution journal unavailable. Check the database service."
    try:
        account = account_status()
    except Exception:  # noqa: BLE001 - never disclose credential-bearing transport errors
        account = {
            "configured": bool(settings.bybit_api_key and settings.bybit_api_secret),
            "account": None,
            "position": None,
            "error": "Account read failed. Check credentials, exchange environment and connection.",
        }
    worker_statuses = _all_worker_statuses()
    worker = _worker_status()
    all_workers_ready = bool(worker_statuses) and all(
        status["running"]
        and status["stream_connected"]
        and status["fresh"]
        and not status["last_error"]
        for status in worker_statuses.values()
    )
    configuration_errors = _portfolio_live_configuration_errors()
    activation = _activation_gate(_strategy_slots())
    portfolio_guards = _primary_runtime().portfolio_guards
    portfolio = _portfolio_snapshot()
    open_critical = []
    if journal["available"]:
        try:
            open_critical = store.open_alert_conditions(symbols)
        except Exception:  # noqa: BLE001 - alert readout never blocks operations
            open_critical = []
    intervention = [
        symbol
        for symbol, status in worker_statuses.items()
        if status.get("manual_intervention_required")
    ]
    checks = [
        {
            "label": "Operator controls",
            "passed": len(settings.bot_operator_token) >= 32,
            "detail": "Random operator token of at least 32 characters required for live use.",
        },
        {
            "label": "Trial equity reference",
            "passed": settings.bot_trial_equity_usdt is not None,
            "detail": "Reference only; not a maximum-loss guarantee.",
        },
        {
            "label": "Execution journal",
            "passed": journal["available"],
            "detail": "Actual fills and configuration must be persisted.",
        },
        {
            "label": "Exchange account",
            "passed": account.get("account") is not None,
            "detail": "Read-only account and position checks; never place an order here.",
        },
        {
            "label": "Worker and private stream",
            "passed": all_workers_ready,
            "detail": "Every enabled symbol requires a recent reconciliation and connected stream.",
        },
        {
            "label": "Live configuration",
            "passed": not configuration_errors,
            "detail": " ".join(configuration_errors)
            or "Configuration checks passed; exchange validation is separate.",
        },
        {
            "label": "Strategy activation",
            "passed": activation.allowed,
            "detail": " ".join(activation.errors())
            or f"Configuration is {activation.status} for the {'testnet' if settings.bybit_testnet else 'mainnet'} environment.",
        },
        {
            "label": "Position protection",
            "passed": not intervention and "missing_take_profit" not in open_critical,
            "detail": (
                f"Manual intervention required for: {', '.join(intervention)}."
                if intervention
                else "Every open bot position must hold a valid exchange-hosted take profit."
            ),
        },
        {
            "label": "Portfolio budget",
            "passed": portfolio.total_margin_usdt
            <= portfolio_guards.max_total_bot_margin_usdt,
            "detail": (
                f"{portfolio.total_margin_usdt:.2f} of "
                f"{portfolio_guards.max_total_bot_margin_usdt:.2f} USDT committed across "
                f"{len(portfolio.exposures)} symbol(s); "
                f"{portfolio.deep_basket_count(portfolio_guards.deep_dca_level)} deep."
            ),
        },
    ]
    can_resume_live = all(c["passed"] for c in checks) and all(
        status["last_sync_status"]
        not in {
            "waiting_for_reconciliation",
            "entry_blocked",
            "max_dca_reached",
            "protection_ambiguous",
        }
        for status in worker_statuses.values()
    )
    bot_snapshots = [asdict(configured_runtime.snapshot()) for configured_runtime in runtimes.values()]
    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": ("testnet" if settings.bybit_testnet else "mainnet")
        if settings.bot_live_trading
        else "preview",
        "bot": bot_snapshots[0],
        "bots": bot_snapshots,
        "account": account,
        "worker": worker,
        "workers": worker_statuses,
        "journal": journal,
        "configuration": _configuration_snapshot(),
        "portfolio": portfolio.describe(portfolio_guards.deep_dca_level),
        "portfolio_guards": asdict(portfolio_guards),
        "activation": activation.describe(),
        "open_critical_alerts": open_critical,
        "strategy_versions": {
            version_id: version.describe()
            for version_id, version in sorted(STRATEGY_VERSIONS.items())
        },
        "readiness": checks,
        "can_resume": bool(settings.bot_operator_token)
        and journal["available"]
        and (not settings.bot_live_trading or can_resume_live),
    }


@app.get("/api/v1/journal/executions.csv")
def execution_export():
    store = _event_store()
    symbols = tuple(_strategy_runtimes())
    rows = store.recent_executions_for_symbols(symbols, limit=10000)
    output = io.StringIO()
    fields = [
        "execution_time_ms",
        "execution_id",
        "order_id",
        "order_link_id",
        "symbol",
        "side",
        "price",
        "qty",
        "fee",
        "realized_pnl",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in reversed(rows):
        # CSV cells supplied by exchange identifiers must not become spreadsheet formulas.
        writer.writerow(
            {
                k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                for k, v in row.items()
            }
        )
    execution_count = (
        store.symbol_execution_count(symbols[0])
        if len(symbols) == 1
        else store.execution_count_for_symbols(symbols)
    )
    truncated = execution_count > len(rows)
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="botdca-recent-executions.csv"',
            "X-Export-Truncated": str(truncated).lower(),
        },
    )


@app.get("/journal", response_class=HTMLResponse)
@app.get("/configuration", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/health")
def health() -> dict:
    workers = getattr(app.state, "live_workers", {})
    return {
        "status": "ok",
        "live_worker_enabled": settings.bot_start_live_worker,
        "live_worker_running": bool(workers) and all(worker.running for worker in workers.values()),
        "live_worker_has_error": any(worker.last_error for worker in workers.values()),
        "configured_symbols": list(_strategy_runtimes()),
    }


@app.get("/api/v1/bot/status")
def bot_status() -> dict:
    return asdict(_primary_runtime().snapshot())


def _bybit_exchange(*, live_trading: bool) -> BybitExchange:
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        raise HTTPException(
            status_code=503,
            detail="Bybit API credentials are not configured.",
        )
    return BybitExchange(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        testnet=settings.bybit_testnet,
        live_trading=live_trading,
    )


def _credential_transport_allowed(request: Request) -> bool:
    client_host = request.client.host if request.client is not None else ""
    forwarded_scheme = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    return (
        request.url.scheme == "https"
        or client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        or (settings.bot_trusted_https_proxy and forwarded_scheme == "https")
    )


@app.put("/api/v1/configuration/bybit-credentials")
def store_bybit_credentials(payload: BybitCredentialSubmission, request: Request) -> dict:
    if not _credential_transport_allowed(request):
        raise HTTPException(
            403,
            "Credential submission requires private HTTPS or a trusted loopback proxy.",
        )
    if payload.confirm_mainnet != "MAINNET":
        raise HTTPException(422, "Type MAINNET to confirm the production exchange environment.")
    if settings.bybit_testnet:
        raise HTTPException(409, "Disable BYBIT_TESTNET before validating mainnet credentials.")
    if _workers_running():
        raise HTTPException(409, "Pause deployment and stop the worker before rotating credentials.")
    vault = _credential_vault()
    if not vault.enabled:
        raise HTTPException(
            503,
            "The VPS credential vault is unavailable. Mount the deployment key first.",
        )

    api_key = payload.api_key.get_secret_value().strip()
    api_secret = payload.api_secret.get_secret_value().strip()
    if not api_key or not api_secret:
        raise HTTPException(422, "Enter both the Bybit API key and API secret.")
    exchange = BybitExchange(
        api_key=api_key,
        api_secret=api_secret,
        testnet=False,
        live_trading=False,
    )
    try:
        metadata = validate_mainnet_credentials(
            exchange=exchange,
            symbol=_primary_runtime().strategy.config.symbol,
            api_key=api_key,
        )
        for symbol in list(_strategy_runtimes())[1:]:
            exchange.get_position(symbol)
        vault.store(BybitCredentials(api_key, api_secret), metadata)
    except CredentialValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CredentialVaultError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502,
            "Bybit could not validate the credentials. Check the key, secret, IP binding and region.",
        ) from exc

    settings.bybit_api_key = api_key
    settings.bybit_api_secret = api_secret
    _audit_action("BYBIT_CREDENTIALS_VALIDATED")
    return {
        "status": "validated_and_stored",
        "credential": metadata,
        "worker_started": False,
        "live_trading_armed": False,
    }


@app.get("/api/v1/account/status")
def account_status() -> dict:
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        return {"configured": False, "account": None, "dca_risk": None}

    exchange = _bybit_exchange(live_trading=False)
    try:
        account = exchange.get_account_snapshot()
        positions = {
            symbol: exchange.get_position(symbol) for symbol in _strategy_runtimes()
        }
    except BybitApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    decisions = {
        symbol: asdict(
            evaluate_next_dca(
                configured_runtime.strategy,
                configured_runtime.risk_limits,
                account=account,
            )
        )
        for symbol, configured_runtime in _strategy_runtimes().items()
    }
    primary_symbol = next(iter(_strategy_runtimes()))
    return {
        "configured": True,
        "account": asdict(account),
        "position": asdict(positions[primary_symbol]),
        "positions": {symbol: asdict(position) for symbol, position in positions.items()},
        "dca_risk": decisions[primary_symbol],
        "dca_risks": decisions,
    }


@app.get("/api/v1/configuration/bybit-symbols")
def bybit_symbols() -> dict:
    try:
        symbols = BybitInstrumentClient(testnet=settings.bybit_testnet).list_linear_usdt_symbols()
    except Exception as exc:
        raise HTTPException(
            502, "Bybit symbol list is unavailable. Existing configuration is unchanged."
        ) from exc
    return {"symbols": symbols}


@app.put("/api/v1/configuration/strategy-slots")
def store_strategy_slots(payload: StrategySlotsSubmission) -> dict:
    if _workers_running():
        raise HTTPException(409, "Stop all live workers before changing strategy slots.")
    if any(configured_runtime.snapshot().position_qty > 0 for configured_runtime in _strategy_runtimes().values()):
        raise HTTPException(409, "Close or reconcile every open basket before changing strategy slots.")

    try:
        candidates = [
            StrategySlot(
                slot=item.slot,
                enabled=item.enabled,
                symbol=item.symbol,
                base_margin_usdt=item.initial_margin_usdt,
                sizing_mode=SizingMode(item.sizing_mode),
                sizing_value=item.sizing_value,
                strategy_version_id=(
                    item.strategy_version_id or settings.bot_strategy_version_id
                ),
                # Any edit to the traded parameters returns the configuration to
                # DRAFT. Saving can never make a strategy mainnet-live.
                activation_status=ActivationStatus.DRAFT,
            )
            for item in payload.slots
        ]
    except ValueError as exc:
        raise HTTPException(422, f"Unsupported sizing mode: {exc}") from exc
    try:
        validated = validate_strategy_slots(candidates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    enabled = enabled_strategy_slots(validated)
    if len(enabled) > settings.effective_max_active_symbols:
        raise HTTPException(
            409,
            f"Trial mode allows at most {settings.effective_max_active_symbols} active "
            f"symbol(s); {len(enabled)} are enabled.",
        )
    versions = {slot.strategy_version_id for slot in enabled}
    if len(versions) > 1:
        raise HTTPException(
            422,
            "Enabled slots must share one strategy-family version. "
            f"Found: {', '.join(sorted(versions))}.",
        )

    instrument_client = BybitInstrumentClient(testnet=settings.bybit_testnet)
    try:
        for slot in enabled_strategy_slots(validated):
            instrument_client.get_linear_rules(slot.symbol)
    except (LookupError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            422,
            "Bybit could not verify one of the enabled USDT perpetual symbols. "
            "Check the symbol and try again.",
        ) from exc

    if settings.bybit_api_key and settings.bybit_api_secret:
        exchange = _bybit_exchange(live_trading=False)
        checked_symbols = sorted(
            set(_strategy_runtimes())
            | {slot.symbol for slot in enabled_strategy_slots(validated)}
        )
        try:
            open_positions = [
                symbol for symbol in checked_symbols if exchange.get_position(symbol).is_open
            ]
            symbols_with_orders = [
                symbol for symbol in checked_symbols if exchange.get_open_orders(symbol)
            ]
        except BybitApiError as exc:
            raise HTTPException(
                502,
                "Strategy slots were not changed because exchange positions and orders "
                "could not be verified.",
            ) from exc
        if open_positions or symbols_with_orders:
            affected = sorted(set(open_positions) | set(symbols_with_orders))
            raise HTTPException(
                409,
                "Strategy slots were not changed. Reconcile or clear the position and open "
                f"orders for: {', '.join(affected)}.",
            )

    store = getattr(app.state, "strategy_slot_store", None)
    if store is None:
        try:
            database = Database(settings.database_url)
            database.create_schema()
            store = StrategySlotStore(database)
            app.state.strategy_slot_store = store
        except Exception as exc:
            raise HTTPException(
                503, "Strategy configuration could not be saved. Check the database service."
            ) from exc
    saved = store.replace(validated)
    app.state.strategy_slots = saved
    app.state.strategy_runtimes = _make_runtimes(saved)
    _audit_action("STRATEGY_SLOTS_UPDATED")
    return {
        "status": "strategy_slots_saved",
        "strategy_slots": _slot_configuration(),
        "worker_started": False,
        "live_trading_armed": False,
    }


@app.post("/api/v1/bot/resume")
def resume_bot() -> dict:
    return resume_configured_bot(None)


@app.post("/api/v1/bots/{symbol}/resume")
def resume_configured_bot(symbol: str | None) -> dict:
    if not operations_status()["can_resume"]:
        raise HTTPException(409, "Resume blocked. Resolve the readiness checks first.")
    selected_runtime = _runtime_for_symbol(symbol)
    _audit_action("OPERATOR_RESUME", required=True, symbol=selected_runtime.strategy.config.symbol)
    return asdict(selected_runtime.resume())


@app.post("/api/v1/bot/pause")
def pause_bot() -> dict:
    return pause_configured_bot(None)


@app.post("/api/v1/bots/{symbol}/pause")
def pause_configured_bot(symbol: str | None) -> dict:
    selected_runtime = _runtime_for_symbol(symbol)
    result = asdict(selected_runtime.pause())
    _audit_action("OPERATOR_PAUSE", symbol=selected_runtime.strategy.config.symbol)
    return result


def _live_controller(configured_runtime: BotRuntime) -> TradingController:
    exchange = _bybit_exchange(live_trading=True)
    return TradingController(
        strategy=configured_runtime.strategy,
        exchange=exchange,
        symbol=configured_runtime.strategy.config.symbol,
    )


@app.post("/api/v1/bot/manual-close")
def manual_close() -> dict:
    return manual_close_configured_bot(None)


@app.post("/api/v1/bots/{symbol}/manual-close")
def manual_close_configured_bot(symbol: str | None) -> dict:
    selected_runtime = _runtime_for_symbol(symbol)
    selected_symbol = selected_runtime.strategy.config.symbol
    _audit_action("OPERATOR_CLOSE_REQUESTED", symbol=selected_symbol)
    if not settings.bot_live_trading:
        return asdict(selected_runtime.manual_close_and_pause())

    controller = _live_controller(selected_runtime)
    try:
        with selected_runtime.lock:
            result = controller.manual_close_and_pause()
    except ControllerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BybitApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return asdict(result)


def _symbol_forecasts(reference_prices: dict[str, float] | None = None) -> dict:
    """Ladder forecasts for every enabled slot, research levels flagged."""
    forecasts = {}
    prices = reference_prices or {}
    for slot in enabled_strategy_slots(_strategy_slots()):
        version = STRATEGY_VERSIONS.get(slot.strategy_version_id)
        if version is None:
            continue
        forecasts[slot.symbol] = forecast_symbol(
            symbol=slot.symbol,
            version=version,
            allocation=slot.allocation,
            reference_price=prices.get(slot.symbol, 100.0),
            live_dca_cap=settings.effective_max_dca_level,
        )
    return forecasts


def _live_reference_prices() -> dict[str, float]:
    if not (settings.bybit_api_key and settings.bybit_api_secret):
        return {}
    try:
        exchange = _bybit_exchange(live_trading=False)
        return {symbol: exchange.get_last_price(symbol) for symbol in _strategy_runtimes()}
    except Exception:  # noqa: BLE001 - a forecast must still render without the exchange
        return {}


@app.get("/api/v1/strategy/versions")
def strategy_versions() -> dict:
    """Every known strategy version, with the live and research ladders separated."""
    return {
        "configured_version_id": settings.bot_strategy_version_id,
        "versions": {
            version_id: version.describe()
            for version_id, version in sorted(STRATEGY_VERSIONS.items())
        },
        "research_ladder_notice": (
            "Research-only DCA levels are never traded by the live runtime. They exist "
            "for historical comparison, capital forecasting and stress testing."
        ),
    }


@app.get("/api/v1/strategy/forecast")
def strategy_forecast() -> dict:
    """Per-symbol ladder forecasts plus combined portfolio scenarios and stress."""
    prices = _live_reference_prices()
    forecasts = _symbol_forecasts(prices)
    if not forecasts:
        raise HTTPException(503, "No enabled symbol has a resolvable strategy version.")

    equity = None
    try:
        account = account_status()
        equity = (account.get("account") or {}).get("total_equity_usd")
    except Exception:  # noqa: BLE001 - forecasts never depend on a live account read
        equity = settings.bot_trial_equity_usdt

    max_live = min(
        version.max_live_dca_level
        for version in (
            STRATEGY_VERSIONS[slot.strategy_version_id]
            for slot in enabled_strategy_slots(_strategy_slots())
            if slot.strategy_version_id in STRATEGY_VERSIONS
        )
    )
    max_live = min(max_live, settings.effective_max_dca_level)
    worst = worst_case_scenario(
        forecasts, max_live_dca_level=max_live, account_equity_usdt=equity
    )
    return {
        "reference_prices": prices,
        "reference_price_source": "exchange" if prices else "normalized (100.0)",
        "account_equity_usdt": equity,
        "max_live_dca_level": max_live,
        "symbols": {s: f.describe() for s, f in sorted(forecasts.items())},
        "scenarios": [
            scenario.describe()
            for scenario in portfolio_scenarios(
                forecasts, max_live_dca_level=max_live, account_equity_usdt=equity
            )
        ],
        "worst_case": worst.describe() if worst is not None else None,
        "research_expansion": research_expansion(forecasts, max_live_dca_level=max_live),
        "disclaimer": (
            "EXPERIMENTAL reconstruction. Forecasts are an APPROXIMATE STRESS MODEL, "
            "NOT AN EXACT BYBIT UTA LIQUIDATION CALCULATION."
        ),
    }


def _portfolio_snapshot() -> PortfolioSnapshot:
    """Aggregate bot exposure across every enabled symbol."""
    exposures = []
    for symbol, configured_runtime in _strategy_runtimes().items():
        snapshot = configured_runtime.snapshot()
        notional = (snapshot.average_entry or 0.0) * snapshot.position_qty
        exposures.append(
            SymbolExposure(
                symbol=symbol,
                dca_level=snapshot.dca_level,
                position_qty=snapshot.position_qty,
                margin_usdt=snapshot.committed_margin_usdt,
                notional_usdt=notional,
                strategy_version_id=snapshot.strategy_version_id,
                max_dca_reached=snapshot.max_dca_reached,
            )
        )
    account = None
    if settings.bybit_api_key and settings.bybit_api_secret:
        try:
            account = _bybit_exchange(live_trading=False).get_account_snapshot()
        except Exception:  # noqa: BLE001 - never disclose credential-bearing errors
            account = None
    return PortfolioSnapshot(
        account=account,
        exposures=tuple(exposures),
        reserved_margin_usdt=0.0,
        reserved_notional_usdt=0.0,
    )


@app.get("/api/v1/portfolio")
def portfolio_status() -> dict:
    """Account-level exposure and the guards it is evaluated against."""
    guards = _primary_runtime().portfolio_guards
    snapshot = _portfolio_snapshot()
    return {
        "guards": asdict(guards),
        "snapshot": snapshot.describe(guards.deep_dca_level),
        "trial_mode": {
            "enabled": settings.bot_trial_mode,
            "max_active_symbols": settings.effective_max_active_symbols,
            "max_dca_level": settings.effective_max_dca_level,
            "max_portfolio_margin_usdt": settings.effective_max_total_bot_margin_usdt,
            "manual_resume_after_restart": settings.bot_trial_manual_resume_after_restart,
            "pause_after_cycle": settings.effective_pause_after_cycle,
        },
        "note": (
            "Every enabled symbol draws on one Bybit Unified Account. Guards are "
            "evaluated across the portfolio, never per symbol."
        ),
    }


@app.get("/api/v1/accounting")
def accounting_status() -> dict:
    """Attributed strategy P&L. Never inferred from wallet-balance change."""
    store = _event_store()
    symbols = tuple(_strategy_runtimes())
    executions = store.recent_executions_for_symbols(symbols, limit=10000)

    transaction_log = None
    truncated = False
    if settings.bybit_api_key and settings.bybit_api_secret:
        try:
            transaction_log, truncated = _bybit_exchange(
                live_trading=False
            ).get_transaction_log(limit=100, max_pages=5)
        except Exception:  # noqa: BLE001 - accounting still renders without funding
            transaction_log = None

    accounting = build_accounting(
        executions=executions,
        transaction_log=transaction_log,
        tracked_symbols=symbols,
        window_truncated=truncated,
    )
    return accounting.describe()


@app.get("/api/v1/alerts")
def alerts_status(limit: int = 50) -> dict:
    store = _event_store()
    symbols = tuple(_strategy_runtimes())
    return {
        "alerts": store.recent_alerts(symbols, limit=min(max(limit, 1), 200)),
        "open_critical_conditions": store.open_alert_conditions(symbols),
        "webhook_configured": bool(settings.bot_alert_webhook_url),
        "dedupe_seconds": settings.bot_alert_dedupe_seconds,
    }


@app.post("/api/v1/alerts/acknowledge")
def acknowledge_alerts(payload: AlertAcknowledgement) -> dict:
    """Acknowledge open alerts. They stay in the audit history."""
    if payload.conditions:
        unknown = sorted(set(payload.conditions) - {str(c) for c in AlertCondition})
        if unknown:
            raise HTTPException(422, f"Unknown alert conditions: {', '.join(unknown)}")
    symbols = (
        (_runtime_for_symbol(payload.symbol).strategy.config.symbol,)
        if payload.symbol
        else tuple(_strategy_runtimes())
    )
    store = _event_store()
    acknowledged = store.acknowledge_alerts(symbols=symbols, conditions=payload.conditions)
    _audit_action("OPERATOR_ALERTS_ACKNOWLEDGED", symbol=symbols[0] if payload.symbol else None)
    return {
        "acknowledged": acknowledged,
        "open_critical_conditions": store.open_alert_conditions(symbols),
    }


def _activation_gate(slots: tuple[StrategySlot, ...]) -> ActivationGate:
    # The portfolio is only as approved as its least-approved enabled slot.
    weakest = min(
        (slot.activation_status for slot in enabled_strategy_slots(slots)),
        key=activation_rank,
        default=ActivationStatus.DRAFT,
    )
    return ActivationGate(
        status=weakest,
        testnet=settings.bybit_testnet,
        mainnet_preflight_approved=settings.bot_mainnet_preflight_approved,
    )


@app.get("/api/v1/configuration/activation")
def activation_status() -> dict:
    slots = _strategy_slots()
    return {
        "gate": _activation_gate(slots).describe(),
        "slots": {
            str(slot.slot): {
                "symbol": slot.symbol,
                "enabled": slot.enabled,
                "activation_status": str(slot.activation_status),
            }
            for slot in slots
        },
        "lifecycle": [
            "draft",
            "validated",
            "approved_for_testnet",
            "approved_for_mainnet_trial",
            "retired",
        ],
    }


@app.put("/api/v1/configuration/activation")
def advance_activation(payload: ActivationSubmission) -> dict:
    """Advance activation one step. Never a shortcut to mainnet."""
    if _workers_running():
        raise HTTPException(409, "Stop all live workers before changing activation.")
    try:
        target = ActivationStatus(payload.status)
    except ValueError as exc:
        raise HTTPException(422, f"Unknown activation status: {payload.status}") from exc

    slots = _strategy_slots()
    updated = []
    for slot in slots:
        if not slot.enabled:
            updated.append(slot)
            continue
        try:
            new_status = require_transition(slot.activation_status, target)
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        updated.append(
            StrategySlot(
                slot=slot.slot,
                enabled=slot.enabled,
                symbol=slot.symbol,
                base_margin_usdt=slot.base_margin_usdt,
                sizing_mode=slot.sizing_mode,
                sizing_value=slot.sizing_value,
                strategy_version_id=slot.strategy_version_id,
                activation_status=new_status,
            )
        )

    store = getattr(app.state, "strategy_slot_store", None)
    if store is None:
        raise HTTPException(503, "Activation could not be saved. Check the database service.")
    saved = store.replace(updated)
    app.state.strategy_slots = saved
    _audit_action("STRATEGY_ACTIVATION_CHANGED")
    return activation_status()
