from __future__ import annotations

from hashlib import sha256


def deterministic_order_link_id(role: str, *parts: object) -> str:
    """Return a stable Bybit-safe identity for one logical order intent."""
    normalized_role = "".join(character for character in role.lower() if character.isalnum())[:8]
    if not normalized_role:
        raise ValueError("order role must contain an alphanumeric character")
    payload = "|".join(str(part) for part in parts)
    digest = sha256(payload.encode()).hexdigest()[:20]
    return f"botdca-{normalized_role}-{digest}"[:36]
