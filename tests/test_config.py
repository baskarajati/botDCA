from botdca.config import Settings


def test_database_url_can_be_loaded_from_mounted_secret(tmp_path) -> None:
    secret = tmp_path / "database_url"
    secret.write_text(
        "postgresql+psycopg://botdca:private-password@db:5432/botdca\n",
        encoding="utf-8",
    )

    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://ignored@db/botdca",
        DATABASE_URL_FILE=str(secret),
    )

    assert settings.database_url == (
        "postgresql+psycopg://botdca:private-password@db:5432/botdca"
    )


def test_operator_token_can_be_loaded_from_mounted_secret(tmp_path) -> None:
    secret = tmp_path / "operator_token"
    secret.write_text("x" * 48 + "\n", encoding="utf-8")

    settings = Settings(
        _env_file=None,
        BOT_OPERATOR_TOKEN="ignored",
        BOT_OPERATOR_TOKEN_FILE=str(secret),
    )

    assert settings.bot_operator_token == "x" * 48
    assert "x" * 48 not in repr(settings)


def test_blank_optional_numeric_env_values_are_treated_as_unset() -> None:
    from botdca.config import Settings

    settings = Settings(
        _env_file=None,
        BOT_TRIAL_EQUITY_USDT="",
        BOT_MAX_TOTAL_FLOATING_LOSS_USDT="",
    )
    assert settings.bot_trial_equity_usdt is None
    assert settings.bot_max_total_floating_loss_usdt is None


def test_shipped_env_example_loads() -> None:
    from botdca.config import Settings

    settings = Settings(_env_file=".env.example")
    assert settings.bot_strategy_version_id == "greensynergy-reconstructed-v1"
    assert settings.bot_live_trading is False
    assert settings.bot_start_live_worker is False
    assert settings.bot_mainnet_preflight_approved is False


def test_unknown_strategy_version_is_rejected_at_configuration_time() -> None:
    import pytest

    from botdca.config import Settings

    with pytest.raises(ValueError, match="is unknown"):
        Settings(_env_file=None, BOT_STRATEGY_VERSION_ID="not-a-version")


def test_alert_severity_is_validated_and_normalized() -> None:
    import pytest

    from botdca.config import Settings

    assert Settings(_env_file=None, BOT_ALERT_MIN_SEVERITY="CRITICAL").bot_alert_min_severity == (
        "critical"
    )
    with pytest.raises(ValueError, match="BOT_ALERT_MIN_SEVERITY"):
        Settings(_env_file=None, BOT_ALERT_MIN_SEVERITY="loud")


def test_trial_mode_only_ever_tightens_limits() -> None:
    from botdca.config import Settings

    loose = Settings(
        _env_file=None,
        BOT_TRIAL_MODE=True,
        BOT_MAX_DCA_LEVEL=8,
        BOT_TRIAL_MAX_DCA_LEVEL=4,
        BOT_MAX_TOTAL_BOT_MARGIN_USDT=240,
        BOT_TRIAL_MAX_PORTFOLIO_MARGIN_USDT=25,
    )
    assert loose.effective_max_dca_level == 4
    assert loose.effective_max_total_bot_margin_usdt == 25

    # A trial value larger than the real limit must not raise the real limit.
    tighter_real = Settings(
        _env_file=None,
        BOT_TRIAL_MODE=True,
        BOT_MAX_DCA_LEVEL=3,
        BOT_TRIAL_MAX_DCA_LEVEL=8,
        BOT_MAX_TOTAL_BOT_MARGIN_USDT=10,
        BOT_TRIAL_MAX_PORTFOLIO_MARGIN_USDT=500,
    )
    assert tighter_real.effective_max_dca_level == 3
    assert tighter_real.effective_max_total_bot_margin_usdt == 10
