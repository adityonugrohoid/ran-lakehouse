"""What-if as the API serves it (rule M6).

The network replayed is the network as it stands at the start of the
replay week: the built network with the faults of the run present then.
Only KPIs leave this module, never the faults (rule A3). Changes pass
through faults.whatif.apply_changes, which holds the M6 bounds and
recomputes coverage, serving and interference coupling for every point a
change reaches; the replay uses the run's common random numbers, so before
and after differ only by the change.
"""

from datetime import date, timedelta

import numpy as np

from ran_lakehouse.faults.plant import apply_faults, plan_faults
from ran_lakehouse.faults.whatif import Change, apply_changes, replay, set_kpis, touched_cells
from ran_lakehouse.model import RUN_START, NetworkModel, day_starts

TOTAL = "touched cells"


class WhatIfEngine:
    """Replays the week after gold ends, before and after bounded changes."""

    def __init__(self, model: NetworkModel, first_day: int) -> None:
        """Hold the network of the replay week.

        Args:
            model: The network as it stands at the start of the week.
            first_day: Day index (from RUN_START) of the week's first day.
        """
        self.model = model
        self.first_day = first_day
        self.index = {name: i for i, name in enumerate(model.state.cell_names)}

    @classmethod
    def for_run(cls, base: NetworkModel, run_weeks: int, first_day: int) -> "WhatIfEngine":
        """The engine for a run's network, with its faults present at the week start.

        Args:
            base: The network as built.
            run_weeks: Weeks of the run (its fault plan's length).
            first_day: Day index of the replay week's first day.

        Returns:
            The engine.

        Raises:
            ValueError: If the week does not fit in the run.
        """
        if first_day < 0 or first_day + 7 > 7 * run_weeks:
            raise ValueError(f"replay days {first_day}..{first_day + 6} are outside the run")
        start = day_starts(first_day)[0]
        present = [f for f in plan_faults(base, run_weeks) if f.active_at(start)]
        return cls(apply_faults(base, present) if present else base, first_day)

    @property
    def week_start(self) -> date:
        """First WIB day of the replay week."""
        return (RUN_START + timedelta(days=self.first_day)).date()

    def cell(self, name: str) -> int:
        """A cell's index, for an LTE cell only.

        Args:
            name: Cell name.

        Returns:
            Global cell index.

        Raises:
            ValueError: For an unknown or non-LTE cell.
        """
        if name not in self.index:
            raise ValueError(f"unknown cell {name}")
        i = self.index[name]
        if self.model.state.technology[i] != "LTE":
            raise ValueError(f"{name} is a GSM cell; what-if replays LTE KPIs only")
        return i

    def changes(self, requested: list[tuple[str, str, float, str | None]]) -> list[Change]:
        """Requested changes as model changes.

        Args:
            requested: (kind, cell, delta, target) per change.

        Returns:
            The changes.

        Raises:
            ValueError: For a neighbour change without a target, or a target
                on any other change.
        """
        out = []
        for kind, cell, delta, target in requested:
            neighbour = kind in ("add_neighbour", "remove_neighbour")
            if neighbour and target is None:
                raise ValueError(f"{kind} needs a target cell")
            if not neighbour and target is not None:
                raise ValueError(f"{kind} takes no target cell")
            if neighbour and delta != 0:
                raise ValueError(f"{kind} takes delta 0")
            out.append(
                Change(kind, self.cell(cell), float(delta), self.cell(target) if target else -1)
            )
        return out

    def run(self, changes: list[Change]) -> tuple[list[dict[str, object]], dict[str, object]]:
        """Replay the week before and after.

        Args:
            changes: Bounded changes (apply_changes refuses any beyond M6).

        Returns:
            (per cell: name, changed, before, after; the same over all
            touched cells).
        """
        after = apply_changes(self.model, changes)
        changed = {c.cell for c in changes} | {c.target for c in changes if c.target >= 0}
        cells = touched_cells(self.model, after, changed)
        names = self.model.state.cell_names
        sets = {names[i]: np.array([i]) for i in cells} | {TOTAL: cells}
        columns = sorted(self.model.neighbours | after.neighbours)
        before_kpis = set_kpis(replay(self.model, self.first_day, columns), sets)
        after_kpis = set_kpis(replay(after, self.first_day, columns), sets)

        def entry(name: str, is_changed: bool) -> dict[str, object]:
            return {
                "cell_name": name,
                "changed": is_changed,
                "before": finite(before_kpis[name]),
                "after": finite(after_kpis[name]),
            }

        per_cell = [entry(names[i], int(i) in changed) for i in cells]
        return per_cell, entry(TOTAL, False)


def finite(kpis: dict[str, float]) -> dict[str, float | None]:
    """KPIs with NaN (a zero denominator) as None, rounded for the wire.

    Args:
        kpis: KPI name to value.

    Returns:
        The same, JSON-safe.
    """
    return {k: None if np.isnan(v) else round(float(v), 4) for k, v in kpis.items()}
