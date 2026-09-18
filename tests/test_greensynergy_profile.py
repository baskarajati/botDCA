import csv
from pathlib import Path

import pytest

from botdca.greensynergy_profile import (
    group_baskets,
    portfolio_capital_expansion,
    profile_symbol_export,
    reconstruct_ladder_fit,
    simultaneous_deep_periods,
    split_export_by_symbol,
)
from botdca.strategy_version import GREENSYNERGY_RECONSTRUCTED_V1 as GS
from botdca.trader_export import TraderOrder

HEADER = [
    "page", "row_on_page", "position_symbol", "position_side", "margin_and_leverage",
    "order_qty", "roi_percent", "entry_price", "opened_on", "closing_price",
    "closed_on", "followers",
]


def _row(symbol, qty, entry, opened, closing, closed, *, leverage="Cross 24.00x"):
    return {
        "page": "1", "row_on_page": "1",
        "position_symbol": symbol, "position_side": "Long",
        "margin_and_leverage": leverage,
        "order_qty": f"{qty} {symbol.replace('USDT', '')}",
        "roi_percent": "+23.00%",
        "entry_price": f"{entry} USDT",
        "opened_on": opened,
        "closing_price": f"{closing} USDT",
        "closed_on": closed,
        "followers": "20",
    }


def _write(path: Path, rows) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _order(qty, average, closing, opened_ms, closed_ms, symbol="HYPEUSDT"):
    return TraderOrder(
        source_row=1, symbol=symbol, side="Long",
        opened_ms=opened_ms, closed_ms=closed_ms,
        order_qty=qty, average_entry=average, closing_price=closing,
        leverage=24.0, roi_percent=23.0, followers=20,
    )


def test_a_multi_symbol_export_is_partitioned_per_symbol(tmp_path) -> None:
    source = _write(
        tmp_path / "export.csv",
        [
            _row("HYPEUSDT", "0.33", "83.31", "2026-09-17 21:47:03", "84.22", "2026-09-18 00:59:36"),
            _row("ONDOUSDT", "67", "0.3543", "2026-09-17 10:00:00", "0.3582", "2026-09-17 12:00:00"),
            _row("DOGEUSDT", "121", "0.2100", "2026-09-17 08:00:00", "0.2123", "2026-09-17 09:00:00"),
        ],
    )
    written = split_export_by_symbol(source, tmp_path / "work")

    assert set(written) == {"HYPEUSDT", "ONDOUSDT", "DOGEUSDT"}
    # The upstream parser is single-symbol; each partition must now parse alone.
    profile = profile_symbol_export(written["ONDOUSDT"], "ONDOUSDT")
    assert len(profile.baskets) == 1


