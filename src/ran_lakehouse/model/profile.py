"""Diurnal and weekly traffic profile (rule M3).

The shape is ASSUMPTION: a night trough, a morning rise, a midday plateau
and an evening peak, with weekends later and flatter. It is checked later
against the real-data cross-check (rule E2).
"""

from datetime import datetime, timedelta

import numpy as np

PERIOD_MINUTES = 15  # granPeriod PT900S (TS 32.435)
PERIODS_PER_DAY = 24 * 60 // PERIOD_MINUTES

# (centre hour, width in hours, weight) of the bumps in the weekday and the
# weekend shape, on top of a night floor (ASSUMPTION).
NIGHT_FLOOR = 0.12
WEEKDAY_BUMPS = ((8.5, 1.5, 0.45), (13.0, 2.5, 0.55), (20.5, 2.0, 1.0))
WEEKEND_BUMPS = ((10.5, 2.0, 0.5), (14.5, 2.5, 0.6), (21.0, 2.0, 1.0))
WEEKEND_LEVEL = 0.92  # weekend peak relative to weekday peak (ASSUMPTION)


def _shape(hours: np.ndarray, bumps: tuple[tuple[float, float, float], ...]) -> np.ndarray:
    """Sum of periodic Gaussian bumps over a night floor.

    Args:
        hours: Hour of day, 0-24.
        bumps: (centre, width, weight) triples.

    Returns:
        The unnormalized shape.
    """
    total = np.full(hours.shape, NIGHT_FLOOR)
    for centre, width, weight in bumps:
        delta = (hours - centre + 12.0) % 24.0 - 12.0
        total = total + weight * np.exp(-0.5 * (delta / width) ** 2)
    return total


def activity(starts: list[datetime]) -> np.ndarray:
    """Share of subscribers active in each period, 1.0 at the weekday peak.

    Args:
        starts: Local start times of the periods.

    Returns:
        Activity factor per period.
    """
    grid = np.arange(PERIODS_PER_DAY) * PERIOD_MINUTES / 60.0 + PERIOD_MINUTES / 120.0
    weekday = _shape(grid, WEEKDAY_BUMPS)
    peak = weekday.max()
    weekday = weekday / peak
    weekend = WEEKEND_LEVEL * _shape(grid, WEEKEND_BUMPS) / _shape(grid, WEEKEND_BUMPS).max()
    out = np.empty(len(starts))
    for i, t in enumerate(starts):
        index = (t.hour * 60 + t.minute) // PERIOD_MINUTES
        out[i] = weekend[index] if t.weekday() >= 5 else weekday[index]
    return out


def period_starts(first: datetime, n_periods: int) -> list[datetime]:
    """Consecutive 15-minute period starts.

    Args:
        first: Start of the first period.
        n_periods: Number of periods.

    Returns:
        The start times.
    """
    return [first + timedelta(minutes=PERIOD_MINUTES * i) for i in range(n_periods)]
