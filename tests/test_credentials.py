from __future__ import annotations

import stat

import pytest
from cryptography.fernet import Fernet

from botdca.credentials import (
    BybitCredentials,
    CredentialValidationError,
    CredentialVaultError,
    EncryptedCredentialVault,
    validate_mainnet_credentials,
)
from botdca.exchange import AccountSnapshot, PositionSnapshot


def _account() -> AccountSnapshot:
    return AccountSnapshot(100.0, 100.0, 100.0, 80.0, 20.0, 1.0, 0.0, 0.2, 0.01)


class ValidationExchange:
    def __init__(self, info: dict) -> None:
        self.info = info
        self.account_reads = 0
        self.position_symbols: list[str] = []

    def get_api_key_information(self) -> dict:
        return self.info

    def get_account_snapshot(self) -> AccountSnapshot:
        self.account_reads += 1
        return _account()

    def get_position(self, symbol: str) -> PositionSnapshot:
        self.position_symbols.append(symbol)
        return PositionSnapshot(symbol, "", 0.0, 0.0, 0.0, 80.0, None, 0.0)


def _valid_info() -> dict:
    return {
        "readOnly": 0,
        "uta": 1,
        "ips": ["203.0.113.10"],
        "permissions": {
            "ContractTrade": ["Order", "Position"],
            "Wallet": [],
        },
    }


def test_encrypted_vault_round_trip_and_permissions(tmp_path) -> None:
    key_path = tmp_path / "deployment.key"
    key_path.write_bytes(Fernet.generate_key())
    store_path = tmp_path / "private" / "bybit.enc"
    vault = EncryptedCredentialVault(key_path, store_path)

    vault.store(
        BybitCredentials("sensitive-key", "sensitive-secret"),
        {"validated_at": "2026-09-18T00:00:00+00:00"},
    )

    ciphertext = store_path.read_bytes()
    assert b"sensitive-key" not in ciphertext
    assert b"sensitive-secret" not in ciphertext
    credentials, metadata = vault.load()
    assert credentials.api_key == "sensitive-key"
    assert credentials.api_secret == "sensitive-secret"
    assert metadata["validated_at"] == "2026-09-18T00:00:00+00:00"
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store_path.parent.stat().st_mode) == 0o700


def test_encrypted_vault_rejects_wrong_key(tmp_path) -> None:
    first_key = tmp_path / "first.key"
    second_key = tmp_path / "second.key"
    first_key.write_bytes(Fernet.generate_key())
    second_key.write_bytes(Fernet.generate_key())
    store_path = tmp_path / "bybit.enc"
    EncryptedCredentialVault(first_key, store_path).store(
        BybitCredentials("api-key", "api-secret"), {}
    )

    with pytest.raises(CredentialVaultError, match="cannot be decrypted"):
        EncryptedCredentialVault(second_key, store_path).load()


def test_mainnet_validation_reads_account_and_position() -> None:
    exchange = ValidationExchange(_valid_info())

    metadata = validate_mainnet_credentials(
        exchange=exchange,
        symbol="HYPEUSDT",
        api_key="api-key",
    )

    assert metadata["key_fingerprint"]
    assert metadata["ip_binding_count"] == 1
    assert exchange.account_reads == 1
    assert exchange.position_symbols == ["HYPEUSDT"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"readOnly": 1}, "read-only"),
        ({"uta": 0}, "Unified Trading Account"),
        ({"ips": []}, "VPS public IP"),
        (
            {"permissions": {"ContractTrade": ["Position"], "Wallet": []}},
            "Order and Position",
        ),
        (
            {
                "permissions": {
                    "ContractTrade": ["Order", "Position"],
                    "Wallet": ["AccountTransfer"],
                }
            },
            "Remove every Wallet permission",
        ),
    ],
)
def test_mainnet_validation_rejects_unsafe_key(change, message) -> None:
    info = _valid_info()
    info.update(change)

    with pytest.raises(CredentialValidationError, match=message):
        validate_mainnet_credentials(
            exchange=ValidationExchange(info),
            symbol="HYPEUSDT",
            api_key="api-key",
        )
