"""How far each planted fault reaches (rule F1: faults stay local).

A fault's day is replayed with and without it on the same random draws
(common random numbers). A cell is affected when one of its access or
retainability KPIs moves by more than AFFECTED_POINTS over that day: RRC
setup success, E-RAB accessibility or E-RAB drop rate for LTE, service
access success or TCH blocking for GSM; the faulty cell always counts. The
fault's reach is the affected cells' share of their technology's access
attempts that day (RRC.ConnEstabAtt for LTE, attImmediateAssingProcs for
GSM), the larger of the two. No scheduled fault may reach more than
MAX_SHARE.
"""

from dataclasses import dataclass

import numpy as np

from ran_lakehouse.faults.plant import Fault, apply_faults
from ran_lakehouse.model import (
    RUN_START,
    Day,
    NetworkModel,
    day_counters,
    day_starts,
    plan_relations,
)

AFFECTED_POINTS = 1.0  # KPI change, percentage points (START)
MAX_SHARE = 0.02  # of a technology's access attempts in a day (START)


@dataclass(frozen=True)
class Reach:
    """How far one fault reaches.

    Attributes:
        fault_id: The fault.
        kind: Its kind.
        cells: Cells affected.
        share: Largest share of a technology's access attempts they carry.
    """

    fault_id: str
    kind: str
    cells: int
    share: float


def percent(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """100 * num / den per cell over a day, NaN where den is 0.

    Args:
        num: Numerator, shape (periods, cells).
        den: Denominator, same shape.

    Returns:
        Per-cell percentage.
    """
    n = np.nansum(num, axis=0)
    d = np.nansum(den, axis=0)
    out = np.full(n.shape, np.nan)
    np.divide(100 * n, d, out=out, where=d > 0)
    return out


def lte_kpis(day: Day) -> list[np.ndarray]:
    """Per-cell daily LTE KPIs compared for reach.

    Args:
        day: The day's counters.

    Returns:
        RRC setup success, E-RAB accessibility and E-RAB drop rate (%).
    """
    v = day.lte.values
    rrc = percent(v["RRC.ConnEstabSucc.sum"], v["RRC.ConnEstabAtt.sum"])
    s1 = percent(v["S1SIG.ConnEstabSucc"], v["S1SIG.ConnEstabAtt"])
    erab = percent(v["ERAB.EstabInitSuccNbr.sum"], v["ERAB.EstabInitAttNbr.sum"])
    drop = percent(v["ERAB.RelActNbr.sum"], v["ERAB.EstabInitSuccNbr.sum"])
    return [rrc, rrc * s1 * erab / 10_000, drop]


def gsm_kpis(day: Day) -> list[np.ndarray]:
    """Per-cell daily GSM KPIs compared for reach.

    Args:
        day: The day's counters.

    Returns:
        Service access success and TCH blocking (%).
    """
    g = day.gsm.values
    tch = percent(g["succTCHSeizures"], g["attTCHSeizures"])
    ia = percent(g["succImmediateAssingProcs"], g["attImmediateAssingProcs"])
    blocked = g["attTCHSeizuresMeetingTCHBlockedState"]
    block = percent(blocked, g["attTCHSeizures"] + blocked)
    return [tch * ia / 100, block]


def affected(clean: list[np.ndarray], faulty: list[np.ndarray]) -> np.ndarray:
    """Cells where any KPI moved by more than AFFECTED_POINTS.

    Args:
        clean: Per-cell KPIs without the fault.
        faulty: Per-cell KPIs with the fault.

    Returns:
        Mask per cell; a KPI that has no value in one run counts as moved.
    """
    moved = np.zeros(clean[0].shape, dtype=bool)
    for a, b in zip(clean, faulty, strict=True):
        gap = np.isnan(a) != np.isnan(b)
        moved |= gap | (np.nan_to_num(np.abs(a - b)) > AFFECTED_POINTS)
    return moved


def reach(base: NetworkModel, fault: Fault) -> Reach:
    """How far a fault reaches on the first whole day it is present.

    Args:
        base: The network as built.
        fault: The fault.

    Returns:
        Its reach.
    """
    day = (fault.start - RUN_START).days + 1
    starts = day_starts(day)
    columns = sorted(base.neighbours)
    faulty_model = apply_faults(base, [fault])
    clean = day_counters(base, day, plan_relations(base, columns), starts)
    faulty = day_counters(faulty_model, day, plan_relations(faulty_model, columns), starts)
    shares = []
    cells = 0
    for kpis, counters, attempts in (
        (lte_kpis, "lte", "RRC.ConnEstabAtt.sum"),
        (gsm_kpis, "gsm", "attImmediateAssingProcs"),
    ):
        mask = affected(kpis(clean), kpis(faulty))
        ids = getattr(clean, counters).cells
        mask |= ids == fault.cell
        load = np.nansum(getattr(clean, counters).values[attempts], axis=0)
        cells += int(mask.sum())
        shares.append(float(load[mask].sum() / load.sum()))
    return Reach(fault.fault_id, fault.kind, cells, max(shares))
