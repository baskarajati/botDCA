import pytest
from cryptography.fernet import Fernet

from botdca import api
from botdca.config import Settings
from botdca.credentials import BybitCredentials, EncryptedCredentialVault


class FakeWorker:
    running = True
    last_error = None


class FakeDeployment:
    def __init__(self) -> None:
        self.worker = FakeWorker()
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_api_lifespan_explicitly_starts_and_stops_enabled_worker(monkeypatch) -> None:
    deployment = FakeDeployment()
    monkeypatch.setattr(api.settings, "bot_start_live_worker", True)
    monkeypatch.setattr(api, "build_live_worker_deployment", lambda settings, runtime: deployment)

    async with api.lifespan(api.app):
        assert deployment.started is True
        assert api.app.state.live_worker is deployment.worker

    assert deployment.stopped is True
    assert api.app.state.live_worker is None


@pytest.mark.asyncio
async def test_api_lifespan_leaves_worker_off_by_default(monkeypatch) -> None:
    monkeypatch.setattr(api.settings, "bot_start_live_worker", False)

    async with api.lifespan(api.app):
        assert api.app.state.live_worker is None


@pytest.mark.asyncio
async def test_api_lifespan_loads_persisted_credentials_before_worker_gate(
    monkeypatch, tmp_path
) -> None:
    key_path = tmp_path / "vault.key"
    store_path = tmp_path / "bybit.enc"
    key_path.write_bytes(Fernet.generate_key())
    EncryptedCredentialVault(key_path, store_path).store(
        BybitCredentials("persisted-key", "persisted-secret"),
        {"validated_at": "2026-09-18T00:00:00+00:00"},
    )
    settings = Settings(
        _env_file=None,
        BOT_CREDENTIAL_KEY_FILE=str(key_path),
        BOT_CREDENTIAL_STORE_PATH=str(store_path),
        BOT_START_LIVE_WORKER=False,
    )
    monkeypatch.setattr(api, "settings", settings)

    async with api.lifespan(api.app):
        assert settings.bybit_api_key == "persisted-key"
        assert settings.bybit_api_secret == "persisted-secret"
        assert api.app.state.live_worker is None
