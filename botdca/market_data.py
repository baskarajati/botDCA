from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import sleep
from typing import Iterable

from pybit.unified_trading import HTTP


@dataclass(frozen=True, slots=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float = 0.0

    @property
    def start_time(self) -> datetime:
        return datetime.fromtimestamp(self.start_ms / 1000, tz=timezone.utc)


class BybitKlineSource:
    """Read-only historical market data source for Bybit V5 linear perpetuals."""

    def __init__(self, *, testnet: bool = False, request_pause_seconds: float = 0.03) -> None:
        self.session = HTTP(testnet=testnet)
        self.request_pause_seconds = request_pause_seconds

    def fetch(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "1",
    ) -> list[Candle]:
        if start_ms >= end_ms:
            raise ValueError("start_ms must be earlier than end_ms")

        candles: dict[int, Candle] = {}
        cursor_end = end_ms

        while cursor_end >= start_ms:
            response = self.session.get_kline(
                category="linear",
                symbol=symbol.upper(),
                interval=interval,
                start=start_ms,
                end=cursor_end,
                limit=1000,
            )
            rows = response.get("result", {}).get("list", [])
            if not rows:
                break

            oldest = cursor_end
            for row in rows:
                start = int(row[0])
                if start < start_ms or start > end_ms:
                    continue
                candles[start] = Candle(
                    start_ms=start,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[6]),
                )
                oldest = min(oldest, start)

            if oldest <= start_ms:
                break
            if oldest >= cursor_end:
                raise RuntimeError("Bybit kline pagination did not advance")

            cursor_end = oldest - 1
            if self.request_pause_seconds:
                sleep(self.request_pause_seconds)

        return sorted(candles.values(), key=lambda candle: candle.start_ms)


def ensure_chronological(candles: Iterable[Candle]) -> list[Candle]:
    ordered = sorted(candles, key=lambda candle: candle.start_ms)
    if not ordered:
        raise ValueError("at least one candle is required")
    return ordered