def test_structural_outliers_can_be_filtered_by_leverage(tmp_path) -> None:
    source = _write(
        tmp_path / "export.csv",
        [
            _row("HYPEUSDT", "0.33", "83.31", "2026-09-17 21:47:03", "84.22", "2026-09-18 00:59:36"),
            _row(
                "HYPEUSDT", "1.0", "70.00", "2026-06-01 00:00:00", "71.0", "2026-06-01 01:00:00",
                leverage="Cross 12.50x",
            ),
        ],
    )
    kept = split_export_by_symbol(source, tmp_path / "work", leverage_filter="Cross 24.00x")
    with open(kept["HYPEUSDT"], encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_close_fragments_of_one_basket_are_merged_despite_rounding_noise() -> None:
    # Real GreenSynergy rows: one position, average entry differing in the last
    # digits and close prices differing by a fraction of a tick.
    orders = [
        _order(0.33, 62.4982875, 63.184999629635044, 1_782_468_364_000, 1_782_489_090_000),
        _order(0.47, 62.49828749, 63.18499901145631, 1_782_471_982_000, 1_782_489_090_000),
    ]
    baskets = group_baskets(orders)

    assert len(baskets) == 1
    assert baskets[0].dca_depth == 1
    assert baskets[0].order_quantities == (0.33, 0.47)


def test_genuinely_separate_baskets_are_not_merged() -> None:
    orders = [
        _order(0.33, 62.50, 63.18, 1_782_468_364_000, 1_782_489_090_000),
        _order(0.33, 64.25, 64.96, 1_782_494_766_000, 1_782_508_484_000),
    ]
    assert len(group_baskets(orders)) == 2


def test_grouped_baskets_do_not_overlap_in_time(tmp_path) -> None:
    source = _write(
        tmp_path / "export.csv",
        [
            _row("HYPEUSDT", "0.33", "62.4982875", "2026-06-26 10:06:04", "63.1849996", "2026-06-26 15:51:30"),
            _row("HYPEUSDT", "0.47", "62.49828749", "2026-06-26 11:06:22", "63.1849990", "2026-06-26 15:51:30"),
            _row("HYPEUSDT", "0.33", "64.25482352", "2026-06-26 17:26:06", "64.9609990", "2026-06-26 21:14:44"),
        ],
    )
    written = split_export_by_symbol(source, tmp_path / "work")
    profile = profile_symbol_export(written["HYPEUSDT"], "HYPEUSDT")

    assert len(profile.baskets) == 2
    assert profile.overlapping_baskets == 0


def test_profile_reports_depth_take_profit_multiplier_and_reentry() -> None:
    orders = [
        _order(0.33, 80.0, 80.872, 1_000_000, 2_000_000),
        _order(0.4686, 80.0, 80.872, 1_500_000, 2_000_000),
        _order(0.5, 81.0, 81.883, 2_006_000, 3_000_000),
    ]
    baskets = group_baskets(orders)
    from botdca.greensynergy_profile import SymbolProfile

    described = SymbolProfile("HYPEUSDT", 3, baskets).describe()

    assert described["basket_count"] == 2
    assert described["max_observed_dca"] == 1
    assert described["dca_depth_distribution"] == {"0": 1, "1": 1}
    assert described["take_profit_percent"]["median"] == pytest.approx(1.09, abs=0.01)
    assert described["size_multiplier"]["median"] == pytest.approx(1.42, abs=0.01)
    assert described["reentry_gap_seconds"]["median"] == pytest.approx(6.0)
    assert described["probability_reaching_level"]["1"] == pytest.approx(0.5)


def test_ladder_fit_scores_the_quantity_rule_and_refuses_to_claim_trigger_accuracy() -> None:
    from botdca.greensynergy_profile import SymbolProfile

    baskets = group_baskets(
        [
            _order(0.33, 80.0, 80.872, 1_000_000, 2_000_000),
            _order(0.4686, 80.0, 80.872, 1_500_000, 2_000_000),
        ]
    )
    fit = reconstruct_ladder_fit(SymbolProfile("HYPEUSDT", 2, baskets), GS)

    assert fit["expected_size_multiplier"] == pytest.approx(1.42)
    assert fit["size_multiplier_relative_error_percent"]["median"] == pytest.approx(0, abs=0.1)
    # Per-fill prices are not exported, so this must never be claimed.
    assert fit["trigger_ladder_measurable_from_export"] is False
    assert "not exported" in fit["note"]


def test_simultaneous_deep_periods_surface_correlated_tail_risk() -> None:
    from botdca.greensynergy_profile import GreenSynergyBasket, SymbolProfile

    deep = tuple(0.1 * (1.42**i) for i in range(6))  # depth 5
    hype = SymbolProfile(
        "HYPEUSDT", 6,
        [GreenSynergyBasket("HYPEUSDT", 1_000, 5_000, 80.0, 80.9, 24.0, deep)],
    )
    ondo = SymbolProfile(
        "ONDOUSDT", 6,
        [GreenSynergyBasket("ONDOUSDT", 3_000, 9_000, 0.35, 0.354, 24.0, deep)],
    )
    report = simultaneous_deep_periods([hype, ondo], deep_dca_level=5)

    assert report["deep_basket_count"] == 2
    assert report["overlapping_pair_count"] == 1
    assert report["longest_overlaps"][0]["symbols"] == ["HYPEUSDT", "ONDOUSDT"]
    assert report["longest_overlaps"][0]["overlap_seconds"] == pytest.approx(2.0)


def test_capital_expansion_reports_the_deepest_observed_basket() -> None:
    from botdca.greensynergy_profile import GreenSynergyBasket, SymbolProfile

    quantities = tuple(0.3 * (1.42**i) for i in range(9))
    profile = SymbolProfile(
        "HYPEUSDT", 9,
        [GreenSynergyBasket("HYPEUSDT", 1_000, 2_000, 80.0, 80.9, 24.0, quantities)],
    )
    expansion = portfolio_capital_expansion([profile])["per_symbol"]["HYPEUSDT"]

    assert expansion["max_observed_dca"] == 8
    assert expansion["expansion_multiple"] == pytest.approx(
        sum(quantities) / quantities[0]
    )
    assert expansion["deepest_basket_margin_usdt"] > expansion["initial_margin_usdt"]
