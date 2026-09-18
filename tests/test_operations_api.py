import csv
import io

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from botdca import api
from botdca.bybit_events import ExecutionEvent
from botdca.config import Settings
from botdca.credentials import EncryptedCredentialVault
from botdca.exchange import AccountSnapshot, PositionSnapshot
from botdca.operations import configuration_snapshot, live_configuration_errors
from botdca.runtime import BotRuntime

TOKEN = "test-only-operator-token-more-than-32-chars"


@pytest.fixture
def client(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        BOT_LIVE_TRADING=False,
        BOT_START_LIVE_WORKER=False,
        BOT_OPERATOR_TOKEN=TOKEN,
        BOT_TRIAL_EQUITY_USDT=100,
        BYBIT_API_KEY="",
        BYBIT_API_SECRET="",
        BOT_CREDENTIAL_KEY_FILE=str(tmp_path / "vault.key"),
        BOT_CREDENTIAL_STORE_PATH=str(tmp_path / "vault" / "bybit.enc"),
        DATABASE_URL=f"sqlite+pysqlite:///{tmp_path}/journal.db",
    )
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "runtime", BotRuntime(settings))
    with TestClient(api.app, base_url="http://localhost", headers={"X-Operator-Token": TOKEN}) as c:
        yield c


def test_authentication_and_host_boundary(client):
    assert client.get("/api/v1/operations", headers={"X-Operator-Token": ""}).status_code == 401
    assert (
        client.post("/api/v1/bot/resume", headers={"X-Operator-Token": "wrong"}).status_code == 401
    )
    assert client.get("/api/v1/operations", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/").status_code == 200
    assert client.get("/configuration").status_code == 200
    assert client.get("/journal").status_code == 200
    assert 'href="/configuration"' in client.get("/").text
    assert 'href="/journal"' in client.get("/").text
    assert client.get("/static/console.js").status_code == 200
    assert client.get("/static/console.css").status_code == 200


def test_preview_operations_and_audited_controls(client):
    status = client.get("/api/v1/operations").json()
    assert status["mode"] == "preview"
    assert status["configuration"]["trial_equity_reference_usdt"] == 100
    assert status["configuration"]["strategy"]["tp_percent"] == 1.09
    assert status["configuration"]["exchange"]["api_key_configured"] is False
    assert status["configuration"]["exchange"]["api_secret_configured"] is False
    assert status["configuration"]["console"]["operator_token_configured"] is True
    assert status["configuration"]["runtime"]["live_trading"] is False
    assert status["worker"]["running"] is False
    assert status["journal"]["available"] is True
    assert status["journal"]["execution_count"] == 0
    assert status["can_resume"] is True  # Local state only, not live readiness.
    assert client.post("/api/v1/bot/resume").json()["state"] == "idle"
    assert client.post("/api/v1/bot/pause").json()["state"] == "paused"
    events = client.get("/api/v1/operations").json()["journal"]["events"]
    assert [e["event_type"] for e in events][:2] == ["OPERATOR_PAUSE", "OPERATOR_RESUME"]
    assert TOKEN not in str(events)


def test_blank_token_disables_mutations(client, monkeypatch):
    monkeypatch.setattr(api.settings, "bot_operator_token", "")
    assert client.post("/api/v1/bot/resume").status_code == 403


def test_journal_failure_blocks_resume_but_not_pause(client, monkeypatch):
    def unavailable():
        raise HTTPException(503, "unavailable")

    monkeypatch.setattr(api, "_event_store", unavailable)
    status = client.get("/api/v1/operations").json()
    assert status["journal"]["available"] is False
    assert status["can_resume"] is False
    assert client.post("/api/v1/bot/resume").status_code == 409
    assert client.post("/api/v1/bot/pause").status_code == 200


def test_live_resume_fails_without_worker_and_approval(client, monkeypatch):
    monkeypatch.setattr(api.settings, "bot_live_trading", True)
    assert client.post("/api/v1/bot/resume").status_code == 409


def test_csv_exports_actual_fills_and_escapes_formula_ids(client):
    store = api._event_store()
    event = ExecutionEvent(
        symbol="HYPEUSDT",
        order_id="=not-a-formula",
        order_link_id="botdca-open-test",
        execution_id="fill1",
        side="Buy",
        price=80.123,
        qty=0.25,
        fee=0.011,
        realized_pnl=0,
        execution_time_ms=1700000000123,
    )
    assert store.record_execution(event)
    assert not store.record_execution(event)
    response = client.get("/api/v1/journal/executions.csv")
    assert response.status_code == 200
    assert response.headers["X-Export-Truncated"] == "false"
    row = next(iter(csv.DictReader(io.StringIO(response.text))))
    assert row["price"] == "80.123"
    assert row["execution_time_ms"] == "1700000000123"
    assert row["order_id"] == "'=not-a-formula"
    assert client.get("/api/v1/operations").json()["journal"]["execution_count"] == 1


def test_csv_truncation_is_explicit(client, monkeypatch):
    store = api._event_store()
    monkeypatch.setattr(store, "symbol_execution_count", lambda symbol: 10001)
    assert client.get("/api/v1/journal/executions.csv").headers["X-Export-Truncated"] == "true"


def test_configuration_fingerprint_excludes_credentials():
    settings = Settings(
        _env_file=None, BYBIT_API_KEY="sensitive-key", BYBIT_API_SECRET="sensitive-secret"
    )
    first = configuration_snapshot(settings, BotRuntime(settings))
    second = configuration_snapshot(settings, BotRuntime(settings))
    assert first == second
    assert "sensitive" not in str(first)
    assert first["exchange"]["api_key_configured"] is True
    assert first["exchange"]["api_secret_configured"] is True
    assert first["strategy"]["direction"] == "long-only"
    assert len(first["strategy"]["dca_steps"]) == 8
    assert live_configuration_errors(settings)


def test_live_config_rejects_reference_overspend_and_missing_manual_gate():
    settings = Settings(_env_file=None, BOT_OPERATOR_TOKEN=TOKEN, BOT_TRIAL_EQUITY_USDT=50)
    errors = live_configuration_errors(settings)
    assert any("margin cap exceeds" in e for e in errors)
    assert any("preflight" in e for e in errors)


class FakeInstrumentClient:
    def __init__(self, **kwargs) -> None:
        pass

    def get_linear_rules(self, symbol):
        if symbol not in {"HYPEUSDT", "BTCUSDT", "ETHUSDT"}:
            raise LookupError(symbol)
        return object()

    def list_linear_usdt_symbols(self):
        return ["BTCUSDT", "ETHUSDT", "HYPEUSDT"]


def test_three_strategy_slots_are_saved_and_create_distinct_runtimes(client, monkeypatch):
    monkeypatch.setattr(api, "BybitInstrumentClient", FakeInstrumentClient)
    response = client.put(
        "/api/v1/configuration/strategy-slots",
        json={
            "slots": [
                {
                    "slot": 1,
                    "enabled": True,
                    "symbol": "HYPEUSDT",
                    "initial_margin_usdt": 1,
                },
                {
                    "slot": 2,
                    "enabled": True,
                    "symbol": "BTCUSDT",
                    "initial_margin_usdt": 2,
                },
                {
                    "slot": 3,
                    "enabled": True,
                    "symbol": "ETHUSDT",
                    "initial_margin_usdt": 3,
                },
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["worker_started"] is False
    status = client.get("/api/v1/operations").json()
    assert [bot["symbol"] for bot in status["bots"]] == [
        "HYPEUSDT",
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert [
        slot["base_margin_usdt"]
        for slot in status["configuration"]["strategy_slots"]
    ] == [1, 2, 3]
    slots = status["configuration"]["strategy_slots"]
    combined = status["configuration"]["combined_full_ladder_margin_usdt"]
    # The combined figure is the sum of every enabled slot's full-ladder margin,
    # and a geometric ladder commits far more than the initial margins alone.
    assert combined == pytest.approx(
        sum(slot["full_ladder_margin_usdt"] for slot in slots if slot["enabled"])
    )
    assert combined > 10 * sum(slot["base_margin_usdt"] for slot in slots if slot["enabled"])
    assert all(
        slot["strategy_version"]["version_id"] == "greensynergy-reconstructed-v1"
        for slot in slots
    )
    assert client.post("/api/v1/bots/BTCUSDT/resume").json()["state"] == "idle"
    assert client.post("/api/v1/bots/HYPEUSDT/pause").json()["state"] == "paused"


def test_strategy_slot_update_rejects_duplicate_enabled_symbol(client, monkeypatch):
    monkeypatch.setattr(api, "BybitInstrumentClient", FakeInstrumentClient)
    response = client.put(
        "/api/v1/configuration/strategy-slots",
        json={
            "slots": [
                {"slot": 1, "enabled": True, "symbol": "BTCUSDT", "initial_margin_usdt": 1},
                {"slot": 2, "enabled": True, "symbol": "btcusdt", "initial_margin_usdt": 2},
                {"slot": 3, "enabled": False, "symbol": "ETHUSDT", "initial_margin_usdt": 3},
            ]
        },
    )

    assert response.status_code == 422
    assert "configured more than once" in response.json()["detail"]


class FakeConfiguredAccountExchange:
    def get_position(self, symbol):
        size = 0.5 if symbol == "HYPEUSDT" else 0.0
        return PositionSnapshot(symbol, "Buy" if size else "", size, 80.0, 24.0, 80.0, None, 0.0)

    def get_open_orders(self, symbol):
        return []


def test_strategy_slot_update_checks_exchange_positions_before_saving(client, monkeypatch):
    monkeypatch.setattr(api, "BybitInstrumentClient", FakeInstrumentClient)
    monkeypatch.setattr(api.settings, "bybit_api_key", "configured")
    monkeypatch.setattr(api.settings, "bybit_api_secret", "configured")
    monkeypatch.setattr(
        api,
        "_bybit_exchange",
        lambda *, live_trading: FakeConfiguredAccountExchange(),
    )

    response = client.put(
        "/api/v1/configuration/strategy-slots",
        json={
            "slots": [
                {"slot": 1, "enabled": True, "symbol": "HYPEUSDT", "initial_margin_usdt": 1},
                {"slot": 2, "enabled": False, "symbol": "BTCUSDT", "initial_margin_usdt": 1},
                {"slot": 3, "enabled": False, "symbol": "ETHUSDT", "initial_margin_usdt": 1},
            ]
        },
    )

    assert response.status_code == 409
    assert "HYPEUSDT" in response.json()["detail"]


class FakeCredentialExchange:
    def __init__(self, **kwargs) -> None:
        assert kwargs["testnet"] is False
        assert kwargs["live_trading"] is False

    def get_api_key_information(self):
        return {
            "readOnly": 0,
            "uta": 1,
            "ips": ["203.0.113.10"],
            "permissions": {
                "ContractTrade": ["Order", "Position"],
                "Wallet": [],
            },
        }

    def get_account_snapshot(self):
        return AccountSnapshot(100.0, 100.0, 100.0, 80.0, 20.0, 1.0, 0.0, 0.2, 0.01)

    def get_position(self, symbol):
        return PositionSnapshot(symbol, "", 0.0, 0.0, 0.0, 80.0, None, 0.0)


def test_mainnet_credentials_are_validated_encrypted_and_sanitized(client, monkeypatch):
    key_path = api.settings.bot_credential_key_file
    with open(key_path, "wb") as key_file:
        key_file.write(Fernet.generate_key())
    monkeypatch.setattr(api, "BybitExchange", FakeCredentialExchange)

    response = client.put(
        "/api/v1/configuration/bybit-credentials",
        json={
            "api_key": "sensitive-key",
            "api_secret": "sensitive-secret",
            "confirm_mainnet": "MAINNET",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "validated_and_stored"
    assert body["worker_started"] is False
    assert body["live_trading_armed"] is False
    assert "sensitive" not in response.text
    vault = EncryptedCredentialVault(
        api.settings.bot_credential_key_file,
        api.settings.bot_credential_store_path,
    )
    assert b"sensitive" not in vault.store_path.read_bytes()
    status = client.get("/api/v1/operations").json()
    assert status["configuration"]["exchange"]["credential_source"] == "encrypted-vault"
    assert status["configuration"]["exchange"]["credential_vault_persisted"] is True
    assert "sensitive" not in str(status["journal"]["events"])


def test_mainnet_credential_confirmation_and_vault_are_required(client):
    payload = {
        "api_key": "candidate-key",
        "api_secret": "candidate-secret",
        "confirm_mainnet": "mainnet",
    }
    assert client.put("/api/v1/configuration/bybit-credentials", json=payload).status_code == 422
    payload["confirm_mainnet"] = "MAINNET"
    assert client.put("/api/v1/configuration/bybit-credentials", json=payload).status_code == 503


def test_invalid_credential_input_is_not_reflected(client):
    response = client.put(
        "/api/v1/configuration/bybit-credentials",
        json={
            "api_key": "abc",
            "api_secret": "sensitive-secret",
            "confirm_mainnet": "MAINNET",
        },
    )

    assert response.status_code == 422
    assert "abc" not in response.text
    assert "sensitive-secret" not in response.text


def test_mainnet_credential_submission_requires_secure_transport(client, monkeypatch):
    monkeypatch.setattr(api, "_credential_transport_allowed", lambda request: False)
    response = client.put(
        "/api/v1/configuration/bybit-credentials",
        json={
            "api_key": "candidate-key",
            "api_secret": "candidate-secret",
            "confirm_mainnet": "MAINNET",
        },
    )
    assert response.status_code == 403


def test_trusted_proxy_requires_forwarded_https(client, monkeypatch):
    monkeypatch.setattr(api.settings, "bot_trusted_https_proxy", True)
    request = type(
        "ProxyRequest",
        (),
        {
            "client": type("Client", (), {"host": "10.0.0.8"})(),
            "url": type("URL", (), {"scheme": "http"})(),
            "headers": {},
        },
    )()
    assert api._credential_transport_allowed(request) is False
    request.headers = {"X-Forwarded-Proto": "https"}
    assert api._credential_transport_allowed(request) is True


def test_operator_can_acknowledge_stale_alerts(client):
    store = api._event_store()
    for condition in ("worker_crashed", "private_stream_disconnected"):
        store.record_alert(
            condition=condition,
            severity="critical",
            symbol="HYPEUSDT",
            message="from an earlier incident",
            dedupe_key=f"{condition}:HYPEUSDT:-",
        )
    assert client.get("/api/v1/alerts").json()["open_critical_conditions"] == [
        "private_stream_disconnected",
        "worker_crashed",
    ]

    rejected = client.post("/api/v1/alerts/acknowledge", json={"conditions": ["nonsense"]})
    assert rejected.status_code == 422
    assert (
        client.post(
            "/api/v1/alerts/acknowledge", json={}, headers={"X-Operator-Token": "wrong"}
        ).status_code
        == 401
    )

    response = client.post(
        "/api/v1/alerts/acknowledge", json={"conditions": ["worker_crashed"]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "acknowledged": 1,
        "open_critical_conditions": ["private_stream_disconnected"],
    }
    response = client.post("/api/v1/alerts/acknowledge", json={"symbol": "HYPEUSDT"})
    assert response.json()["open_critical_conditions"] == []
    alerts = client.get("/api/v1/alerts").json()["alerts"]
    assert len(alerts) == 2 and all(row["acknowledged"] for row in alerts)
    events = [row["event_type"] for row in store.recent_events("HYPEUSDT", limit=5)]
    assert "OPERATOR_ALERTS_ACKNOWLEDGED" in events
