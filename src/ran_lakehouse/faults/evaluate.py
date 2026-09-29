"""The right answer's recovery per fault (rule F4) and the evaluation-only
answers table.

Each fault is replayed for a week from its start day, three ways with
common random numbers (rule M6): the clean network, the faulty network,
and the faulty network after the right fix. Faults with a fix are held
present for the week so the fix acts on them; an outage (F1f), which has
no fix, runs over its own window. Recovery on the
kind's primary KPI is (fixed - faulty) / (clean - faulty): 1 means the
fix restores the clean value, 0 means no effect, below 0 means worse.

The answers table is evaluation-only (rule A3): it is built in memory here
for the scoring entry point and the lake's evaluation table, and is never
written to results/ or served by the API.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from ran_lakehouse.faults.plant import (
    CAUSE,
    MISTAKEN_TILT_DEG,
    POWER_DROP_DB,
    RIGHT_ANSWER,
    Fault,
    apply_faults,
)
from ran_lakehouse.faults.simulate import simulate_with_faults
from ran_lakehouse.faults.whatif import (
    MAX_CIO_STEP_DB,
    MAX_POWER_STEP_DB,
    MAX_TILT_STEP_DEG,
    REPLAY_DAYS,
    Change,
    apply_changes,
    lte_kpis,
    replay,
    set_kpis,
    touched_cells,
)
from ran_lakehouse.model import RUN_START, Day, NetworkModel

PRIMARY_KPI = {
    "F1a": "E-RAB drop rate (%)",
    "F1b": "E-RAB drop rate (%)",
    "F1c": "DL IP throughput (kbit/s)",
    "F1d": "DL IP throughput (kbit/s)",
    "F1e": "RRC setup success (%)",
    "F1f": "Cell availability (%)",
}
# Cells the primary KPI is measured over: the faulty cell alone, or every
# cell the fault or the fix touches.
MEASURED_OVER = {
    "F1a": "area",
    "F1b": "cell",
    "F1c": "area",
    "F1d": "cell",
    "F1e": "cell",
    "F1f": "cell",
}
# The load-balancing offset for F1d: two steps down on the congested cell.
F1D_OFFSET_STEPS = 2


def steps(total: float, step: float) -> list[float]:
    """Split a change into bounded steps.

    Args:
        total: Total change.
        step: Largest step.

    Returns:
        Steps summing to total, each within +/-step.
    """
    n = int(np.ceil(abs(total) / step - 1e-9))
    return [total / n] * n if n else []


def right_fix(base: NetworkModel, fault: Fault) -> list[Change]:
    """The right answer as bounded what-if changes (rule M6).

    Args:
        base: The network as built.
        fault: The fault.

    Returns:
        The changes; empty for faults with no parameter fix (F1e, F1f).
    """
    if fault.kind == "F1a":
        total = float(base.state.tilt_deg[fault.cell]) - MISTAKEN_TILT_DEG
        return [Change("tilt", fault.cell, d, -1) for d in steps(total, MAX_TILT_STEP_DEG)]
    if fault.kind == "F1b":
        return [Change("add_neighbour", fault.cell, 0.0, fault.target)]
    if fault.kind == "F1c":
        return [
            Change("power", fault.cell, d, -1) for d in steps(-POWER_DROP_DB, MAX_POWER_STEP_DB)
        ]
    if fault.kind == "F1d":
        return [Change("cio", fault.cell, -MAX_CIO_STEP_DB, -1)] * F1D_OFFSET_STEPS
    return []


def parameter_tries(fault: Fault) -> list[list[Change]]:
    """Every single bounded parameter change on the faulty cell.

    Args:
        fault: The fault.

    Returns:
        One change list per try.
    """
    c = fault.cell
    return [
        [Change("tilt", c, MAX_TILT_STEP_DEG, -1)],
        [Change("tilt", c, -MAX_TILT_STEP_DEG, -1)],
        [Change("power", c, MAX_POWER_STEP_DB, -1)],
        [Change("power", c, -MAX_POWER_STEP_DB, -1)],
        [Change("cio", c, -MAX_CIO_STEP_DB, -1)],
        [Change("cio", c, MAX_CIO_STEP_DB, -1)],
    ]


def recovery(clean: float, faulty: float, fixed: float) -> float:
    """Share of the fault's damage a fix undoes.

    Args:
        clean: KPI without the fault.
        faulty: KPI with the fault.
        fixed: KPI with the fault and the fix.

    Returns:
        (fixed - faulty) / (clean - faulty); NaN when the fault did no
        measurable damage.
    """
    damage = clean - faulty
    if not np.isfinite(damage) or abs(damage) < 1e-9:
        return float("nan")
    return (fixed - faulty) / damage


@dataclass(frozen=True)
class Evaluation:
    """One fault replayed clean, faulty and fixed.

    Attributes:
        fault: The fault.
        area_cells: The faulty cell and every cell the fault or the fix
            touches.
        clean: KPIs without the fault, keyed "cell" (the faulty cell) and
            "area" (area_cells).
        faulty: KPIs with the fault, same keys.
        fixed: KPIs after the right fix (equal to faulty when there is
            none), same keys.
        recovery: Recovery on the primary KPI, measured over the cell or
            the area (MEASURED_OVER).
        area_recovery: Recovery on the primary KPI over the area, which
            shows what a fix on one cell costs its neighbours.
        tries: For faults without a parameter fix, (changes, recovery) of
            every single bounded parameter change.
    """

    fault: Fault
    area_cells: np.ndarray
    clean: dict[str, dict[str, float]]
    faulty: dict[str, dict[str, float]]
    fixed: dict[str, dict[str, float]]
    recovery: float
    area_recovery: float
    tries: list[tuple[list[Change], float]]


def evaluate(base: NetworkModel, fault: Fault, try_parameters: bool) -> Evaluation:
    """Replay a fault's week clean, faulty and fixed.

    Args:
        base: The network as built.
        fault: The fault.
        try_parameters: Also replay every single parameter change on the
            faulty cell (for faults without a parameter fix).

    Returns:
        The evaluation.
    """
    first_day = (fault.start - RUN_START).days
    faulty_model = apply_faults(base, [fault])
    fix = right_fix(base, fault)
    fixed_model = apply_changes(faulty_model, fix) if fix else faulty_model
    columns = sorted(base.neighbours | fixed_model.neighbours)
    changed = {fault.cell} | {c.target for c in fix if c.target >= 0}
    area = np.union1d(
        touched_cells(base, faulty_model, changed),
        touched_cells(faulty_model, fixed_model, changed),
    )
    sets = {"cell": np.array([fault.cell]), "area": area}
    if fault.kind == "F1f":
        faulty_days: Iterable[Day] = simulate_with_faults(base, [fault], first_day, REPLAY_DAYS)
    else:
        faulty_days = replay(faulty_model, first_day, columns)
    kpis = {
        "clean": set_kpis(replay(base, first_day, columns), sets),
        "faulty": set_kpis(faulty_days, sets),
    }
    kpis["fixed"] = (
        set_kpis(replay(fixed_model, first_day, columns), sets) if fix else kpis["faulty"]
    )
    kpi = PRIMARY_KPI[fault.kind]
    over = MEASURED_OVER[fault.kind]

    def primary(run: str) -> float:
        return kpis[run][over][kpi]

    tries: list[tuple[list[Change], float]] = []
    if try_parameters:
        for changes in parameter_tries(fault):
            tried = lte_kpis(
                replay(apply_changes(faulty_model, changes), first_day, columns), sets[over]
            )
            tries.append((changes, recovery(primary("clean"), primary("faulty"), tried[kpi])))
    return Evaluation(
        fault=fault,
        area_cells=area,
        clean=kpis["clean"],
        faulty=kpis["faulty"],
        fixed=kpis["fixed"],
        recovery=recovery(primary("clean"), primary("faulty"), primary("fixed")),
        area_recovery=recovery(
            kpis["clean"]["area"][kpi], kpis["faulty"]["area"][kpi], kpis["fixed"]["area"][kpi]
        ),
        tries=tries,
    )


def answers(base: NetworkModel, evaluations: list[Evaluation]) -> list[dict[str, Any]]:
    """The evaluation-only answers table (rule F4), one row per fault.

    Args:
        base: The network as built.
        evaluations: Evaluations of the planted faults.

    Returns:
        Rows: fault id, kind, cell, DN, start, end, cause, right answer, the
        primary KPI and the recovery the right fix achieves in the what-if.
    """
    return [
        {
            "fault_id": e.fault.fault_id,
            "kind": e.fault.kind,
            "cell": base.state.cell_names[e.fault.cell],
            "dn": base.world.cells[e.fault.cell].dn,
            "start": e.fault.start.isoformat(),
            "end": e.fault.end.isoformat(),
            "cause": CAUSE[e.fault.kind],
            "right_answer": RIGHT_ANSWER[e.fault.kind],
            "primary_kpi": PRIMARY_KPI[e.fault.kind],
            "recovery": None if np.isnan(e.recovery) else round(e.recovery, 3),
        }
        for e in evaluations
    ]
