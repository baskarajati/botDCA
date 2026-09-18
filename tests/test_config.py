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
