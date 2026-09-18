import pytest

from botdca.domain import BasketStatus
from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy import DcaStrategy, strategy_config_from_version
from botdca.strategy_version import (
    GREENSYNERGY_RECONSTRUCTED_V1,
    ZUYA_RECONSTRUCTED_V1,
    StrategyStatus,
    get_strategy_version,
    live_dca_steps_for,
    research_dca_steps_for,
)

GS = GREENSYNERGY_RECONSTRUCTED_V1


def test_greensynergy_live_ladder_is_exactly_the_eight_reconstructed_triggers() -> None:
    assert GS.live_dca_triggers == (1.30, 1.85, 2.40, 2.70, 3.20, 3.60, 3.80, 4.00)
    assert GS.max_live_dca_level == 8
    assert GS.leverage == 24
    assert GS.margin_mode == "cross"
    assert GS.direction == "long"
    assert GS.take_profit_percent == pytest.approx(1.09)
    assert GS.dca_size_multiplier == pytest.approx(1.42)


def test_research_ladder_is_separate_and_never_part_of_the_live_ladder() -> None:
    assert GS.research_dca_triggers == (4.20, 4.40, 4.60, 4.80, 5.00)
    assert GS.max_research_dca_level == 13

    live = GS.live_dca_steps()
    research = GS.research_dca_steps()

    assert len(live) == 8
    assert len(research) == 13
    # The research ladder extends the live one; the live one never contains it.
    assert research[:8] == live
    assert all(step.drop_percent_from_average <= 4.00 for step in live)
    assert GS.is_research_only_level(9)
    assert not GS.is_research_only_level(8)


def test_greensynergy_is_labelled_experimental_and_never_claims_validation() -> None:
    described = GS.describe()
    assert described["status"] == str(StrategyStatus.EXPERIMENTAL)
    assert described["experimental"] is True
    assert described["research_ladder_is_live"] is False
    assert "EXPERIMENTAL" in GS.summary
    assert "not validated" in GS.summary.lower()


def test_reentry_policy_is_explicit_versioned_and_not_a_hidden_delay() -> None:
    reentry = GS.reentry
    assert reentry.observed_median_seconds == 6.0
    assert reentry.reconstructed is True
    assert reentry.delay_seconds >= 0
    assert "not proven" in reentry.note


def test_zuya_version_preserves_its_non_uniform_multipliers() -> None:
    steps = live_dca_steps_for(ZUYA_RECONSTRUCTED_V1)
    assert [round(step.size_multiplier_from_previous, 3) for step in steps] == [
        1.339, 1.539, 1.430, 1.442, 1.449, 1.450, 1.466, 1.467
    ]
    assert research_dca_steps_for(ZUYA_RECONSTRUCTED_V1) == steps
    assert ZUYA_RECONSTRUCTED_V1.family != GS.family


def test_strategy_versions_are_immutable() -> None:
    with pytest.raises(AttributeError):
        GS.leverage = 30  # type: ignore[misc]


def test_unknown_strategy_version_is_rejected_with_the_known_list() -> None:
    with pytest.raises(KeyError, match="greensynergy-reconstructed-v1"):
        get_strategy_version("does-not-exist")


def test_basket_is_pinned_to_the_version_it_opened_with() -> None:
    config = strategy_config_from_version(
        GS,
        symbol="HYPEUSDT",
        allocation=InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 1.1),
    )
    strategy = DcaStrategy(config)
    strategy.resume()
    cycle = strategy.begin_cycle(83.0)

    assert cycle.strategy_version_id == "greensynergy-reconstructed-v1"
    assert cycle.max_dca_level == 8

    # Editing configuration afterwards must not alter the open basket.
    strategy.config.strategy_version_id = "zuya-reconstructed-v1"
    strategy.config.tp_percent = 5.0
    assert cycle.strategy_version_id == "greensynergy-reconstructed-v1"
    assert cycle.tp_percent == pytest.approx(1.09)


def test_basket_reports_max_dca_reached_at_the_pinned_maximum() -> None:
    config = strategy_config_from_version(
        GS,
        symbol="HYPEUSDT",
        allocation=InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 1.0),
    )
    strategy = DcaStrategy(config)
    strategy.resume()
    cycle = strategy.begin_cycle(100.0)

    assert cycle.status is BasketStatus.OPEN
    for _ in range(8):
        strategy.apply_dca_fill(cycle.average_entry * 0.98)

    assert cycle.dca_level == 8
    assert cycle.at_max_dca
    assert cycle.status is BasketStatus.MAX_DCA_REACHED
    # There is no DCA9. The ladder simply stops.
    assert strategy.next_dca_trigger_price() is None
    assert strategy.next_dca_qty() is None
