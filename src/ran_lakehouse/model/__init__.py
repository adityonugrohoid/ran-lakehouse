"""The network model (rules M1-M5, M7): coverage, load and counters.

Stated simplifications (rule M7): no terrain in the served region, no
scheduler, fading or mobility traces; coverage, load and interference are
computed on the population grid, and interference uses a fixed reference
load of the other cells.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from ran_lakehouse.model.cells import (
    GSM_GRADE_OF_SERVICE,
    GSM_TRX_MAX,
    GSM_TRX_MIN,
    CellState,
    build_cell_state,
)
from ran_lakehouse.model.counters import (
    ERLANG_PER_USER_AT_PEAK,
    DayCounters,
    gsm_channels,
    gsm_day,
    lte_day,
    missing_neighbour_share,
)
from ran_lakehouse.model.coverage import Grid, LayerCoverage, all_layers, build_grid
from ran_lakehouse.model.erlang import erlang_b
from ran_lakehouse.model.profile import (
    PERIODS_PER_DAY,
    activity_by_class,
    period_starts,
    population_shift,
)
from ran_lakehouse.model.serving import Serving, build_serving
from ran_lakehouse.world import World

# First period of every run, local time (WIB, rule W5); a Monday.
RUN_START = datetime(2026, 1, 5, 0, 0)


@dataclass(frozen=True)
class NetworkModel:
    """A world's network with its coverage and serving summary.

    Attributes:
        world: The world.
        state: Cell parameters.
        grid: Served-region grid points.
        coverage: Band to layer coverage.
        serving: Who each cell serves.
        missing_neighbours: (source, target) relations removed.
    """

    world: World
    state: CellState
    grid: Grid
    coverage: dict[str, LayerCoverage]
    serving: Serving
    missing_neighbours: frozenset[tuple[int, int]]


@dataclass(frozen=True)
class Day:
    """One simulated day.

    Attributes:
        index: Day index from RUN_START.
        starts: Local start time of each period.
        lte: LTE counters.
        gsm: GSM counters.
    """

    index: int
    starts: list[datetime]
    lte: DayCounters
    gsm: DayCounters


def build_model(
    world: World, state: CellState, missing_neighbours: frozenset[tuple[int, int]]
) -> NetworkModel:
    """Compute coverage and serving for a world and a cell state.

    Args:
        world: The world.
        state: Cell parameters (build_cell_state for the defaults).
        missing_neighbours: (source, target) relations removed from the
            neighbour lists; empty for the default network.

    Returns:
        The network model.
    """
    grid = build_grid(world)
    coverage = all_layers(state, grid)
    serving = build_serving(state, grid, coverage, grid.persons)
    return NetworkModel(world, state, grid, coverage, serving, missing_neighbours)


def default_model(world: World) -> NetworkModel:
    """The network as built, with every parameter at its default.

    GSM transceivers are dimensioned from the served users (dimension_gsm).

    Args:
        world: The world.

    Returns:
        The network model.
    """
    first = build_model(world, build_cell_state(world), frozenset())
    state = first.state.with_values(trx=dimension_gsm(first.state, first.serving))
    return NetworkModel(world, state, first.grid, first.coverage, first.serving, frozenset())


def dimension_gsm(state: CellState, serving: Serving) -> np.ndarray:
    """Transceivers per GSM cell for the grade of service at the busy hour.

    Args:
        state: Cell parameters.
        serving: Who each cell serves.

    Returns:
        Transceivers per cell (0 for LTE cells).
    """
    busy_hour = serving.subscribers * ERLANG_PER_USER_AT_PEAK
    trx = np.zeros(len(state.cell_names), dtype=int)
    gsm = state.technology == "GSM"
    trx[gsm] = GSM_TRX_MAX
    for n in range(GSM_TRX_MAX, GSM_TRX_MIN - 1, -1):
        n_tch, _ = gsm_channels(np.full(1, n))
        ok = erlang_b(busy_hour, np.full(busy_hour.shape, int(n_tch[0]))) <= GSM_GRADE_OF_SERVICE
        trx[gsm & ok] = n
    return trx


def simulate_days(model: NetworkModel, first_day: int, n_days: int) -> Iterator[Day]:
    """Counters for consecutive days.

    Args:
        model: The network model.
        first_day: Index of the first day from RUN_START.
        n_days: Number of days.

    Yields:
        One Day at a time, so a long run never holds every period in memory.
    """
    n_cells = len(model.state.cell_names)
    missing = missing_neighbour_share(model.serving, model.missing_neighbours, n_cells)
    load = np.ones((PERIODS_PER_DAY, n_cells))
    lte = model.state.technology == "LTE"
    class_totals = model.serving.subscribers_by_class[lte].sum(axis=0)
    for day in range(first_day, first_day + n_days):
        starts = period_starts(RUN_START + timedelta(days=day), PERIODS_PER_DAY)
        class_load = activity_by_class(starts) * population_shift(starts, class_totals)
        yield Day(
            index=day,
            starts=starts,
            lte=lte_day(model.state, model.serving, class_load, day, missing, load),
            gsm=gsm_day(model.state, model.serving, class_load, day, missing, load),
        )
