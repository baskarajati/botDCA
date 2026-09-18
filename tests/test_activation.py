import pytest

from botdca.activation import (
    ActivationError,
    ActivationGate,
    ActivationStatus,
    can_transition,
    require_transition,
)


def test_a_freshly_edited_draft_cannot_jump_straight_to_mainnet() -> None:
    assert not can_transition(
        ActivationStatus.DRAFT, ActivationStatus.APPROVED_FOR_MAINNET_TRIAL
    )
    with pytest.raises(ActivationError, match="advance one step at a time"):
        require_transition(
            ActivationStatus.DRAFT, ActivationStatus.APPROVED_FOR_MAINNET_TRIAL
        )


def test_activation_advances_exactly_one_step_at_a_time() -> None:
    assert can_transition(ActivationStatus.DRAFT, ActivationStatus.VALIDATED)
    assert can_transition(ActivationStatus.VALIDATED, ActivationStatus.APPROVED_FOR_TESTNET)
    assert can_transition(
        ActivationStatus.APPROVED_FOR_TESTNET,
        ActivationStatus.APPROVED_FOR_MAINNET_TRIAL,
    )
    assert not can_transition(
        ActivationStatus.VALIDATED, ActivationStatus.APPROVED_FOR_MAINNET_TRIAL
    )


def test_stepping_down_and_retiring_are_always_allowed() -> None:
    assert can_transition(
        ActivationStatus.APPROVED_FOR_MAINNET_TRIAL, ActivationStatus.DRAFT
    )
    assert can_transition(ActivationStatus.DRAFT, ActivationStatus.RETIRED)
    assert can_transition(ActivationStatus.RETIRED, ActivationStatus.DRAFT)
    assert not can_transition(ActivationStatus.RETIRED, ActivationStatus.VALIDATED)


def test_mainnet_requires_mainnet_approval_not_merely_testnet_approval() -> None:
    gate = ActivationGate(
        status=ActivationStatus.APPROVED_FOR_TESTNET,
        testnet=False,
        mainnet_preflight_approved=True,
    )
    assert not gate.allowed
    assert gate.required_status is ActivationStatus.APPROVED_FOR_MAINNET_TRIAL
    assert any("approved_for_mainnet_trial is required" in e for e in gate.errors())


def test_testnet_accepts_testnet_approval() -> None:
    gate = ActivationGate(
        status=ActivationStatus.APPROVED_FOR_TESTNET,
        testnet=True,
        mainnet_preflight_approved=False,
    )
    assert gate.allowed
    assert gate.describe()["environment"] == "testnet"


def test_mainnet_still_requires_operator_preflight_approval() -> None:
    gate = ActivationGate(
        status=ActivationStatus.APPROVED_FOR_MAINNET_TRIAL,
        testnet=False,
        mainnet_preflight_approved=False,
    )
    assert not gate.allowed
    assert any("preflight" in e for e in gate.errors())


def test_a_retired_configuration_never_runs() -> None:
    gate = ActivationGate(
        status=ActivationStatus.RETIRED, testnet=True, mainnet_preflight_approved=True
    )
    assert not gate.allowed
    assert gate.errors() == ["This strategy configuration is retired."]
