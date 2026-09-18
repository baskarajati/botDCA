import pytest

from botdca.database import Database
from botdca.strategy_slots import (
    StrategySlot,
    StrategySlotStore,
    enabled_strategy_slots,
    margin_forecast,
    validate_strategy_slots,
)


def _slots():
    return [
        StrategySlot(1, True, "hypeusdt", 1.0),
        StrategySlot(2, True, "btcusdt", 2.0),
        StrategySlot(3, False, "ethusdt", 3.0),
    ]


def test_strategy_slots_are_normalized_validated_and_persisted(tmp_path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path}/slots.db")
    database.create_schema()
    store = StrategySlotStore(database)

    saved = store.replace(_slots())
    loaded = store.load(default_symbol="SOLUSDT", default_base_margin_usdt=9)

    assert saved == loaded
    assert [slot.symbol for slot in enabled_strategy_slots(loaded)] == [
        "HYPEUSDT",
        "BTCUSDT",
    ]
    assert loaded[1].base_margin_usdt == 2.0


def test_strategy_slots_reject_duplicate_enabled_symbols() -> None:
    slots = _slots()
    slots[1] = StrategySlot(2, True, "HYPEUSDT", 2.0)

    with pytest.raises(ValueError, match="configured more than once"):
        validate_strategy_slots(slots)


def test_margin_forecast_scales_linearly_without_close_rule() -> None:
    one = margin_forecast(1.0, leverage=24)
    three = margin_forecast(3.0, leverage=24)

    assert [row["level"] for row in one] == list(range(9))
    assert one[0]["cumulative_margin_usdt"] == 1.0
    assert one[-1]["cumulative_margin_usdt"] == pytest.approx(54.2387, rel=1e-4)
    assert three[-1]["cumulative_margin_usdt"] == pytest.approx(
        one[-1]["cumulative_margin_usdt"] * 3
    )
    assert all("close" not in row for row in one)


def test_slot_ladder_forecast_follows_the_trial_depth_cap() -> None:
    from botdca.strategy_slots import StrategySlot, serialized_slot

    slot = StrategySlot(1, True, "HYPEUSDT", 0.25)

    result = serialized_slot(slot, leverage=24, max_dca_level=0, reference_price=91.5)

    assert result["ladder_forecast"]["max_live_dca_level"] == 0
    assert result["live_ladder_margin_usdt"] == pytest.approx(
        result["ladder_forecast"]["rows"][0]["cumulative_margin_usdt"]
    )
