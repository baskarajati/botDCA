from __future__ import annotations

from collections.abc import Iterable

from botdca.backtest import Candle
from pybit.unified_trading import HTTP


_NUMERIC_INTERVALS = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720"}


class BybitKlineClient:
    """Small public-market-data wrapper used by the historical replay CLI."""

    def __init__(self, session: HTTP | None = None) -> None:
        self.session = session or HTTP(testnet=False)

    def fetch_linear_candles(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "1",
    ) -> list[Candle]:
        symbol = symbol.upper()
        if start_ms > end_ms:
            raise ValueError("start_ms must be <= end_ms")
        if interval not in _NUMERIC_INTERVALS:
            raise ValueError("historical replay currently requires a numeric minute interval")

        interval_ms = int(interval) * 60_000
        limit = 1000
        cursor = start_ms
        candles: list[Candle] = []

        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + (limit - 1) * interval_ms)
            response = self.session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                start=cursor,
                end=chunk_end,
                limit=limit,
            )
            if response.get("retCode") != 0:
                raise RuntimeError(f"Bybit kline request failed: {response}")
            rows = response.get("result", {}).get("list", [])
            candles.extend(self._parse_rows(rows, start_ms=cursor, end_ms=chunk_end))
            cursor = chunk_end + interval_ms

        deduped = {candle.start_ms: candle for candle in candles}
        return [deduped[key] for key in sorted(deduped)]

    @staticmethod
    def _parse_rows(rows: Iterable[list[str]], *, start_ms: int, end_ms: int) -> list[Candle]:
        parsed: list[Candle] = []
        for row in rows:
            timestamp = int(row[0])
            if timestamp < start_ms or timestamp > end_ms:
                continue
            parsed.append(
                Candle(
                    start_ms=timestamp,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                )
            )
        return sorted(parsed, key=lambda candle: candle.start_ms)
