"""Configuration activation lifecycle.

A freshly edited experimental strategy must not become mainnet-live because
somebody pressed Save. Each configuration carries an explicit status, and each
transition is checked against the exchange environment the operator is running.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActivationStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED_FOR_TESTNET = "approved_for_testnet"
    APPROVED_FOR_MAINNET_TRIAL = "approved_for_mainnet_trial"
    RETIRED = "retired"


#: A save may only move a configuration one step along this path, and any edit
#: to the traded parameters drops it back to DRAFT.
_ORDER: tuple[ActivationStatus, ...] = (
    ActivationStatus.DRAFT,
    ActivationStatus.VALIDATED,
    ActivationStatus.APPROVED_FOR_TESTNET,
    ActivationStatus.APPROVED_FOR_MAINNET_TRIAL,
)


class ActivationError(RuntimeError):
    pass


def rank(status: ActivationStatus) -> int:
    if status is ActivationStatus.RETIRED:
        return -1
    return _ORDER.index(status)


def can_transition(current: ActivationStatus, target: ActivationStatus) -> bool:
    """Allow retiring, staying put, stepping down, or advancing exactly one step."""
    if target is ActivationStatus.RETIRED or current is target:
        return True
    if current is ActivationStatus.RETIRED:
        return target is ActivationStatus.DRAFT
    return rank(target) <= rank(current) + 1


def require_transition(current: ActivationStatus, target: ActivationStatus) -> ActivationStatus:
    if not can_transition(current, target):
        raise ActivationError(
            f"cannot move activation from {current} straight to {target}; "
            "advance one step at a time"
        )
    return target


@dataclass(frozen=True, slots=True)
class ActivationGate:
    """Whether a configuration may run live in the current environment."""

    status: ActivationStatus
    testnet: bool
    mainnet_preflight_approved: bool

    @property
    def required_status(self) -> ActivationStatus:
        return (
            ActivationStatus.APPROVED_FOR_TESTNET
            if self.testnet
            else ActivationStatus.APPROVED_FOR_MAINNET_TRIAL
        )

    def errors(self) -> list[str]:
        problems: list[str] = []
        if self.status is ActivationStatus.RETIRED:
            problems.append("This strategy configuration is retired.")
            return problems
        if rank(self.status) < rank(self.required_status):
            problems.append(
                f"Strategy configuration is {self.status}; "
                f"{self.required_status} is required for this exchange environment."
            )
        if not self.testnet and not self.mainnet_preflight_approved:
            problems.append("Mainnet preflight has not been approved by the operator.")
        return problems

    @property
    def allowed(self) -> bool:
        return not self.errors()

    def describe(self) -> dict:
        return {
            "status": str(self.status),
            "required_status": str(self.required_status),
            "environment": "testnet" if self.testnet else "mainnet",
            "allowed": self.allowed,
            "errors": self.errors(),
        }
