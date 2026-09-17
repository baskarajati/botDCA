import pytest

from botdca import api


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
