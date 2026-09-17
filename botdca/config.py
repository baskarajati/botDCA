from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    bot_env: str = Field(default="development", alias="BOT_ENV")
    bot_live_trading: bool = Field(default=False, alias="BOT_LIVE_TRADING")
    bot_start_live_worker: bool = Field(default=False, alias="BOT_START_LIVE_WORKER")
    bot_worker_interval_seconds: float = Field(
        default=2.0,
        alias="BOT_WORKER_INTERVAL_SECONDS",
    )
    bot_symbol: str = Field(default="HYPEUSDT", alias="BOT_SYMBOL")
    bot_leverage: int = Field(default=24, alias="BOT_LEVERAGE")
    bot_base_margin_usdt: float = Field(default=1.0, alias="BOT_BASE_MARGIN_USDT")
    bot_tp_percent: float = Field(default=1.09, alias="BOT_TP_PERCENT")
    bot_reentry_delay_seconds: int = Field(default=30, alias="BOT_REENTRY_DELAY_SECONDS")
    bot_max_dca_level: int = Field(default=8, alias="BOT_MAX_DCA_LEVEL")
    bot_max_strategy_margin_usdt: float = Field(
        default=80.0,
        alias="BOT_MAX_STRATEGY_MARGIN_USDT",
    )
    bot_min_available_balance_usdt: float = Field(
        default=0.0,
        alias="BOT_MIN_AVAILABLE_BALANCE_USDT",
    )
    bot_min_available_equity_ratio: float = Field(
        default=0.20,
        alias="BOT_MIN_AVAILABLE_EQUITY_RATIO",
    )

    database_url: str = Field(
        default="postgresql+psycopg://botdca:botdca@db:5432/botdca",
        alias="DATABASE_URL",
    )
    bybit_api_key: str = Field(default="", alias="BYBIT_API_KEY")
    bybit_api_secret: str = Field(default="", alias="BYBIT_API_SECRET")
    bybit_testnet: bool = Field(default=False, alias="BYBIT_TESTNET")


@lru_cache
def get_settings() -> Settings:
    return Settings()
