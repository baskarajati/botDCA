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
