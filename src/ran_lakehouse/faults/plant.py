"""Planting faults (rules F1-F4): the schedule, the changed network per
active fault set, and the CM change log and FM alarm log they leave.

Magnitudes, durations and counts are START values or ASSUMPTION.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from ran_lakehouse.model import RUN_START, NetworkModel, derive
from ran_lakehouse.model.cells import band_centre_mhz
from ran_lakehouse.model.counters import UL_NOISE_FLOOR_DBM
from ran_lakehouse.model.radio import UE_HEIGHT_M, antenna_gain_db, hata_path_loss_db
from ran_lakehouse.seeds import Purpose, rng

KINDS = ("F1a", "F1b", "F1c", "F1d", "F1e", "F1f")

# F1a: the tilt is set to this value by mistake, so the cell overshoots
# (START).
MISTAKEN_TILT_DEG = 0.0
POWER_DROP_DB = -6.0  # F1c (START)
SURGE_FACTOR = 2.5  # F1d: persons multiplied around the cell (START)
SURGE_RADIUS_KM = 0.6
# F1e: interference source power per PRB-wide band, dBm, at ground level
# (START), placed this far along the cell's azimuth.
INTERFERER_DBM = 15.0
INTERFERER_DISTANCE_KM = (0.3, 0.8)
INTERFERER_ANGLE_DEG = 20.0
# Durations in hours (START): (minimum, maximum).
DURATION_H = {
    "F1a": (48, 144),
    "F1b": (48, 144),
    "F1c": (48, 144),
    "F1d": (24, 72),
    "F1e": (48, 168),
    "F1f": (3, 12),
}
# Hour windows in which each kind starts (ASSUMPTION): configuration
# mistakes in the maintenance window, surges in the day, the rest any time.
START_HOURS = {
    "F1a": (1, 5),
    "F1b": (1, 5),
    "F1c": (1, 5),
    "F1d": (8, 18),
    "F1e": (0, 24),
    "F1f": (0, 24),
}
FAULTS_PER_BUSY_WEEK = (3, 6)  # rule F3, START
QUIET_WEEKS = 3  # weeks without a planted fault (START)
MIN_RELATIONS_F1B = 3  # a cell needs this many configured relations for F1b

CAUSE = {
    "F1a": "electrical tilt changed by mistake; the cell overshoots",
    "F1b": "neighbour relation deleted from the neighbour list",
    "F1c": "transmit power reduced after maintenance",
    "F1d": "traffic surge in the cell's area",
    "F1e": "external uplink interference source",
    "F1f": "cell out of service",
}
RIGHT_ANSWER = {
    "F1a": "restore tilt",
    "F1b": "add neighbour",
    "F1c": "restore power",
    "F1d": "load-balancing offset or capacity note",
    "F1e": "no parameter fix, field visit",
    "F1f": "no parameter fix, alarm-driven",
}


@dataclass(frozen=True)
class Fault:
    """One planted fault.

    Attributes:
        fault_id: Stable id, "F0001".
        kind: F1a to F1f.
        cell: Global index of the faulty cell.
        start: Local start time.
        end: Local end time (the fault is gone from this time on).
        target: F1b: the neighbour whose relation is deleted; else -1.
        x_km: F1d surge centre or F1e source position, x; else NaN.
        y_km: Same, y.
    """

    fault_id: str
    kind: str
    cell: int
    start: datetime
    end: datetime
    target: int
    x_km: float
    y_km: float

    def active_at(self, t: datetime) -> bool:
        """Whether the fault is present at a time.

        Args:
            t: Local time.

        Returns:
            True from start (inclusive) to end (exclusive).
        """
        return self.start <= t < self.end


def footprint_centre(model: NetworkModel, cell: int) -> tuple[float, float]:
    """Persons-weighted centre of the points a cell serves.

    Args:
        model: The network.
        cell: Global cell index.

    Returns:
        (x, y) in km.

    Raises:
        ValueError: If the cell serves no point.
    """
    layer = model.coverage[str(model.state.band[cell])]
    served = layer.best == cell
    weight = model.persons[served]
    if weight.sum() <= 0:
        raise ValueError(f"cell {cell} serves no point")
    x = float((model.grid.x_km[served] * weight).sum() / weight.sum())
    y = float((model.grid.y_km[served] * weight).sum() / weight.sum())
    return x, y


def strongest_neighbour(model: NetworkModel, cell: int) -> int:
    """The configured neighbour with the most handover demand from a cell.

    Args:
        model: The network.
        cell: Global cell index.

    Returns:
        Global index of the target.

    Raises:
        ValueError: If the cell has no configured neighbour.
    """
    src, tgt, mass = model.serving.relations
    mine = np.flatnonzero(src == cell)
    mine = np.array([i for i in mine if (cell, int(tgt[i])) in model.neighbours], dtype=int)
    if mine.size == 0:
        raise ValueError(f"cell {cell} has no configured neighbour")
    return int(tgt[mine[np.argmax(mass[mine])]])


def eligible(model: NetworkModel, kind: str) -> np.ndarray:
    """Cells a fault kind can be planted on.

    Args:
        model: The network.
        kind: Fault kind.

    Returns:
        Global cell indices, ascending.
    """
    state = model.state
    ok = (state.technology == "LTE") & ~state.down & (model.serving.subscribers > 1.0)
    if kind == "F1b":
        count = np.zeros(len(state.cell_names), dtype=int)
        for s, t in model.neighbours:
            if state.technology[s] == "LTE" and state.technology[t] == "LTE":
                count[s] += 1
        ok &= count >= MIN_RELATIONS_F1B
    if kind == "F1a":
        # Overshoot damages the dense city grid; on flat synthetic terrain a
        # suburban or rural uptilt barely reaches past the first neighbours.
        ok &= state.area_class == "urban"
    if kind == "F1d":
        # The towns, on the suburban lattice, are the network's hotspots.
        ok &= state.area_class == "suburban"
    if kind == "F1f":
        ok = ~state.down & (model.serving.subscribers > 1.0)
    return np.flatnonzero(ok)


def plan_faults(model: NetworkModel, weeks: int) -> list[Fault]:
    """The fault schedule of a run (rule F3), deterministic per seed.

    Quiet weeks carry no fault; busy weeks carry 3 to 6 (START), started at
    random days and hours within their kind's window, so faults overlap in
    some weeks. A cell never carries two faults at once.

    Args:
        model: The network as built.
        weeks: Weeks in the run.

    Returns:
        Faults in start order.
    """
    plan_rng = rng(Purpose.FAULT_PLAN, 0)
    quiet = set(plan_rng.choice(weeks, size=min(QUIET_WEEKS, weeks - 1), replace=False).tolist())
    busy: dict[int, list[tuple[datetime, datetime]]] = {}
    faults: list[Fault] = []
    for week in range(weeks):
        if week in quiet:
            continue
        week_rng = rng(Purpose.FAULT_PLAN, week + 1)
        low, high = FAULTS_PER_BUSY_WEEK
        for _ in range(int(week_rng.integers(low, high + 1))):
            kind = KINDS[int(week_rng.integers(len(KINDS)))]
            h0, h1 = START_HOURS[kind]
            day = week * 7 + int(week_rng.integers(7))
            minutes = int(week_rng.integers(h0 * 4, h1 * 4)) * 15
            start = RUN_START + timedelta(days=day, minutes=minutes)
            d0, d1 = DURATION_H[kind]
            end = start + timedelta(minutes=15 * int(week_rng.integers(d0 * 4, d1 * 4 + 1)))
            candidates = free_cells(model, kind, busy, start, end)
            if not candidates:
                # The profile has no free cell for this kind (the tiny
                # profile has no urban cell for F1a): draw among kinds that do.
                open_kinds = [k for k in KINDS if free_cells(model, k, busy, start, end)]
                if not open_kinds:
                    raise RuntimeError(f"no free cell for any fault kind at {start}")
                kind = open_kinds[int(week_rng.integers(len(open_kinds)))]
                candidates = free_cells(model, kind, busy, start, end)
            cell = candidates[int(week_rng.integers(len(candidates)))]
            fault_id = f"F{len(faults) + 1:04d}"
            faults.append(detail(model, fault_id, kind, cell, start, end))
            busy.setdefault(cell, []).append((start, end))
            if faults[-1].target >= 0:
                busy.setdefault(faults[-1].target, []).append((start, end))
    return sorted(faults, key=lambda f: (f.start, f.fault_id))


def free_cells(
    model: NetworkModel,
    kind: str,
    busy: dict[int, list[tuple[datetime, datetime]]],
    start: datetime,
    end: datetime,
) -> list[int]:
    """Eligible cells of a kind with no other fault in a time window.

    Args:
        model: The network as built.
        kind: Fault kind.
        busy: Cell to the windows of faults already planted on it.
        start: Window start.
        end: Window end.

    Returns:
        Global cell indices, ascending.
    """
    return [
        int(c)
        for c in eligible(model, kind)
        if all(e <= start or s >= end for s, e in busy.get(int(c), []))
    ]


def detail(
    model: NetworkModel, fault_id: str, kind: str, cell: int, start: datetime, end: datetime
) -> Fault:
    """Fill in a fault's kind-specific details.

    Args:
        model: The network as built.
        fault_id: Fault id.
        kind: Fault kind.
        cell: Global cell index.
        start: Local start time.
        end: Local end time.

    Returns:
        The fault.
    """
    target, x, y = -1, float("nan"), float("nan")
    if kind == "F1b":
        target = strongest_neighbour(model, cell)
    if kind == "F1d":
        x, y = footprint_centre(model, cell)
    if kind == "F1e":
        stream = rng(Purpose.FAULT_DETAIL, int(fault_id[1:]))
        distance = float(stream.uniform(*INTERFERER_DISTANCE_KM))
        bearing = np.radians(
            model.state.azimuth_deg[cell]
            + stream.uniform(-INTERFERER_ANGLE_DEG, INTERFERER_ANGLE_DEG)
        )
        x = float(model.state.x_km[cell] + distance * np.sin(bearing))
        y = float(model.state.y_km[cell] + distance * np.cos(bearing))
        x = float(np.clip(x, 0.0, model.world.profile.served_width_km - 0.001))
        y = float(np.clip(y, 0.0, model.world.profile.height_km - 0.001))
    return Fault(fault_id, kind, cell, start, end, target, x, y)


def uplink_rise_db(model: NetworkModel, fault: Fault) -> np.ndarray:
    """Uplink noise rise an interference source causes at every cell.

    Only cells of the faulty cell's band hear the source (a narrowband
    source in that uplink band, ASSUMPTION). The uplink path loss uses the
    band's downlink centre frequency (ASSUMPTION).

    Args:
        model: The network.
        fault: An F1e fault.

    Returns:
        Rise per cell in dB (0 for other bands).
    """
    state = model.state
    band = str(state.band[fault.cell])
    cells = np.flatnonzero((state.band == band) & ~state.down)
    dx = fault.x_km - state.x_km[cells]
    dy = fault.y_km - state.y_km[cells]
    d_km = np.hypot(dx, dy)
    bearing = np.degrees(np.arctan2(dx, dy))
    height = state.height_m[cells]
    elevation = np.degrees(np.arctan2(height - UE_HEIGHT_M, np.maximum(d_km, 0.001) * 1000.0))
    gain = antenna_gain_db(bearing - state.azimuth_deg[cells], elevation - state.tilt_deg[cells])
    grid = model.grid
    point = int(np.argmin(np.hypot(grid.x_km - fault.x_km, grid.y_km - fault.y_km)))
    loss = hata_path_loss_db(
        band_centre_mhz(band),
        d_km,
        height,
        np.full(cells.size, grid.urban_weight[point]),
        np.full(cells.size, grid.suburban_weight[point]),
    )
    received = INTERFERER_DBM + gain - loss
    rise = np.zeros(len(state.cell_names))
    rise[cells] = 10.0 * np.log10(1.0 + 10.0 ** ((received - UL_NOISE_FLOOR_DBM) / 10.0))
    return rise


def apply_faults(base: NetworkModel, faults: list[Fault]) -> NetworkModel:
    """The network with a set of faults present.

    Args:
        base: The network as built.
        faults: The faults present.

    Returns:
        The changed network.
    """
    tilt = base.state.tilt_deg.copy()
    power = base.state.power_dbm.copy()
    down = base.state.down.copy()
    neighbours = set(base.neighbours)
    persons = base.persons.copy()
    interference = np.zeros(len(base.state.cell_names))
    for fault in faults:
        if fault.kind == "F1a":
            tilt[fault.cell] = MISTAKEN_TILT_DEG
        elif fault.kind == "F1b":
            neighbours.discard((fault.cell, fault.target))
        elif fault.kind == "F1c":
            power[fault.cell] += POWER_DROP_DB
        elif fault.kind == "F1d":
            near = np.hypot(base.grid.x_km - fault.x_km, base.grid.y_km - fault.y_km)
            persons = np.where(near <= SURGE_RADIUS_KM, persons * SURGE_FACTOR, persons)
        elif fault.kind == "F1e":
            interference += 10.0 ** (uplink_rise_db(base, fault) / 10.0) - 1.0
        elif fault.kind == "F1f":
            down[fault.cell] = True
        else:
            raise ValueError(f"unknown fault kind {fault.kind}")
    state = base.state.with_values(tilt_deg=tilt, power_dbm=power, down=down)
    rise = 10.0 * np.log10(1.0 + interference)
    return derive(base, state, frozenset(neighbours), persons, rise)
