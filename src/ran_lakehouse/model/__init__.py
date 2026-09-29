"""The network model (rules M1-M5, M7): coverage, load and counters.

Stated simplifications (rule M7): no terrain in the served region, no
scheduler, fading or mobility traces; coverage, load and interference are
computed on the population grid, and interference uses a fixed reference
load of the other cells.
"""

import weakref
from collections.abc import Iterator
from dataclasses import dataclass, replace
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
    RelationPlan,
    gsm_channels,
    gsm_day,
    lte_day,
    relation_plan,
)
from ran_lakehouse.model.coverage import (
    Grid,
    LayerCoverage,
    build_grid,
    fill_points,
    layer_coverage,
    points_near,
)
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

Relation = tuple[int, int]


@dataclass(frozen=True)
class NetworkModel:
    """A world's network in one configuration, with coverage and serving.

    Attributes:
        world: The world.
        state: Cell parameters.
        grid: Served-region grid points.
        persons: Persons per grid point (the population layer, or a changed
            one such as a traffic surge, rule F1d).
        coverage: Band to layer coverage.
        serving: Who each cell serves; its relations are the handover
            demand from coverage overlap.
        neighbours: Configured neighbour relations (source, target), the CM
            neighbour lists; handover demand towards a target outside them
            cannot be served.
        ul_rise_db: Uplink noise rise per cell from external interference,
            dB (rule F1e); 0 by default.
        shift_totals: Users by area class that the population shift keeps
            constant (fixed at the network as built).
    """

    world: World
    state: CellState
    grid: Grid
    persons: np.ndarray
    coverage: dict[str, LayerCoverage]
    serving: Serving
    neighbours: frozenset[Relation]
    ul_rise_db: np.ndarray
    shift_totals: np.ndarray


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

    def __post_init__(self) -> None:
        """Count the day as alive until it is garbage-collected."""
        DAY_TRACKER.born(self)


class DayTracker:
    """Counts distinct simulated days alive at once, so runs prove they stream.

    A demo day holds tens of MB of counters; a run that keeps its days
    instead of streaming them runs the machine out of memory. Parts of one
    day (one per fault set, spliced together) count as that one day.
    """

    def __init__(self) -> None:
        """Start with no day alive."""
        self.alive: dict[int, int] = {}
        self.peak = 0

    def born(self, day: "Day") -> None:
        """Register a new day object and count it until it is collected.

        Args:
            day: The new day.
        """
        self.alive[day.index] = self.alive.get(day.index, 0) + 1
        self.peak = max(self.peak, len(self.alive))
        weakref.finalize(day, self.died, day.index)

    def died(self, index: int) -> None:
        """Uncount a collected day object.

        Args:
            index: Its day index.
        """
        self.alive[index] -= 1
        if self.alive[index] == 0:
            del self.alive[index]

    def reset_peak(self) -> None:
        """Start a new peak measurement from the days alive now."""
        self.peak = len(self.alive)


DAY_TRACKER = DayTracker()


def default_model(world: World) -> NetworkModel:
    """The network as built, with every parameter at its default.

    GSM transceivers are dimensioned from the served users (dimension_gsm)
    and every coverage-overlap relation is configured as a neighbour.

    Args:
        world: The world.

    Returns:
        The network model.
    """
    grid = build_grid(world)
    state = build_cell_state(world)
    bands = sorted({str(b) for b in state.band})
    coverage = {b: layer_coverage(state, grid, b) for b in bands}
    serving = build_serving(state, grid, coverage, grid.persons)
    state = state.with_values(trx=dimension_gsm(state, serving))
    src, tgt, _ = serving.relations
    lte = state.technology == "LTE"
    return NetworkModel(
        world=world,
        state=state,
        grid=grid,
        persons=grid.persons,
        coverage=coverage,
        serving=serving,
        neighbours=frozenset(zip(src.tolist(), tgt.tolist(), strict=True)),
        ul_rise_db=np.zeros(len(state.cell_names)),
        shift_totals=serving.subscribers_by_class[lte].sum(axis=0),
    )


def derive(
    model: NetworkModel,
    state: CellState,
    neighbours: frozenset[Relation],
    persons: np.ndarray,
    ul_rise_db: np.ndarray,
) -> NetworkModel:
    """A changed network, recomputing only what the change touches.

    A band's coverage is recomputed within LOCAL_RADIUS_KM of any of its
    cells that changed a parameter reaching coverage (tilt, power, offset,
    service state); that recomputes signal and interference coupling for
    every point the change can reach.
    Serving is recomputed when coverage or persons changed.

    Args:
        model: The network before the change.
        state: New cell parameters.
        neighbours: New configured neighbour relations.
        persons: New persons per grid point.
        ul_rise_db: New uplink noise rise per cell.

    Returns:
        The changed network.
    """
    old = model.state
    changed = (
        (old.tilt_deg != state.tilt_deg)
        | (old.power_dbm != state.power_dbm)
        | (old.cio_db != state.cio_db)
        | (old.down != state.down)
    )
    coverage = dict(model.coverage)
    for band in sorted({str(b) for b in state.band[changed]}):
        if not ((state.band == band) & ~state.down).any():
            coverage.pop(band)
            continue
        near = points_near(model.grid, state, np.flatnonzero(changed & (state.band == band)))
        if band in coverage:
            coverage[band] = fill_points(coverage[band], state, model.grid, near)
        else:
            coverage[band] = layer_coverage(state, model.grid, band)
    same_persons = persons is model.persons or np.array_equal(persons, model.persons)
    serving = model.serving
    if changed.any() or not same_persons:
        serving = build_serving(state, model.grid, coverage, persons)
    return replace(
        model,
        state=state,
        persons=persons,
        coverage=coverage,
        serving=serving,
        neighbours=neighbours,
        ul_rise_db=ul_rise_db,
    )


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


def plan_relations(model: NetworkModel, columns: list[Relation]) -> RelationPlan:
    """Handover demand and configured relations of a network.

    Args:
        model: The network.
        columns: Relation columns of the per-relation counters.

    Returns:
        The relation plan the counters read.
    """
    return relation_plan(model.state, model.serving, model.neighbours, columns)


def day_counters(model: NetworkModel, day: int, plan: RelationPlan, starts: list[datetime]) -> Day:
    """Counters of one network configuration for one day.

    Args:
        model: The network.
        day: Day index from RUN_START.
        plan: Relation plan of the network.
        starts: Period starts of the day.

    Returns:
        The day's counters.
    """
    class_load = activity_by_class(starts) * population_shift(starts, model.shift_totals)
    return Day(
        index=day,
        starts=starts,
        lte=lte_day(model.state, model.serving, class_load, day, plan, model.ul_rise_db),
        gsm=gsm_day(model.state, model.serving, class_load, day, plan),
    )


def day_starts(day: int) -> list[datetime]:
    """Local period starts of a day.

    Args:
        day: Day index from RUN_START.

    Returns:
        The period starts.
    """
    return period_starts(RUN_START + timedelta(days=day), PERIODS_PER_DAY)


def simulate_days(model: NetworkModel, first_day: int, n_days: int) -> Iterator[Day]:
    """Counters for consecutive days of one network configuration.

    Args:
        model: The network model.
        first_day: Index of the first day from RUN_START.
        n_days: Number of days.

    Yields:
        One Day at a time, so a long run never holds every period in memory.
    """
    plan = plan_relations(model, sorted(model.neighbours))
    for day in range(first_day, first_day + n_days):
        yield day_counters(model, day, plan, day_starts(day))
