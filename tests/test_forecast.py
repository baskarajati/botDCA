import pytest

from botdca.forecast import (
    APPROXIMATION_NOTE,
    forecast_symbol,
    portfolio_scenarios,
    research_expansion,
    worst_case_scenario,
)
from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy_version import GREENSYNERGY_RECONSTRUCTED_V1 as GS


def _forecast(symbol="HYPEUSDT", margin=1.1, price=83.0):
    return forecast_symbol(
        symbol=symbol,
        version=GS,
        allocation=InitialAllocation(SizingMode.FIXED_MARGIN_USDT, margin),
        reference_price=price,
    )


def test_forecast_reports_quantity_margin_and_notional_at_every_level() -> None:
    forecast = _forecast()
    row = forecast.row_at(0)
    assert row.incremental_qty == pytest.approx(1.1 * 24 / 83.0)
    assert row.incremental_margin_usdt == pytest.approx(1.1)
    assert row.weighted_average_entry == pytest.approx(83.0)

    for previous, row in zip(forecast.rows, forecast.rows[1:]):
        assert row.cumulative_qty > previous.cumulative_qty
        assert row.cumulative_margin_usdt > previous.cumulative_margin_usdt
        # Each fill is below the running weighted average, which therefore falls.
        assert row.fill_price < previous.weighted_average_entry
        assert row.weighted_average_entry < previous.weighted_average_entry


def test_quantity_grows_by_the_version_multiplier() -> None:
    forecast = _forecast()
    for previous, row in zip(forecast.rows, forecast.rows[1:]):
        assert row.incremental_qty / previous.incremental_qty == pytest.approx(1.42)


def test_levels_beyond_the_live_maximum_are_flagged_research_only() -> None:
    forecast = _forecast()
    live = [row for row in forecast.rows if not row.research_only]
    research = [row for row in forecast.rows if row.research_only]

    assert [row.level for row in live] == list(range(9))
    assert [row.level for row in research] == [9, 10, 11, 12, 13]
    assert forecast.max_live_dca_level == 8


def test_research_levels_can_be_excluded_entirely() -> None:
    forecast = forecast_symbol(
        symbol="HYPEUSDT",
        version=GS,
        allocation=InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 1.0),
        include_research_levels=False,
    )
    assert len(forecast.rows) == 9
    assert all(not row.research_only for row in forecast.rows)


def test_fixed_quantity_allocation_forecasts_from_the_configured_quantity() -> None:
    forecast = forecast_symbol(
        symbol="HYPEUSDT",
        version=GS,
        allocation=InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.6),
        reference_price=83.0,
    )
    assert forecast.initial_qty == pytest.approx(0.6)
    assert forecast.initial_margin_usdt == pytest.approx(0.6 * 83.0 / 24)


def test_full_live_ladder_commits_far_more_than_the_initial_margin() -> None:
    forecast = _forecast(margin=1.0, price=100.0)
    # A 1.42x geometric ladder to DCA8 is on the order of fifty initial units.
    ratio = forecast.row_at(8).cumulative_margin_usdt / 1.0
    assert 40 < ratio < 60


def test_portfolio_scenarios_combine_every_coin_and_flag_research_levels() -> None:
    forecasts = {
        "HYPEUSDT": _forecast("HYPEUSDT", 1.1),
        "ONDOUSDT": _forecast("ONDOUSDT", 1.0),
        "DOGEUSDT": _forecast("DOGEUSDT", 0.4),
    }
    scenarios = portfolio_scenarios(forecasts, max_live_dca_level=8)
    names = [scenario.name for scenario in scenarios]

    assert "all coins DCA4" in names
    assert "all coins DCA6" in names
    assert "all coins DCA8" in names
    assert any("DCA8 + 2 x DCA4" in name for name in names)

    all_eight = next(s for s in scenarios if s.name == "all coins DCA8")
    all_four = next(s for s in scenarios if s.name == "all coins DCA4")
    assert all_eight.total_margin_usdt > all_four.total_margin_usdt
    assert not all_eight.contains_research_levels


def test_stress_points_report_loss_at_5_10_and_20_percent_and_are_labelled() -> None:
    forecasts = {"HYPEUSDT": _forecast()}
    scenario = worst_case_scenario(
        forecasts, max_live_dca_level=8, account_equity_usdt=200.0
    )
    assert [point.drop_percent for point in scenario.stress] == [5.0, 10.0, 20.0]
    assert all(point.floating_pnl_usdt < 0 for point in scenario.stress)

    losses = [point.floating_pnl_usdt for point in scenario.stress]
    assert losses[0] > losses[1] > losses[2]
    assert scenario.stress[0].equity_consumption_usdt == pytest.approx(-losses[0])

    described = scenario.describe()
    assert described["note"] == APPROXIMATION_NOTE
    assert "NOT AN EXACT BYBIT UTA LIQUIDATION" in described["note"]


def test_equity_buffer_is_omitted_when_account_equity_is_unknown() -> None:
    scenario = worst_case_scenario({"HYPEUSDT": _forecast()}, max_live_dca_level=8)
    assert all(point.approximate_equity_buffer_usdt is None for point in scenario.stress)


def test_research_expansion_quantifies_the_deeper_ladder_without_enabling_it() -> None:
    forecasts = {"HYPEUSDT": _forecast(margin=1.0, price=100.0)}
    expansion = research_expansion(forecasts, max_live_dca_level=8)

    assert expansion["live_max_level"] == 8
    assert expansion["research_max_level"] == 13
    assert expansion["research_total_margin_usdt"] > expansion["live_total_margin_usdt"]
    assert expansion["expansion_multiple"] > 4
    assert "never traded" in expansion["note"]


def test_worst_case_is_empty_when_no_coin_is_enabled() -> None:
    assert worst_case_scenario({}, max_live_dca_level=8) is None
    assert portfolio_scenarios({}, max_live_dca_level=8) == []


def test_a_trial_cap_lowers_live_depth_without_calling_levels_research() -> None:
    forecast = forecast_symbol(
        symbol="HYPEUSDT",
        version=GS,
        allocation=InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 0.25),
        reference_price=91.5,
        live_dca_cap=0,
    )

    assert forecast.max_live_dca_level == 0
    assert forecast.row_at(0).live is True
    assert forecast.row_at(1).live is False
    assert forecast.row_at(1).research_only is False  # DCA1 is live ladder, only capped
    assert forecast.row_at(9).research_only is True
    assert forecast.describe()["rows"][1]["live"] is False


def test_scenarios_separate_research_levels_from_levels_beyond_the_live_cap() -> None:
    from botdca.forecast import build_scenario

    forecasts = {"HYPEUSDT": _forecast()}

    capped = build_scenario("DCA4", forecasts, {"HYPEUSDT": 4}, max_live_dca_level=0)
    research = build_scenario("DCA9", forecasts, {"HYPEUSDT": 9}, max_live_dca_level=8)

    assert capped.contains_research_levels is False
    assert capped.exceeds_live_cap is True
    assert capped.describe()["exceeds_live_cap"] is True
    assert research.contains_research_levels is True
    assert research.exceeds_live_cap is True
