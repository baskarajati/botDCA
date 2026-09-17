from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from botdca.bybit_exchange import BybitApiError, BybitExchange
from botdca.config import get_settings
from botdca.controller import ControllerSafetyError, TradingController
from botdca.dashboard import DASHBOARD_HTML
from botdca.live_runtime import LiveWorkerDeployment, build_live_worker_deployment
from botdca.risk import evaluate_next_dca
from botdca.runtime import BotRuntime

settings = get_settings()
runtime = BotRuntime(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    deployment: LiveWorkerDeployment | None = None
    app.state.live_worker = None
    if settings.bot_start_live_worker:
        deployment = build_live_worker_deployment(settings, runtime)
        deployment.start()
        app.state.live_worker = deployment.worker
    try:
        yield
    finally:
        if deployment is not None:
            deployment.stop()
        app.state.live_worker = None


app = FastAPI(title="botDCA", version="0.1.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/health")
def health() -> dict:
    worker = getattr(app.state, "live_worker", None)
    return {
        "status": "ok",
        "live_worker_enabled": settings.bot_start_live_worker,
        "live_worker_running": bool(worker is not None and worker.running),
        "live_worker_last_error": worker.last_error if worker is not None else None,
    }


@app.get("/api/v1/bot/status")
def bot_status() -> dict:
    return asdict(runtime.snapshot())


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


@app.get("/api/v1/account/status")
def account_status() -> dict:
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        return {"configured": False, "account": None, "dca_risk": None}

    exchange = _bybit_exchange(live_trading=False)
    try:
        account = exchange.get_account_snapshot()
    except BybitApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    decision = evaluate_next_dca(
        runtime.strategy,
        runtime.risk_limits,
        account=account,
    )
    return {
        "configured": True,
        "account": asdict(account),
        "dca_risk": asdict(decision),
    }


@app.post("/api/v1/bot/resume")
def resume_bot() -> dict:
    return asdict(runtime.resume())


@app.post("/api/v1/bot/pause")
def pause_bot() -> dict:
    return asdict(runtime.pause())


def _live_controller() -> TradingController:
    exchange = _bybit_exchange(live_trading=True)
    return TradingController(
        strategy=runtime.strategy,
        exchange=exchange,
        symbol=settings.bot_symbol,
    )


@app.post("/api/v1/bot/manual-close")
def manual_close() -> dict:
    if not settings.bot_live_trading:
        return asdict(runtime.manual_close_and_pause())

    controller = _live_controller()
    try:
        with runtime.lock:
            result = controller.manual_close_and_pause()
    except ControllerSafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BybitApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return asdict(result)
