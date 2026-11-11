"""Applies the coin filters and builds the rows of the two lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .binance_api import Candle
from .levels import Approach, Level, average_candle_range, nearest_approach
from .metrics import natr_values
from .settings import Settings


@dataclass
class LevelRow:
    symbol: str
    side: str
    level: float
    price: float
    distance_percent: float
    distance_candles: float     # distance in "average 1-minute candles"
    touches: int
    volume_24h: float
    natr: float
    trades: int

    @property
    def key(self) -> str:
        """Identity used for the cooldown and the 'since' column."""
        return f"{self.symbol}:{self.side}"


def activity(candles_1m: Sequence[Candle], window: int) -> tuple[float, int]:
    """(NATR %, number of trades) over the last `window` 1-minute candles."""
    window = max(1, int(window))
    recent = list(candles_1m[-(window + 1):])  # one extra candle gives the previous close for the first TR
    if not recent:
        return 0.0, 0
    natr = natr_values(recent)[-window:]
    trades = sum(c.trades for c in recent[-window:])
    return (sum(natr) / len(natr) if natr else 0.0), trades


def candidate_symbols(settings: Settings, volumes_24h: dict[str, float], symbols: set[str]) -> list[str]:
    return sorted(s for s in symbols if volumes_24h.get(s, 0.0) > settings.min_volume_usdt)


def build_row(
    settings: Settings,
    symbol: str,
    price: float,
    volume_24h: float,
    candles_1m: Sequence[Candle],
    levels: Sequence[Level],
) -> Optional[LevelRow]:
    """Row for one coin, or None if it fails a filter or has no usable cascade."""
    if volume_24h <= settings.min_volume_usdt:
        return None
    natr, trades = activity(candles_1m, settings.activity_window_minutes)
    if natr < settings.min_natr or trades < settings.min_trades:
        return None
    avg_range = average_candle_range(candles_1m, settings.avg_candle_minutes)
    approach: Optional[Approach] = nearest_approach(levels, price, avg_range)
    if approach is None:
        return None
    return LevelRow(
        symbol=symbol,
        side=approach.level.side,
        level=approach.level.price,
        price=price,
        distance_percent=approach.distance_percent,
        distance_candles=approach.distance_candles,
        touches=approach.level.touches,
        volume_24h=volume_24h,
        natr=natr,
        trades=trades,
    )


def split_rows(settings: Settings, rows: Sequence[LevelRow]) -> tuple[list[LevelRow], list[LevelRow]]:
    """(coins that are close enough to a cascade, the N coins closest to a cascade), both nearest first."""
    ordered = sorted(rows, key=lambda r: r.distance_candles)
    near = [r for r in ordered if r.distance_candles <= settings.approach_candles]
    return near, ordered[: max(1, settings.closest_count)]
