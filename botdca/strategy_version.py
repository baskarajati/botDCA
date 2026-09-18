"""Versioned, explicitly labelled strategy families.

A strategy version is immutable. An open basket is pinned to the version it
opened with, so editing parameters can never silently change a live ladder.
Changing a parameter is a new version, and a new version only affects baskets
opened after it becomes the configured version.

Nothing in this module is validated or optimal. The GreenSynergy ladder is a
reconstruction from a public trader export and is labelled EXPERIMENTAL.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from botdca.domain import DEFAULT_DCA_STEPS, DcaStep


class StrategyStatus(StrEnum):
    """How much confidence the operator may place in a version."""

    EXPERIMENTAL = "experimental"
    RECONSTRUCTED = "reconstructed"
    VALIDATED = "validated"
    RETIRED = "retired"


class MaxDcaPolicy(StrEnum):
    """What happens when a basket reaches the configured live maximum."""

    HOLD_AND_ALERT = "hold_and_alert"


class ReentryMode(StrEnum):
    FIXED_DELAY = "fixed_delay"


@dataclass(frozen=True, slots=True)
class ReentryPolicy:
    """Explicit, versioned re-entry behaviour.

    GreenSynergy re-enters a symbol almost immediately after a basket closes
    (observed median roughly six seconds). The exact scheduler and admission
    rules are NOT proven by the trader export, so the production delay stays
    visible and configurable rather than hidden as a magic constant.
    """

    mode: ReentryMode = ReentryMode.FIXED_DELAY
    delay_seconds: float = 30.0
    observed_median_seconds: float | None = None
    reconstructed: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if self.delay_seconds < 0:
            raise ValueError("reentry delay_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    """One immutable parameter set for one strategy family."""

    version_id: str
    family: str
    status: StrategyStatus
    direction: str
    margin_mode: str
    leverage: int
    take_profit_percent: float
    dca_size_multiplier: float
    live_dca_triggers: tuple[float, ...]
    research_dca_triggers: tuple[float, ...] = ()
    reentry: ReentryPolicy = ReentryPolicy()
    max_dca_policy: MaxDcaPolicy = MaxDcaPolicy.HOLD_AND_ALERT
    trigger_reference: str = "weighted-average"
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.version_id or not self.family:
            raise ValueError("strategy version requires a version_id and family")
        if self.direction != "long":
            raise ValueError("only long-only strategy versions are supported")
        if self.margin_mode not in {"cross", "isolated"}:
            raise ValueError("margin_mode must be cross or isolated")
        if self.leverage < 1:
            raise ValueError("leverage must be at least 1")
        if self.take_profit_percent <= 0:
            raise ValueError("take_profit_percent must be positive")
        if self.dca_size_multiplier <= 1:
            raise ValueError("dca_size_multiplier must be greater than 1")
        if not self.live_dca_triggers:
            raise ValueError("a strategy version needs at least one live DCA trigger")
        if any(trigger <= 0 for trigger in self.live_dca_triggers):
            raise ValueError("DCA triggers must be positive percentage drops")
        if any(trigger <= 0 for trigger in self.research_dca_triggers):
            raise ValueError("research DCA triggers must be positive percentage drops")

    @property
    def max_live_dca_level(self) -> int:
        """Deepest DCA level this version may reach autonomously."""
        return len(self.live_dca_triggers)

    @property
    def max_research_dca_level(self) -> int:
        """Deepest level available for research, forecasting and stress only."""
        return len(self.live_dca_triggers) + len(self.research_dca_triggers)

    def live_dca_steps(self) -> tuple[DcaStep, ...]:
        """The only ladder the live runtime may ever trade."""
        return tuple(
            DcaStep(trigger, self.dca_size_multiplier) for trigger in self.live_dca_triggers
        )

    def research_dca_steps(self) -> tuple[DcaStep, ...]:
        """Live ladder plus the research-only continuation.

        NEVER pass this into a live strategy or order planner. It exists for
        historical comparison, capital forecasting and stress testing.
        """
        return tuple(
            DcaStep(trigger, self.dca_size_multiplier)
            for trigger in (*self.live_dca_triggers, *self.research_dca_triggers)
        )

    def is_research_only_level(self, level: int) -> bool:
        return level > self.max_live_dca_level

    def with_reentry_delay(self, delay_seconds: float) -> StrategyVersion:
        """Derive a sibling version. The caller must give it a new version_id."""
        return replace(self, reentry=replace(self.reentry, delay_seconds=delay_seconds))

    def describe(self) -> dict:
        return {
            "version_id": self.version_id,
            "family": self.family,
            "status": str(self.status),
            "experimental": self.status
            in {StrategyStatus.EXPERIMENTAL, StrategyStatus.RECONSTRUCTED},
            "direction": self.direction,
            "margin_mode": self.margin_mode,
            "leverage": self.leverage,
            "take_profit_percent": self.take_profit_percent,
            "dca_size_multiplier": self.dca_size_multiplier,
            "trigger_reference": self.trigger_reference,
            "live_dca_triggers": list(self.live_dca_triggers),
            "max_live_dca_level": self.max_live_dca_level,
            "research_dca_triggers": list(self.research_dca_triggers),
            "max_research_dca_level": self.max_research_dca_level,
            "research_ladder_is_live": False,
            "max_dca_policy": str(self.max_dca_policy),
            "reentry": {
                "mode": str(self.reentry.mode),
                "delay_seconds": self.reentry.delay_seconds,
                "observed_median_seconds": self.reentry.observed_median_seconds,
                "reconstructed": self.reentry.reconstructed,
                "note": self.reentry.note,
            },
            "summary": self.summary,
        }


GREENSYNERGY_RECONSTRUCTED_V1 = StrategyVersion(
    version_id="greensynergy-reconstructed-v1",
    family="greensynergy",
    status=StrategyStatus.EXPERIMENTAL,
    direction="long",
    margin_mode="cross",
    leverage=24,
    take_profit_percent=1.09,
    dca_size_multiplier=1.42,
    # Percentage drops from the CURRENT weighted-average entry, DCA1..DCA8.
    live_dca_triggers=(1.30, 1.85, 2.40, 2.70, 3.20, 3.60, 3.80, 4.00),
    # DCA9..DCA13. Research, historical comparison, capital forecasting and
    # stress testing only. The live runtime never reaches these levels.
    research_dca_triggers=(4.20, 4.40, 4.60, 4.80, 5.00),
    reentry=ReentryPolicy(
        mode=ReentryMode.FIXED_DELAY,
        delay_seconds=30.0,
        observed_median_seconds=6.0,
        reconstructed=True,
        note=(
            "GreenSynergy re-enters a symbol almost immediately after a basket closes "
            "(observed median about six seconds, roughly 98-99% within five minutes). "
            "The exact scheduler and admission rules are not proven by the trader "
            "export, so the production delay stays explicit and configurable."
        ),
    ),
    summary=(
        "EXPERIMENTAL reconstruction of the GreenSynergy long-only Cross 24x multi-coin "
        "DCA portfolio. Not validated, not optimal and not guaranteed profitable. "
        "Autonomous live depth is capped at DCA8; DCA9-DCA13 are research-only."
    ),
)


ZUYA_RECONSTRUCTED_V1 = StrategyVersion(
    version_id="zuya-reconstructed-v1",
    family="zuya",
    status=StrategyStatus.RECONSTRUCTED,
    direction="long",
    margin_mode="cross",
    leverage=24,
    take_profit_percent=1.09,
    # Zuya's per-level multipliers are not uniform, so the scalar multiplier here
    # is only a nominal value. `live_dca_steps` is overridden below to preserve
    # the exact PR #1..#3 ladder.
    dca_size_multiplier=1.42,
    live_dca_triggers=tuple(step.drop_percent_from_average for step in DEFAULT_DCA_STEPS),
    research_dca_triggers=(),
    reentry=ReentryPolicy(
        mode=ReentryMode.FIXED_DELAY,
        delay_seconds=30.0,
        observed_median_seconds=None,
        reconstructed=True,
        note="Zuya autonomous entry and re-entry behaviour was not validated in PR #3.",
    ),
    summary=(
        "Legacy reconstruction derived mainly from Zuya HYPEUSDT. Preserved so existing "
        "behaviour and PR #3 historical validation remain reproducible."
    ),
)


def zuya_live_dca_steps() -> tuple[DcaStep, ...]:
    """Zuya keeps its original non-uniform per-level size multipliers."""
    return DEFAULT_DCA_STEPS


STRATEGY_VERSIONS: dict[str, StrategyVersion] = {
    GREENSYNERGY_RECONSTRUCTED_V1.version_id: GREENSYNERGY_RECONSTRUCTED_V1,
    ZUYA_RECONSTRUCTED_V1.version_id: ZUYA_RECONSTRUCTED_V1,
}

DEFAULT_STRATEGY_VERSION_ID = GREENSYNERGY_RECONSTRUCTED_V1.version_id


def get_strategy_version(version_id: str) -> StrategyVersion:
    version = STRATEGY_VERSIONS.get(version_id)
    if version is None:
        known = ", ".join(sorted(STRATEGY_VERSIONS))
        raise KeyError(f"unknown strategy version {version_id!r}; known versions: {known}")
    return version


def live_dca_steps_for(version: StrategyVersion) -> tuple[DcaStep, ...]:
    """Resolve the live ladder, preserving Zuya's non-uniform multipliers."""
    if version.version_id == ZUYA_RECONSTRUCTED_V1.version_id:
        return zuya_live_dca_steps()
    return version.live_dca_steps()


def research_dca_steps_for(version: StrategyVersion) -> tuple[DcaStep, ...]:
    if version.version_id == ZUYA_RECONSTRUCTED_V1.version_id:
        return zuya_live_dca_steps()
    return version.research_dca_steps()
