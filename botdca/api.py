from dataclasses import asdict

from fastapi import FastAPI, HTTPException

from botdca.config import get_settings
from botdca.runtime import BotRuntime

settings = get_settings()
runtime = BotRuntime(settings)
app = FastAPI(title="botDCA", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/bot/status")
def bot_status() -> dict:
    return asdict(runtime.snapshot())


@app.post("/api/v1/bot/resume")
def resume_bot() -> dict:
    return asdict(runtime.resume())


@app.post("/api/v1/bot/pause")
def pause_bot() -> dict:
    return asdict(runtime.pause())


@app.post("/api/v1/bot/manual-close")
def manual_close() -> dict:
    if settings.bot_live_trading:
        raise HTTPException(
            status_code=501,
            detail="Live manual close is intentionally disabled until the Bybit executor is implemented.",
        )
    return asdict(runtime.manual_close_and_pause())
