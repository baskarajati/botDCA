from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CredentialVaultError(RuntimeError):
    pass


class CredentialValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BybitCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


class EncryptedCredentialVault:
    def __init__(self, key_path: str | Path, store_path: str | Path) -> None:
        self.key_path = Path(key_path)
        self.store_path = Path(store_path)

    @property
    def enabled(self) -> bool:
        return self.key_path.is_file()

    @property
    def configured(self) -> bool:
        return self.store_path.is_file()

    def _fernet(self) -> Fernet:
        if not self.enabled:
            raise CredentialVaultError("The VPS credential-vault key is not mounted.")
        try:
            key = self.key_path.read_bytes().strip()
            return Fernet(key)
        except (OSError, ValueError) as exc:
            raise CredentialVaultError("The VPS credential-vault key is invalid.") from exc

    def store(self, credentials: BybitCredentials, metadata: dict[str, Any]) -> None:
        if not credentials.api_key or not credentials.api_secret:
            raise CredentialVaultError("Both Bybit credential fields are required.")
        payload = json.dumps(
            {
                "version": 1,
                "api_key": credentials.api_key,
                "api_secret": credentials.api_secret,
                "metadata": metadata,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        ciphertext = self._fernet().encrypt(payload)
        parent = self.store_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        temporary = self.store_path.with_name(
            f".{self.store_path.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.store_path)
            os.chmod(self.store_path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def load(self) -> tuple[BybitCredentials, dict[str, Any]]:
        if not self.configured:
            raise CredentialVaultError("No encrypted Bybit credentials are stored.")
        try:
            plaintext = self._fernet().decrypt(self.store_path.read_bytes())
            payload = json.loads(plaintext)
            if payload.get("version") != 1:
                raise ValueError("unsupported credential payload")
            credentials = BybitCredentials(
                api_key=str(payload["api_key"]),
                api_secret=str(payload["api_secret"]),
            )
            metadata = dict(payload.get("metadata", {}))
        except (OSError, InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CredentialVaultError(
                "Stored Bybit credentials cannot be decrypted with the mounted key."
            ) from exc
        if not credentials.api_key or not credentials.api_secret:
            raise CredentialVaultError("Stored Bybit credentials are incomplete.")
        return credentials, metadata

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": self.enabled,
            "persisted": self.configured,
        }
        if self.configured and self.enabled:
            _, metadata = self.load()
            result.update(metadata)
        return result


def validate_mainnet_credentials(
    *,
    exchange: Any,
    symbol: str,
    api_key: str,
) -> dict[str, Any]:
    info = exchange.get_api_key_information()
    permissions = info.get("permissions") or {}
    contract_permissions = set(permissions.get("ContractTrade") or [])
    wallet_permissions = set(permissions.get("Wallet") or [])
    ips = [str(value) for value in info.get("ips") or [] if str(value)]
    errors: list[str] = []
    if int(info.get("readOnly", 1)) != 0:
        errors.append("The API key is read-only; Contract Trade order access is required.")
    missing = {"Order", "Position"} - contract_permissions
    if missing:
        errors.append("Enable Contract Trade Order and Position permissions.")
    if wallet_permissions:
        errors.append("Remove every Wallet permission, including transfers and withdrawals.")
    if int(info.get("uta", 0)) != 1:
        errors.append("Use an API key for a Unified Trading Account.")
    if not ips:
        errors.append("Bind the API key to the VPS public IP address.")
    if errors:
        raise CredentialValidationError(" ".join(errors))

    account = exchange.get_account_snapshot()
    exchange.get_position(symbol)
    return {
        "validated_at": datetime.now(UTC).isoformat(),
        "key_fingerprint": sha256(api_key.encode()).hexdigest()[:12],
        "ip_binding_count": len(ips),
        "uta": True,
        "contract_order_permission": True,
        "contract_position_permission": True,
        "wallet_permissions": False,
        "account_equity_observed": account.total_equity_usd is not None,
    }
