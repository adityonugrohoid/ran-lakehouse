"""Diurnal and weekly traffic profiles (rule M3).

Two shapes, both ASSUMPTION and checked later against the real-data
cross-check (rule E2):
- residential, for suburban and rural points: a night trough, a morning
  rise, a midday plateau and an evening peak, weekends later and flatter;
- business, for urban points: a weekday peak from late morning to
  mid-afternoon, a small evening and low weekends.
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
BUSINESS_BUMPS = ((11.0, 1.6, 0.9), (14.5, 1.8, 0.85), (20.0, 1.5, 0.3))
BUSINESS_WEEKEND_LEVEL = 0.45  # business weekend relative to its weekday


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


def activity_by_class(starts: list[datetime]) -> np.ndarray:
    """Activity per period for users at urban, suburban and rural points.

    Urban points follow the business shape, the others the residential
    shape; each shape is 1.0 at its own weekday peak.

    Args:
        starts: Local start times of the periods.

    Returns:
        Activity, shape (periods, 3).
    """
    residential = activity(starts)
    grid = np.arange(PERIODS_PER_DAY) * PERIOD_MINUTES / 60.0 + PERIOD_MINUTES / 120.0
    shape = _shape(grid, BUSINESS_BUMPS)
    shape = shape / shape.max()
    business = np.array(
        [
            shape[(t.hour * 60 + t.minute) // PERIOD_MINUTES]
            * (BUSINESS_WEEKEND_LEVEL if t.weekday() >= 5 else 1.0)
            for t in starts
        ]
    )
    return np.stack([business, residential, residential], axis=1)


def activity(starts: list[datetime]) -> np.ndarray:
    """Residential activity per period, 1.0 at the weekday peak.

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


# Daytime population shift (ASSUMPTION, checked later by the real-data
# cross-check, rule E2): urban cores gain users during weekday working
# hours and residential (suburban and rural) areas gain in the evening; the
# other side gives up the same number, so totals hold.
URBAN_DAYTIME_FACTOR = 1.4  # START, weekdays 08:00-17:00
# Every day 18:00-23:00 urban cores keep this share of their users and the
# rest go to residential areas (ASSUMPTION; urban users are about a fifth
# of the total, so a larger residential factor would empty the cores).
URBAN_EVENING_FACTOR = 0.75
DAYTIME_HOURS = (8.0, 17.0)
EVENING_HOURS = (18.0, 23.0)
RAMP_HOURS = 1.0  # shifts ramp in and out over this long


def _window(hours: np.ndarray, span: tuple[float, float]) -> np.ndarray:
    """Weight 1 inside a window of the day, ramping linearly at its edges.

    Args:
        hours: Hour of day at the period centres.
        span: (start, end) hours, each the midpoint of its ramp.

    Returns:
        Weight 0-1 per period.
    """
    half = RAMP_HOURS / 2.0
    rise = np.clip((hours - span[0] + half) / RAMP_HOURS, 0.0, 1.0)
    fall = np.clip((span[1] + half - hours) / RAMP_HOURS, 0.0, 1.0)
    result: np.ndarray = rise * fall
    return result


def population_shift(starts: list[datetime], class_totals: np.ndarray) -> np.ndarray:
    """User multipliers per period for urban, suburban and rural points.

    Args:
        starts: Local start times of the periods.
        class_totals: Users living at urban, suburban and rural points.

    Returns:
        Multipliers, shape (periods, 3); the users they imply sum to the
        class totals' sum in every period.

    Raises:
        ValueError: If a shift would leave a class with negative users.
    """
    hours = np.array([t.hour + t.minute / 60.0 + PERIOD_MINUTES / 120.0 for t in starts])
    weekday = np.array([t.weekday() < 5 for t in starts])
    urban, residential = class_totals[0], class_totals[1] + class_totals[2]
    day = _window(hours, DAYTIME_HOURS) * weekday
    evening = _window(hours, EVENING_HOURS)
    urban_gain = (URBAN_DAYTIME_FACTOR - 1.0) * day
    urban_loss = (1.0 - URBAN_EVENING_FACTOR) * evening
    urban_factor = 1.0 + urban_gain - urban_loss
    residential_factor = 1.0 + (urban_loss - urban_gain) * urban / residential
    if (urban_factor < 0).any() or (residential_factor < 0).any():
        raise ValueError("population shift leaves a class with negative users")
    return np.stack([urban_factor, residential_factor, residential_factor], axis=1)
