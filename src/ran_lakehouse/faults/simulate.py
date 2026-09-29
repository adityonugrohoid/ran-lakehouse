"""Counters of a network whose faults come and go (rules F1-F3).

Each period runs on the network with the faults present at its start; a
day with several fault sets is computed once per set and spliced by
period. Noise is common to every set (rule M6), so splicing never mixes
random draws.
"""

from collections.abc import Iterator

import numpy as np

from ran_lakehouse.faults.plant import Fault, apply_faults
from ran_lakehouse.model import Day, NetworkModel, day_counters, day_starts, plan_relations
from ran_lakehouse.model.counters import DayCounters, RelationPlan


class ScenarioCache:
    """Networks and relation plans per active fault set, built once each."""

    def __init__(self, base: NetworkModel, columns: list[tuple[int, int]]) -> None:
        """Start with the fault-free network.

        Args:
            base: The network as built.
            columns: Per-relation counter columns.
        """
        self.base = base
        self.columns = columns
        self.cache: dict[frozenset[str], tuple[NetworkModel, RelationPlan]] = {}

    def get(self, faults: list[Fault]) -> tuple[NetworkModel, RelationPlan]:
        """The network with a set of faults present, and its relation plan.

        Args:
            faults: Faults present.

        Returns:
            The network and its plan.
        """
        key = frozenset(f.fault_id for f in faults)
        if key not in self.cache:
            model = apply_faults(self.base, faults) if faults else self.base
            self.cache[key] = (model, plan_relations(model, self.columns))
        return self.cache[key]


def splice(parts: list[tuple[np.ndarray, DayCounters]]) -> DayCounters:
    """Join counters of several fault sets by period.

    Args:
        parts: (period mask, counters) pairs covering every period once.

    Returns:
        The joined counters.
    """
    first = parts[0][1]
    values = {k: v.copy() for k, v in first.values.items()}
    relation_values = {k: v.copy() for k, v in first.relation_values.items()}
    for mask, counters in parts[1:]:
        for name, array in counters.values.items():
            values[name][mask] = array[mask]
        for name, array in counters.relation_values.items():
            relation_values[name][mask] = array[mask]
    return DayCounters(first.cells, values, first.relations, relation_values)


def simulate_with_faults(
    base: NetworkModel, faults: list[Fault], first_day: int, n_days: int
) -> Iterator[Day]:
    """Counters for consecutive days with the planted faults.

    Args:
        base: The network as built.
        faults: The fault schedule.
        first_day: Index of the first day.
        n_days: Number of days.

    Yields:
        One Day at a time.
    """
    cache = ScenarioCache(base, sorted(base.neighbours))
    for day in range(first_day, first_day + n_days):
        starts = day_starts(day)
        sets = [tuple(f for f in faults if f.active_at(t)) for t in starts]
        keys = sorted(set(sets), key=lambda s: [f.fault_id for f in s])
        days: list[tuple[np.ndarray, Day]] = []
        for key in keys:
            mask = np.array([s == key for s in sets])
            model, plan = cache.get(list(key))
            days.append((mask, day_counters(model, day, plan, starts)))
        yield Day(
            index=day,
            starts=starts,
            lte=splice([(m, d.lte) for m, d in days]),
            gsm=splice([(m, d.gsm) for m, d in days]),
        )
