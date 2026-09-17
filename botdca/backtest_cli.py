from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime

from botdca.backtest import IntrabarPath, ReplayEngine
from botdca.marketdata import BybitKlineClient
from botdca.strategy import StrategyConfig


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _summary(result: object, include_cycles: bool) -> dict[str, object]:
    payload = asdict(result)
    if not include_cycles:
        payload.pop("cycles", None)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the reconstructed DCA strategy on Bybit klines")
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--start", required=True, help="ISO-8601 UTC or offset datetime")
    parser.add_argument("--end", required=True, help="ISO-8601 UTC or offset datetime")
    parser.add_argument("--interval", default="1", help="Bybit numeric minute interval; default 1")
    parser.add_argument(
        "--path",
        choices=["low-first", "high-first", "both"],
        default="both",
        help="Intrabar path assumption",
    )
    parser.add_argument("--fee-rate", type=float, default=0.00055)
    parser.add_argument("--leverage", type=int, default=24)
    parser.add_argument("--base-margin", type=float, default=1.0)
    parser.add_argument("--tp-percent", type=float, default=1.09)
    parser.add_argument("--include-cycles", action="store_true")
    args = parser.parse_args()

    start = _parse_datetime(args.start)
    end = _parse_datetime(args.end)
    if start >= end:
        parser.error("--start must be earlier than --end")

    config = StrategyConfig(
        symbol=args.symbol.upper(),
        leverage=args.leverage,
        base_margin_usdt=args.base_margin,
        tp_percent=args.tp_percent,
    )
    candles = BybitKlineClient().fetch_linear_candles(
        symbol=config.symbol,
        start_ms=_to_ms(start),
        end_ms=_to_ms(end),
        interval=args.interval,
    )
    if not candles:
        raise SystemExit("Bybit returned no candles for the requested range")

    paths = (
        [IntrabarPath.LOW_FIRST, IntrabarPath.HIGH_FIRST]
        if args.path == "both"
        else [IntrabarPath(args.path)]
    )
    results = []
    for path in paths:
        result = ReplayEngine(config, intrabar_path=path, taker_fee_rate=args.fee_rate).run(candles)
        results.append(_summary(result, args.include_cycles))

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
