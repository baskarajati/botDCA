from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="", extra="ignore", allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    bot_env: str = Field(default="development", alias="BOT_ENV")
    bot_operator_token: str = Field(default="", alias="BOT_OPERATOR_TOKEN", repr=False)
    bot_operator_token_file: str = Field(default="", alias="BOT_OPERATOR_TOKEN_FILE")
    bot_allowed_hosts: list[str] = Field(
        default=["localhost", "127.0.0.1", "[::1]"],
        alias="BOT_ALLOWED_HOSTS",
    )
    bot_trial_equity_usdt: float | None = Field(default=None, gt=0, alias="BOT_TRIAL_EQUITY_USDT")
    bot_mainnet_preflight_approved: bool = Field(
        default=False, alias="BOT_MAINNET_PREFLIGHT_APPROVED"
    )
    bot_live_trading: bool = Field(default=False, alias="BOT_LIVE_TRADING")
    bot_start_live_worker: bool = Field(default=False, alias="BOT_START_LIVE_WORKER")
    bot_worker_interval_seconds: float = Field(
        default=2.0,
        gt=0,
        alias="BOT_WORKER_INTERVAL_SECONDS",
    )
    bot_symbol: str = Field(default="HYPEUSDT", alias="BOT_SYMBOL")
    bot_leverage: int = Field(default=24, ge=1, alias="BOT_LEVERAGE")
    bot_base_margin_usdt: float = Field(default=1.0, gt=0, alias="BOT_BASE_MARGIN_USDT")
    bot_tp_percent: float = Field(default=1.09, gt=0, alias="BOT_TP_PERCENT")
    bot_reentry_delay_seconds: int = Field(default=30, ge=0, alias="BOT_REENTRY_DELAY_SECONDS")
    bot_max_dca_level: int = Field(default=8, ge=0, le=8, alias="BOT_MAX_DCA_LEVEL")
    bot_max_strategy_margin_usdt: float = Field(
        default=80.0,
        gt=0,
        alias="BOT_MAX_STRATEGY_MARGIN_USDT",
    )
    bot_min_available_balance_usdt: float = Field(
        default=0.0,
        ge=0,
        alias="BOT_MIN_AVAILABLE_BALANCE_USDT",
    )
    bot_min_available_equity_ratio: float = Field(
        default=0.20,
        ge=0,
        lt=1,
        alias="BOT_MIN_AVAILABLE_EQUITY_RATIO",
    )

    # -- strategy family ------------------------------------------------
    bot_strategy_version_id: str = Field(
        default="greensynergy-reconstructed-v1",
        alias="BOT_STRATEGY_VERSION_ID",
    )

    # -- portfolio risk ---------------------------------------------------
    # Evaluated across every enabled symbol against one Bybit Unified Account.
    bot_max_total_bot_margin_usdt: float = Field(
        default=240.0, gt=0, alias="BOT_MAX_TOTAL_BOT_MARGIN_USDT"
    )
    bot_max_total_bot_notional_usdt: float = Field(
        default=6000.0, gt=0, alias="BOT_MAX_TOTAL_BOT_NOTIONAL_USDT"
    )
    bot_deep_dca_level: int = Field(default=5, ge=1, le=8, alias="BOT_DEEP_DCA_LEVEL")
    bot_max_simultaneous_deep_baskets: int = Field(
        default=1, ge=0, le=3, alias="BOT_MAX_SIMULTANEOUS_DEEP_BASKETS"
    )
    bot_max_total_floating_loss_usdt: float | None = Field(
        default=None, gt=0, alias="BOT_MAX_TOTAL_FLOATING_LOSS_USDT"
    )

    # -- first-trial mode -------------------------------------------------
    # A constrained configuration for the first live run. Limits are operator
    # configuration, never hardcoded recommendations.
    bot_trial_mode: bool = Field(default=False, alias="BOT_TRIAL_MODE")
    bot_trial_max_active_symbols: int = Field(
        default=1, ge=1, le=3, alias="BOT_TRIAL_MAX_ACTIVE_SYMBOLS"
    )
    bot_trial_max_dca_level: int = Field(
        default=4, ge=0, le=8, alias="BOT_TRIAL_MAX_DCA_LEVEL"
    )
    bot_trial_max_portfolio_margin_usdt: float = Field(
        default=25.0, gt=0, alias="BOT_TRIAL_MAX_PORTFOLIO_MARGIN_USDT"
    )
    bot_trial_manual_resume_after_restart: bool = Field(
        default=True, alias="BOT_TRIAL_MANUAL_RESUME_AFTER_RESTART"
    )

    # -- alerting ---------------------------------------------------------
    bot_alert_webhook_url: str = Field(default="", alias="BOT_ALERT_WEBHOOK_URL", repr=False)
    bot_alert_min_severity: str = Field(default="warning", alias="BOT_ALERT_MIN_SEVERITY")
    bot_alert_dedupe_seconds: float = Field(
        default=900.0, gt=0, alias="BOT_ALERT_DEDUPE_SECONDS"
    )

    database_url: str = Field(
        default="postgresql+psycopg://botdca:botdca@db:5432/botdca",
        alias="DATABASE_URL",
        repr=False,
    )
    database_url_file: str = Field(default="", alias="DATABASE_URL_FILE")
    bybit_api_key: str = Field(default="", alias="BYBIT_API_KEY", repr=False)
    bybit_api_secret: str = Field(default="", alias="BYBIT_API_SECRET", repr=False)
    bybit_testnet: bool = Field(default=False, alias="BYBIT_TESTNET")
    bot_credential_key_file: str = Field(
        default="/run/secrets/botdca_credential_key",
        alias="BOT_CREDENTIAL_KEY_FILE",
    )
    bot_credential_store_path: str = Field(
        default="/var/lib/botdca/secrets/bybit.enc",
        alias="BOT_CREDENTIAL_STORE_PATH",
    )
    bot_trusted_https_proxy: bool = Field(
        default=False,
        alias="BOT_TRUSTED_HTTPS_PROXY",
    )

    @property
    def effective_max_dca_level(self) -> int:
        """Trial mode may only ever reduce the live ladder depth, never raise it."""
        if self.bot_trial_mode:
            return min(self.bot_max_dca_level, self.bot_trial_max_dca_level)
        return self.bot_max_dca_level

    @property
    def effective_max_total_bot_margin_usdt(self) -> float:
        """Trial mode may only ever tighten the portfolio margin cap."""
        if self.bot_trial_mode:
            return min(
                self.bot_max_total_bot_margin_usdt,
                self.bot_trial_max_portfolio_margin_usdt,
            )
        return self.bot_max_total_bot_margin_usdt

    @property
    def effective_max_active_symbols(self) -> int:
        return self.bot_trial_max_active_symbols if self.bot_trial_mode else 3

    @model_validator(mode="after")
    def load_mounted_secrets(self):
        if self.bot_operator_token_file:
            token = Path(self.bot_operator_token_file).read_text(encoding="utf-8").strip()
            if not token:
                raise ValueError("BOT_OPERATOR_TOKEN_FILE is empty")
            self.bot_operator_token = token
        if self.database_url_file:
            value = Path(self.database_url_file).read_text(encoding="utf-8").strip()
            if not value:
                raise ValueError("DATABASE_URL_FILE is empty")
            self.database_url = value
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
