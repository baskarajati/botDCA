import pytest
from fastapi.testclient import TestClient

from botdca import api
from botdca.config import Settings
from botdca.runtime import BotRuntime

TOKEN = "test-only-operator-token-more-than-32-chars"


class FakeInstrumentClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_linear_rules(self, symbol):
        from decimal import Decimal

        from botdca.instruments import InstrumentRules

        return InstrumentRules(
            symbol=symbol,
            tick_size=Decimal("0.01"),
            qty_step=Decimal("0.001"),
            min_order_qty=Decimal("0.001"),
            min_notional_value=Decimal(1),
            max_market_order_qty=Decimal(1000),
        )

    def list_linear_usdt_symbols(self):
        return ["BTCUSDT", "ETHUSDT", "HYPEUSDT", "ONDOUSDT", "DOGEUSDT"]


def _client(monkeypatch, tmp_path, **overrides):
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
        **overrides,
    )
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "runtime", BotRuntime(settings))
    monkeypatch.setattr(api, "BybitInstrumentClient", FakeInstrumentClient)
    return TestClient(
        api.app, base_url="http://localhost", headers={"X-Operator-Token": TOKEN}
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        yield c


def _slots(client, **overrides):
    base = [
        {"slot": 1, "enabled": True, "symbol": "HYPEUSDT", "initial_margin_usdt": 1.1},
        {"slot": 2, "enabled": True, "symbol": "ONDOUSDT", "initial_margin_usdt": 1.0},
        {"slot": 3, "enabled": False, "symbol": "DOGEUSDT", "initial_margin_usdt": 0.4},
    ]
    for index, patch in overrides.items():
        base[int(index)].update(patch)
    return client.put("/api/v1/configuration/strategy-slots", json={"slots": base})


def test_strategy_versions_expose_live_and_research_ladders_separately(client):
    body = client.get("/api/v1/strategy/versions").json()
    gs = body["versions"]["greensynergy-reconstructed-v1"]

    assert body["configured_version_id"] == "greensynergy-reconstructed-v1"
    assert gs["live_dca_triggers"] == [1.30, 1.85, 2.40, 2.70, 3.20, 3.60, 3.80, 4.00]
    assert gs["research_dca_triggers"] == [4.20, 4.40, 4.60, 4.80, 5.00]
    assert gs["max_live_dca_level"] == 8
    assert gs["research_ladder_is_live"] is False
    assert gs["experimental"] is True
    assert "zuya-reconstructed-v1" in body["versions"]
    assert "never traded" in body["research_ladder_notice"]


def test_forecast_reports_per_symbol_ladders_scenarios_and_labelled_stress(client):
    assert _slots(client).status_code == 200
    body = client.get("/api/v1/strategy/forecast").json()

    assert set(body["symbols"]) == {"HYPEUSDT", "ONDOUSDT"}
    hype = body["symbols"]["HYPEUSDT"]
    assert hype["max_live_dca_level"] == 8
    assert hype["max_research_dca_level"] == 13
    assert [row["level"] for row in hype["rows"] if row["research_only"]] == [
        9, 10, 11, 12, 13
    ]
    for row in hype["rows"]:
        assert {"incremental_qty", "cumulative_qty", "incremental_margin_usdt",
                "cumulative_margin_usdt", "position_notional_usdt",
                "weighted_average_entry"} <= set(row)

    names = [scenario["name"] for scenario in body["scenarios"]]
    assert "all coins DCA4" in names
    assert "all coins DCA8" in names
    assert body["worst_case"] is not None
    assert [p["drop_percent"] for p in body["worst_case"]["stress"]] == [5.0, 10.0, 20.0]
    assert "NOT AN EXACT BYBIT UTA LIQUIDATION" in body["worst_case"]["note"]
    assert "EXPERIMENTAL" in body["disclaimer"]

    expansion = body["research_expansion"]
    assert expansion["research_max_level"] == 13
    assert expansion["research_total_margin_usdt"] > expansion["live_total_margin_usdt"]


def test_portfolio_endpoint_reports_guards_and_aggregate_exposure(client):
    assert _slots(client).status_code == 200
    body = client.get("/api/v1/portfolio").json()

    guards = body["guards"]
    assert guards["max_total_bot_margin_usdt"] > 0
    assert guards["deep_dca_level"] >= 1
    assert guards["max_simultaneous_deep_baskets"] >= 0

    snapshot = body["snapshot"]
    assert {s["symbol"] for s in snapshot["symbols"]} == {"HYPEUSDT", "ONDOUSDT"}
    assert snapshot["total_bot_margin_usdt"] == 0.0
    assert snapshot["deep_basket_count"] == 0
    assert "one Bybit Unified Account" in body["note"]


def test_fixed_quantity_allocation_is_persisted_and_forecast_separately(client):
    response = _slots(
        client,
        **{"0": {"sizing_mode": "fixed_base_quantity", "sizing_value": 0.6}},
    )
    assert response.status_code == 200

    slots = response.json()["strategy_slots"]
    hype = next(slot for slot in slots if slot["symbol"] == "HYPEUSDT")
    assert hype["sizing_mode"] == "fixed_base_quantity"
    assert hype["allocation"]["value"] == 0.6
    assert hype["allocation"]["unit"] == "base quantity"

    forecast = client.get("/api/v1/strategy/forecast").json()["symbols"]["HYPEUSDT"]
    assert forecast["initial_qty"] == pytest.approx(0.6)
    assert forecast["allocation"]["mode"] == "fixed_base_quantity"

    ondo = next(slot for slot in slots if slot["symbol"] == "ONDOUSDT")
    assert ondo["sizing_mode"] == "fixed_margin_usdt"
    assert ondo["allocation"]["unit"] == "USDT margin"


def test_enabled_slots_must_share_one_strategy_family_version(client):
    response = _slots(
        client, **{"1": {"strategy_version_id": "zuya-reconstructed-v1"}}
    )
    assert response.status_code == 422
    assert "share one strategy-family version" in response.json()["detail"]


def test_an_unknown_strategy_version_is_refused(client):
    response = _slots(client, **{"0": {"strategy_version_id": "not-a-version"}})
    assert response.status_code == 422
    assert "unknown strategy version" in response.json()["detail"]


def test_saving_a_configuration_always_returns_it_to_draft(client):
    assert _slots(client).status_code == 200
    body = client.get("/api/v1/configuration/activation").json()
    assert body["gate"]["status"] == "draft"
    assert body["gate"]["allowed"] is False


def test_activation_advances_one_step_and_never_jumps_to_mainnet(client):
    assert _slots(client).status_code == 200

    jumped = client.put(
        "/api/v1/configuration/activation",
        json={"status": "approved_for_mainnet_trial"},
    )
    assert jumped.status_code == 409
    assert "one step at a time" in jumped.json()["detail"]

    assert client.put(
        "/api/v1/configuration/activation", json={"status": "validated"}
    ).json()["gate"]["status"] == "validated"
    assert client.put(
        "/api/v1/configuration/activation", json={"status": "approved_for_testnet"}
    ).json()["gate"]["status"] == "approved_for_testnet"


def test_an_unapproved_configuration_blocks_the_worker_but_not_the_console(
    monkeypatch, tmp_path
):
    """The one control that fixes a draft configuration must survive it.

    Advancing activation is only possible through this service. Refusing to
    start the API strands the operator, and the Compose restart policy then
    retries the same failure forever.
    """
    built = _client(monkeypatch, tmp_path)
    # _client pins the flag off, so arm it before lifespan runs.
    monkeypatch.setattr(api.settings, "bot_start_live_worker", True)
    with built as client:
        health = client.get("/health").json()
        assert health["live_worker_running"] is False
        assert "draft" in health["live_worker_blocked"]

        # The console is reachable, and so is the fix.
        assert client.get("/api/v1/operations").status_code == 200
        assert client.put(
            "/api/v1/configuration/activation", json={"status": "validated"}
        ).json()["gate"]["status"] == "validated"


def test_an_approved_configuration_reports_no_block(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/health").json()["live_worker_blocked"] is None


def test_alerts_endpoint_returns_persisted_alerts(client):
    store = api._event_store()
    store.record_alert(
        condition="max_dca_reached",
        severity="critical",
        symbol="HYPEUSDT",
        message="basket reached its live maximum",
        dedupe_key="max_dca_reached:HYPEUSDT:c1",
        cycle_id="c1",
    )
    body = client.get("/api/v1/alerts").json()

    assert body["alerts"][0]["condition"] == "max_dca_reached"
    assert body["open_critical_conditions"] == ["max_dca_reached"]
    assert body["webhook_configured"] is False


def test_accounting_never_infers_strategy_pnl_from_balance_change(client):
    from botdca.bybit_events import ExecutionEvent

    store = api._event_store()
    for index, (link, side, fee, pnl) in enumerate(
        [
            ("botdca-open-1", "Buy", 0.01, 0.0),
            ("botdca-dca-1", "Buy", 0.02, 0.0),
            ("botdca-tp-1", "Sell", 0.03, 1.5),
            ("manual-transfer", "Sell", 0.0, 500.0),
        ]
    ):
        store.record_execution(
            ExecutionEvent(
                execution_id=f"e{index}",
                order_id=f"o{index}",
                order_link_id=link,
                symbol="HYPEUSDT",
                side=side,
                price=80.0,
                qty=0.1,
                fee=fee,
                realized_pnl=pnl,
                execution_time_ms=1000 + index,
            )
        )

    body = client.get("/api/v1/accounting").json()
    hype = body["per_symbol"]["HYPEUSDT"]

    assert hype["entry_fees_usdt"] == pytest.approx(0.01)
    assert hype["dca_fees_usdt"] == pytest.approx(0.02)
    assert hype["close_fees_usdt"] == pytest.approx(0.03)
    assert hype["realized_pnl_usdt"] == pytest.approx(1.5)
    # The 500 USDT unattributed flow never enters strategy P&L.
    assert hype["unattributed_adjustments_usdt"] == pytest.approx(500.0)
    assert hype["net_pnl_usdt"] == pytest.approx(1.44)
    assert any("wallet-balance delta" in note for note in body["limitations"])


def test_operations_status_surfaces_portfolio_activation_and_versions(client):
    assert _slots(client).status_code == 200
    status = client.get("/api/v1/operations").json()

    assert "portfolio" in status
    assert "portfolio_guards" in status
    assert status["activation"]["status"] == "draft"
    assert "greensynergy-reconstructed-v1" in status["strategy_versions"]
    labels = [check["label"] for check in status["readiness"]]
    assert "Strategy activation" in labels
    assert "Position protection" in labels
    assert "Portfolio budget" in labels


def test_trial_mode_caps_active_symbols_and_ladder_depth(monkeypatch, tmp_path):
    with _client(
        monkeypatch,
        tmp_path,
        BOT_TRIAL_MODE=True,
        BOT_TRIAL_MAX_ACTIVE_SYMBOLS=1,
        BOT_TRIAL_MAX_DCA_LEVEL=4,
        BOT_TRIAL_MAX_PORTFOLIO_MARGIN_USDT=10,
    ) as client:
        blocked = _slots(client)
        assert blocked.status_code == 409
        assert "at most 1 active symbol" in blocked.json()["detail"]

        allowed = _slots(client, **{"1": {"enabled": False}})
        assert allowed.status_code == 200

        portfolio = client.get("/api/v1/portfolio").json()
        assert portfolio["trial_mode"]["enabled"] is True
        assert portfolio["trial_mode"]["max_dca_level"] == 4
        assert portfolio["guards"]["max_total_bot_margin_usdt"] == 10
        # Trial mode tightens the live ladder; it can never deepen it.
        assert client.get("/api/v1/bot/status").json()["max_dca_level"] == 4
        assert client.get("/api/v1/strategy/forecast").json()["max_live_dca_level"] == 4


def test_console_surfaces_portfolio_strategy_version_and_alerts(client):
    html = client.get("/").text
    js = client.get("/static/console.js").text

    for element in (
        "portfolio-margin",
        "portfolio-margin-cap",
        "portfolio-deep",
        "portfolio-intervention",
        "strategy-version-id",
        "strategy-live-ladder",
        "strategy-research-note",
        "alerts-rows",
    ):
        assert f'id="{element}"' in html, f"{element} missing from the console markup"
        assert element in js, f"{element} is never populated by the console script"

    assert "renderPortfolio" in js
    assert "renderStrategyVersion" in js
    assert "renderAlerts" in js


def test_operations_payload_contains_every_field_the_console_reads(client):
    assert _slots(client).status_code == 200
    status = client.get("/api/v1/operations").json()

    portfolio = status["portfolio"]
    for key in (
        "total_bot_margin_usdt",
        "total_bot_notional_usdt",
        "total_unrealized_pnl_usdt",
        "deep_basket_count",
        "available_balance_usdt",
        "available_equity_ratio",
    ):
        assert key in portfolio

    for key in (
        "max_total_bot_margin_usdt",
        "max_simultaneous_deep_baskets",
        "deep_dca_level",
    ):
        assert key in status["portfolio_guards"]

    assert "alerts" in status["journal"]
    assert "open_critical_alerts" in status
    assert status["bot"]["strategy_version_id"] == "greensynergy-reconstructed-v1"
    assert status["bot"]["sizing_mode"] == "fixed_margin_usdt"
    assert status["bot"]["basket_status"] == "flat"
    assert status["bot"]["max_dca_reached"] is False

    version = status["strategy_versions"]["greensynergy-reconstructed-v1"]
    assert version["experimental"] is True
    assert version["max_live_dca_level"] == 8
    assert len(version["research_dca_triggers"]) == 5


def test_every_symbol_worker_shares_one_portfolio_coordinator(monkeypatch, tmp_path):
    """A coordinator per symbol would only ever see its own exposure."""
    from decimal import Decimal

    from botdca import live_runtime
    from botdca.exchange import AccountSnapshot, OrderAck, PositionSnapshot

    class FakeExchange:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def get_position(self, symbol):
            return PositionSnapshot(symbol, "", 0.0, 0.0, 0.0, 80.0, None, 0.0)

        def get_account_snapshot(self):
            return AccountSnapshot(500.0, 500.0, 500.0, 400.0, 100.0, 1.0, 0.0, 0.2, 0.01)

        def get_last_price(self, symbol):
            return 80.0

        def get_open_orders(self, symbol):
            return []

        def set_leverage(self, symbol, leverage):
            return None

        def open_long(self, symbol, qty, *, order_link_id=None):
            return OrderAck("x", order_link_id or "botdca-open-x")

    class FakeStream:
        def __init__(self, **kwargs) -> None:
            self.connected = False

        def start(self):
            self.connected = True

        def stop(self):
            self.connected = False

    class FakeInstruments:
        def __init__(self, **kwargs) -> None:
            pass

        def get_linear_rules(self, symbol):
            from botdca.instruments import InstrumentRules

            return InstrumentRules(
                symbol, Decimal("0.01"), Decimal("0.001"),
                Decimal("0.001"), Decimal(1), Decimal(1000),
            )

    from botdca.config import Settings
    from botdca.database import Database

    settings = Settings(
        _env_file=None,
        BOT_LIVE_TRADING=True,
        BOT_START_LIVE_WORKER=True,
        BOT_OPERATOR_TOKEN=TOKEN,
        BOT_TRIAL_EQUITY_USDT=100,
        BYBIT_API_KEY="k",
        BYBIT_API_SECRET="s",
        BYBIT_TESTNET=True,
        DATABASE_URL="postgresql+psycopg://unused",
    )
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(live_runtime, "live_configuration_errors", lambda _s: [])

    def database_factory(_url):
        return Database(f"sqlite+pysqlite:///{tmp_path}/shared.db")

    runtimes = {
        symbol: BotRuntime(settings, symbol=symbol, base_margin_usdt=1.0, lock=api.RLock())
        for symbol in ("HYPEUSDT", "ONDOUSDT")
    }
    # All symbols must share the lock that the coordinator authorizes inside.
    shared_lock = next(iter(runtimes.values())).lock
    for configured in runtimes.values():
        configured._lock = shared_lock

    coordinator = None
    dispatcher = None
    services = []
    for configured in runtimes.values():
        deployment = live_runtime.build_live_worker_deployment(
            settings,
            configured,
            database_factory=database_factory,
            exchange_factory=lambda **kw: FakeExchange(**kw),
            instrument_client_factory=lambda **kw: FakeInstruments(**kw),
            stream_factory=lambda **kw: FakeStream(**kw),
            coordinator=coordinator,
            alerts=dispatcher,
        )
        service = deployment.worker.service
        services.append(service)
        coordinator = coordinator or service.coordinator
        dispatcher = dispatcher or service.alerts

    # One coordinator object, and it sees every symbol's exposure.
    assert services[0].coordinator is services[1].coordinator
    assert services[0].alerts is services[1].alerts
    snapshot = coordinator.snapshot()
    assert {exposure.symbol for exposure in snapshot.exposures} == {
        "HYPEUSDT",
        "ONDOUSDT",
    }
