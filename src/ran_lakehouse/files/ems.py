"""A simulated EMS per vendor region (rules P1, P2, P5, W5): one type B PM
file per granularity period carrying every network element of the region.

The Huawei-style EMS writes 3GPP PM XML (TS 32.435) with local time and its
+0700 offset (rule W5).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from ran_lakehouse.files.dialects import Dialect, Entry, MeasGroup
from ran_lakehouse.files.pm_xml import ElementData, MeasInfo, file_name, write_file
from ran_lakehouse.model import Day, NetworkModel
from ran_lakehouse.model.counters import DayCounters

PERIOD = timedelta(minutes=15)
WIB = timezone(timedelta(hours=7))  # rule W5
ROOT = "SubNetwork=RanLake"
REGION = {"huawei": "West", "nokia": "East"}


@dataclass(frozen=True)
class Ems:
    """A simulated EMS.

    Attributes:
        ems_id: EMS name, the UniqueId of its file names.
        dialect: The dialect it writes.
        tz: Time zone of its timestamps.
        vendor_name: fileHeader vendorName.
    """

    ems_id: str
    dialect: Dialect
    tz: timezone
    vendor_name: str


def relative_ldn(dn: str, element: str) -> str:
    """The part of a DN below its managed element.

    Args:
        dn: Full DN.
        element: Managed element name.

    Returns:
        LDN relative to "ManagedElement=<element>".

    Raises:
        ValueError: If the DN does not contain the managed element.
    """
    marker = f"ManagedElement={element},"
    if marker not in dn:
        raise ValueError(f"{dn} is not under ManagedElement={element}")
    return dn.split(marker, 1)[1]


def entry_values(
    entry: Entry, counters: DayCounters, columns: np.ndarray, model: NetworkModel
) -> np.ndarray:
    """Values of one cell-level vendor counter.

    Args:
        entry: The dictionary entry.
        counters: The day's counters of the technology.
        columns: Columns of the cells in counters.values arrays.
        model: The network (for N_RB and sites).

    Returns:
        Values, shape (periods, cells).

    Raises:
        ValueError: For an unknown rule.
    """
    v = counters.values
    if entry.rule == "copy":
        result: np.ndarray = v[entry.canonical[0]][:, columns] * entry.scale
        return result
    if entry.rule == "sum":
        total: np.ndarray = np.sum([v[c][:, columns] for c in entry.canonical], axis=0)
        return total
    if entry.rule == "difference":
        first = v[entry.canonical[0]][:, columns]
        rest = np.sum([v[c][:, columns] for c in entry.canonical[1:]], axis=0)
        difference: np.ndarray = np.maximum(first - rest, 0.0)
        return difference
    n_rb = model.state.n_rb[counters.cells[columns]][None, :].astype(float)
    if entry.rule == "prb_used":
        used: np.ndarray = np.round(v[entry.canonical[0]][:, columns] / 100.0 * n_rb, 1)
        return used
    if entry.rule == "prb_avail":
        periods = next(iter(v.values())).shape[0]
        return np.repeat(n_rb, periods, axis=0)
    if entry.rule in ("relation_same_site", "relation_other_site"):
        return relation_sum(entry, counters, columns, model)
    if entry.rule == "bin":
        return v[entry.canonical[0]][:, columns, entry.bin_index]
    raise ValueError(f"unknown dictionary rule {entry.rule}")


def relation_sum(
    entry: Entry, counters: DayCounters, columns: np.ndarray, model: NetworkModel
) -> np.ndarray:
    """A per-relation counter summed per source over same-site or other targets.

    Args:
        entry: The dictionary entry.
        counters: The day's counters.
        columns: Columns of the source cells.
        model: The network.

    Returns:
        Values, shape (periods, cells); relations not reported add nothing.
    """
    values = counters.relation_values[entry.canonical[0]]
    site = model.state.site_ids
    want_same = entry.rule == "relation_same_site"
    column_of = {int(c): i for i, c in enumerate(counters.cells[columns])}
    out = np.zeros((values.shape[0], columns.size))
    for r, (s, t) in enumerate(counters.relations):
        if s in column_of and (site[s] == site[t]) == want_same:
            out[:, column_of[s]] += np.nan_to_num(values[:, r])
    return out


def group_values(
    group: MeasGroup, counters: DayCounters, cells: np.ndarray, model: NetworkModel
) -> tuple[list[int], np.ndarray]:
    """Objects and values of one measInfo for some cells.

    Args:
        group: The measInfo definition.
        counters: The day's counters of the group's technology.
        cells: Global indices of the element's cells.
        model: The network.

    Returns:
        Object keys (cell index, or relation position in counters.relations)
        and values, shape (periods, objects, counters).
    """
    if group.objects == "cell":
        columns = np.flatnonzero(np.isin(counters.cells, cells))
        stacked = [entry_values(e, counters, columns, model) for e in group.entries]
        return [int(c) for c in counters.cells[columns]], np.stack(stacked, axis=2)
    wanted = set(int(c) for c in cells)
    rows = [r for r, (s, _) in enumerate(counters.relations) if s in wanted]
    stacked = [counters.relation_values[e.canonical[0]][:, rows] * e.scale for e in group.entries]
    return rows, np.stack(stacked, axis=2)


def day_files(model: NetworkModel, day: Day, ems: Ems) -> Iterator[tuple[str, bytes]]:
    """The EMS's PM files for one simulated day.

    Args:
        model: The network the day was simulated on.
        day: The day's counters.
        ems: The EMS.

    Yields:
        (file name, gzip bytes) per granularity period.
    """
    state = model.state
    world_cells = model.world.cells
    region = f"{ROOT},SubNetwork={REGION[ems.dialect.vendor]}"
    mine = np.flatnonzero(state.vendor == ems.dialect.vendor)
    elements: dict[str, np.ndarray] = {}
    for c in mine:
        elements.setdefault(world_cells[c].managed_element, np.array([], dtype=int))
        elements[world_cells[c].managed_element] = np.append(
            elements[world_cells[c].managed_element], c
        )
    prepared = []
    for element in sorted(elements):
        cells = elements[element]
        technology = str(state.technology[cells[0]])
        counters = day.lte if technology == "LTE" else day.gsm
        infos = []
        for group in ems.dialect.groups:
            if group.technology != technology:
                continue
            keys, values = group_values(group, counters, cells, model)
            if group.objects == "cell":
                ldns = [relative_ldn(world_cells[k].dn, element) for k in keys]
            else:
                ldns = [
                    relative_ldn(world_cells[counters.relations[k][0]].dn, element)
                    + f",{'EUtranRelation' if technology == 'LTE' else 'GsmRelation'}="
                    + world_cells[counters.relations[k][1]].cell_name
                    for k in keys
                ]
            infos.append((group, ldns, values))
        prepared.append((element, infos))
    for p, start in enumerate(day.starts):
        begin = start.replace(tzinfo=WIB).astimezone(ems.tz)
        end = begin + PERIOD
        data = []
        for element, infos in prepared:
            meas_infos = []
            for group, ldns, values in infos:
                rows = values[p]
                reported = ~np.isnan(rows).all(axis=1)
                meas_infos.append(
                    MeasInfo(
                        meas_info_id=group.meas_info_id,
                        counters=[e.name for e in group.entries],
                        objects=[ldn for ldn, ok in zip(ldns, reported, strict=True) if ok],
                        values=rows[reported],
                        suspect=np.zeros(int(reported.sum()), dtype=bool),
                    )
                )
            data.append(ElementData(f"ManagedElement={element}", ems.dialect.release, meas_infos))
        name = file_name("B", begin, end, ems.ems_id)
        yield name, write_file(region, ems.ems_id, ems.vendor_name, begin, end, data)


def local_start(day: Day) -> datetime:
    """The first period start of a day in WIB.

    Args:
        day: The day.

    Returns:
        Aware local time.
    """
    return day.starts[0].replace(tzinfo=WIB)
