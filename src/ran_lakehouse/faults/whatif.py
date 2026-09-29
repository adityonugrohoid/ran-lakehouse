"""What-if (rule M6): apply bounded changes and replay the next week with
the same random noise, for the changed cell and every cell whose load or
interference it touches.

Bounds per change (rule M6): tilt within +/-2 degrees, power within
+/-3 dB, add or remove one neighbour relation, cell individual offset
within +/-6 dB (ASSUMPTION for the offset range) and moved by at most
3 dB per change (ASSUMPTION).
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from ran_lakehouse.model import Day, NetworkModel, day_counters, day_starts, derive, plan_relations

MAX_TILT_STEP_DEG = 2.0
MAX_POWER_STEP_DB = 3.0
MAX_CIO_STEP_DB = 3.0
CIO_RANGE_DB = 6.0
REPLAY_DAYS = 7
# A cell counts as touched when a change moves its served users by more
# than this share, its mean SINR by more than this, or its neighbour list
# (ASSUMPTION).
TOUCH_SUBSCRIBER_SHARE = 0.01
TOUCH_SINR_DB = 0.1


@dataclass(frozen=True)
class Change:
    """One bounded parameter change.

    Attributes:
        kind: "tilt", "power", "cio", "add_neighbour" or "remove_neighbour".
        cell: Global index of the changed cell (the source for neighbours).
        delta: Change of tilt (deg), power (dB) or offset (dB); 0 for
            neighbour changes.
        target: Neighbour target for neighbour changes; -1 otherwise.
    """

    kind: str
    cell: int
    delta: float
    target: int


def apply_changes(model: NetworkModel, changes: list[Change]) -> NetworkModel:
    """The network after a sequence of bounded changes.

    Args:
        model: The network before.
        changes: Changes, applied in order.

    Returns:
        The changed network.

    Raises:
        ValueError: If a change breaks its bound or does not apply.
    """
    tilt = model.state.tilt_deg.copy()
    power = model.state.power_dbm.copy()
    cio = model.state.cio_db.copy()
    neighbours = set(model.neighbours)
    for c in changes:
        if c.kind == "tilt":
            if abs(c.delta) > MAX_TILT_STEP_DEG:
                raise ValueError(f"tilt step {c.delta} beyond +/-{MAX_TILT_STEP_DEG} deg")
            tilt[c.cell] += c.delta
        elif c.kind == "power":
            if abs(c.delta) > MAX_POWER_STEP_DB:
                raise ValueError(f"power step {c.delta} beyond +/-{MAX_POWER_STEP_DB} dB")
            power[c.cell] += c.delta
        elif c.kind == "cio":
            new = cio[c.cell] + c.delta
            if abs(c.delta) > MAX_CIO_STEP_DB or abs(new) > CIO_RANGE_DB:
                raise ValueError(f"offset change {c.delta} to {new} beyond the bounds")
            cio[c.cell] = new
        elif c.kind == "add_neighbour":
            if (c.cell, c.target) in neighbours:
                raise ValueError(f"relation {c.cell}->{c.target} already configured")
            neighbours.add((c.cell, c.target))
        elif c.kind == "remove_neighbour":
            if (c.cell, c.target) not in neighbours:
                raise ValueError(f"relation {c.cell}->{c.target} is not configured")
            neighbours.discard((c.cell, c.target))
        else:
            raise ValueError(f"unknown change kind {c.kind}")
    state = model.state.with_values(tilt_deg=tilt, power_dbm=power, cio_db=cio)
    return derive(model, state, frozenset(neighbours), model.persons, model.ul_rise_db)


def touched_cells(before: NetworkModel, after: NetworkModel, changed: set[int]) -> np.ndarray:
    """Cells whose load or interference a change touches.

    Args:
        before: The network before.
        after: The network after.
        changed: Cells changed directly (always included).

    Returns:
        Global cell indices, ascending.
    """
    b, a = before.serving, after.serving
    share = np.abs(a.subscribers - b.subscribers) / np.maximum(b.subscribers, 1.0)
    moved = (share > TOUCH_SUBSCRIBER_SHARE) | (
        np.abs(a.mean_sinr_db - b.mean_sinr_db) > TOUCH_SINR_DB
    )
    cells = set(np.flatnonzero(moved).tolist()) | changed
    return np.array(sorted(cells), dtype=int)


def replay(model: NetworkModel, first_day: int, columns: list[tuple[int, int]]) -> Iterator[Day]:
    """A week of counters of one network, with the run's common noise.

    Args:
        model: The network.
        first_day: First day of the week.
        columns: Per-relation counter columns.

    Yields:
        One day at a time, so a replay never holds the week in memory.
    """
    plan = plan_relations(model, columns)
    for d in range(first_day, first_day + REPLAY_DAYS):
        yield day_counters(model, d, plan, day_starts(d))


TOTALS = (
    "RRC.ConnEstabAtt.sum",
    "RRC.ConnEstabSucc.sum",
    "S1SIG.ConnEstabAtt",
    "S1SIG.ConnEstabSucc",
    "ERAB.EstabInitAttNbr.sum",
    "ERAB.EstabInitSuccNbr.sum",
    "ERAB.RelActNbr.sum",
    "DRB.IPVolDl.sum",
    "DRB.IPTimeDl.sum",
    "HO.IntraFreqOutAtt",
    "HO.IntraFreqOutSucc",
    "RRU.CellUnavailableTime.sum",
    "RRU.PrbTotDl",
    "RRC.ConnMean",
)


class KpiTotals:
    """Sums of the counters the LTE KPIs need, over some cells and days."""

    def __init__(self, cells: np.ndarray) -> None:
        """Start empty.

        Args:
            cells: Global cell indices; non-LTE cells are ignored.
        """
        self.cells = cells
        self.sums = dict.fromkeys(TOTALS, 0.0)
        self.periods = 0
        self.cell_periods = 0

    def add(self, day: Day) -> None:
        """Add one day.

        Args:
            day: Simulated day.
        """
        columns = np.isin(day.lte.cells, self.cells)
        for name in TOTALS:
            self.sums[name] += float(day.lte.values[name][:, columns].sum())
        periods = day.lte.values["RRC.ConnMean"].shape[0]
        self.periods += periods
        self.cell_periods += periods * int(columns.sum())

    def kpis(self) -> dict[str, float]:
        """LTE KPIs from the sums (ratio of sums, rule L3).

        Returns:
            KPI name to value; NaN where the denominator is zero.
        """
        t = self.sums

        def ratio(num: float, den: float, scale: float) -> float:
            return scale * num / den if den > 0 else float("nan")

        period_time = 900.0 * self.cell_periods
        rrc = ratio(t["RRC.ConnEstabSucc.sum"], t["RRC.ConnEstabAtt.sum"], 1.0)
        s1 = ratio(t["S1SIG.ConnEstabSucc"], t["S1SIG.ConnEstabAtt"], 1.0)
        erab = ratio(t["ERAB.EstabInitSuccNbr.sum"], t["ERAB.EstabInitAttNbr.sum"], 1.0)
        return {
            "E-RAB accessibility (%)": 100.0 * rrc * s1 * erab,
            "RRC setup success (%)": 100.0 * rrc,
            "E-RAB drop rate (%)": ratio(
                t["ERAB.RelActNbr.sum"], t["ERAB.EstabInitSuccNbr.sum"], 100.0
            ),
            "DL IP throughput (kbit/s)": ratio(t["DRB.IPVolDl.sum"], t["DRB.IPTimeDl.sum"], 1000.0),
            "Handover success (%)": ratio(t["HO.IntraFreqOutSucc"], t["HO.IntraFreqOutAtt"], 100.0),
            "Cell availability (%)": ratio(
                period_time - t["RRU.CellUnavailableTime.sum"], period_time, 100.0
            ),
            "Mean DL PRB use (%)": ratio(t["RRU.PrbTotDl"], float(self.cell_periods), 1.0),
            "RRC connections, mean": ratio(t["RRC.ConnMean"], float(self.periods), 1.0),
        }


def set_kpis(days: Iterable[Day], sets: dict[str, np.ndarray]) -> dict[str, dict[str, float]]:
    """LTE KPIs over several cell sets in one pass over the days.

    Args:
        days: Simulated days (consumed once).
        sets: Name to global cell indices.

    Returns:
        Set name to KPIs.
    """
    totals = {name: KpiTotals(cells) for name, cells in sets.items()}
    for day in days:
        for t in totals.values():
            t.add(day)
    return {name: t.kpis() for name, t in totals.items()}


def lte_kpis(days: Iterable[Day], cells: np.ndarray) -> dict[str, float]:
    """LTE KPIs over a set of cells and days (ratio of sums, rule L3).

    Args:
        days: Simulated days (consumed once).
        cells: Global cell indices; non-LTE cells are ignored.

    Returns:
        KPI name to value; NaN where the denominator is zero.
    """
    return set_kpis(days, {"cells": cells})["cells"]


@dataclass(frozen=True)
class WhatIf:
    """Result of a what-if.

    Attributes:
        changes: The changes.
        cells: Changed and touched cells.
        before: KPIs of those cells over the replay week, before.
        after: The same, after.
    """

    changes: list[Change]
    cells: np.ndarray
    before: dict[str, float]
    after: dict[str, float]

    def as_record(self) -> dict[str, Any]:
        """JSON-ready form.

        Returns:
            The result.
        """
        return {
            "changes": [
                {"kind": c.kind, "cell": c.cell, "delta": c.delta, "target": c.target}
                for c in self.changes
            ],
            "cells": [int(c) for c in self.cells],
            "before": {k: round(v, 4) for k, v in self.before.items()},
            "after": {k: round(v, 4) for k, v in self.after.items()},
        }


def what_if(model: NetworkModel, changes: list[Change], first_day: int) -> WhatIf:
    """Replay the next week before and after bounded changes (rule M6).

    Args:
        model: The network now (with any faults present).
        changes: Bounded changes.
        first_day: First day of the replay week.

    Returns:
        KPIs of the changed and touched cells, before and after.
    """
    after = apply_changes(model, changes)
    changed = {c.cell for c in changes} | {c.target for c in changes if c.target >= 0}
    cells = touched_cells(model, after, changed)
    columns = sorted(model.neighbours | after.neighbours)
    return WhatIf(
        changes=changes,
        cells=cells,
        before=lte_kpis(replay(model, first_day, columns), cells),
        after=lte_kpis(replay(after, first_day, columns), cells),
    )
