"""Operator readiness and reproducible configuration, never strategy fitting."""

import json
from dataclasses import asdict
from hashlib import sha256


def configuration_snapshot(settings, runtime, *, credential_vault=None, strategy_slots=None):
    credential_metadata = credential_vault or {}
    configuration = {
        "strategy": {**asdict(runtime.strategy.config), "direction": "long-only"},
        "risk_limits": asdict(runtime.risk_limits),
        "portfolio_guards": asdict(runtime.portfolio_guards),
        "strategy_version": (
            runtime.strategy_version.describe()
            if getattr(runtime, "strategy_version", None) is not None
            else None
        ),
        "trial_mode": {
            "enabled": settings.bot_trial_mode,
            "max_active_symbols": settings.effective_max_active_symbols,
            "max_dca_level": settings.effective_max_dca_level,
            "max_portfolio_margin_usdt": settings.effective_max_total_bot_margin_usdt,
            "manual_resume_after_restart": settings.bot_trial_manual_resume_after_restart,
        },
        "reentry_delay_seconds": settings.bot_reentry_delay_seconds,
        "worker_interval_seconds": settings.bot_worker_interval_seconds,
        "exchange_environment": "testnet" if settings.bybit_testnet else "mainnet",
        "trial_equity_reference_usdt": settings.bot_trial_equity_usdt,
        "console": {
            "operator_token_configured": bool(settings.bot_operator_token),
            "allowed_hosts": list(settings.bot_allowed_hosts),
        },
        "exchange": {
            "api_key_configured": bool(settings.bybit_api_key),
            "api_secret_configured": bool(settings.bybit_api_secret),
            "account_mode": "Unified Trading Account",
            "credential_source": credential_metadata.get(
                "source",
                "environment" if settings.bybit_api_key and settings.bybit_api_secret else "none",
            ),
            "credential_vault_enabled": bool(credential_metadata.get("enabled", False)),
            "credential_vault_persisted": bool(credential_metadata.get("persisted", False)),
            "credential_validation": {
                key: credential_metadata[key]
                for key in (
                    "validated_at",
                    "key_fingerprint",
                    "ip_binding_count",
                    "uta",
                    "contract_order_permission",
                    "contract_position_permission",
                    "wallet_permissions",
                )
                if key in credential_metadata
            },
        },
        "runtime": {
            "environment": settings.bot_env,
            "live_trading": settings.bot_live_trading,
            "start_live_worker": settings.bot_start_live_worker,
            "mainnet_preflight_approved": settings.bot_mainnet_preflight_approved,
            "database_backend": settings.database_url.split(":", 1)[0],
        },
    }
    if strategy_slots is not None:
        configuration["strategy_slots"] = strategy_slots
        configuration["combined_full_ladder_margin_usdt"] = sum(
            slot["full_ladder_margin_usdt"]
            for slot in strategy_slots
            if slot["enabled"]
        )
    fingerprint = sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()[:16]
    return {"fingerprint": fingerprint, **configuration}


def live_configuration_errors(settings):
    errors = []
    if len(settings.bot_operator_token) < 32:
        errors.append("Set BOT_OPERATOR_TOKEN to a random token of at least 32 characters.")
    if settings.bot_trial_equity_usdt is None:
        errors.append("Set BOT_TRIAL_EQUITY_USDT to the operator-approved trial reference.")
    else:
        if settings.bot_max_strategy_margin_usdt > settings.bot_trial_equity_usdt:
            errors.append("Strategy margin cap exceeds the trial equity reference.")
        # The portfolio cap is the one that binds when several coins escalate
        # together, so it matters more than any single symbol's cap.
        if settings.effective_max_total_bot_margin_usdt > settings.bot_trial_equity_usdt:
            errors.append(
                "Portfolio margin cap exceeds the trial equity reference. "
                "Lower BOT_MAX_TOTAL_BOT_MARGIN_USDT."
            )
    if not 0 < settings.bot_base_margin_usdt <= settings.bot_max_strategy_margin_usdt:
        errors.append("Base margin must be positive and within the strategy margin cap.")
    if not settings.database_url.startswith("postgresql"):
        errors.append("Use PostgreSQL for the cross-process live worker lease.")
    if not settings.bybit_testnet and not settings.bot_mainnet_preflight_approved:
        errors.append("Mainnet preflight has not been approved by the operator.")
    return errors
