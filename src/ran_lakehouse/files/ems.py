"""A simulated EMS per vendor region (rules P1, P2, P4, P5, W5): one PM file
per granularity period carrying every network element of the region.

The Huawei-style EMS writes 3GPP PM XML (TS 32.435), type B, with local
time and its +0700 offset; the Nokia-style EMS writes the OMeS-shaped
format with UTC timestamps (rule W5).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from ran_lakehouse.files import omes
from ran_lakehouse.files.dialects import SAMPLES_PER_PERIOD, Dialect, Entry, MeasGroup
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
        vendor_name: fileHeader vendorName (3GPP XML only).
        file_format: "3gpp-xml" (TS 32.435) or "omes" (Nokia-style).
    """

    ems_id: str
    dialect: Dialect
    tz: timezone
    vendor_name: str
    file_format: str


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
    if entry.rule == "availability_samples":
        unavailable = v[entry.canonical[0]][:, columns]
        samples: np.ndarray = np.rint(SAMPLES_PER_PERIOD * (1.0 - unavailable / 900.0))
        return samples
    if entry.rule == "samples_total":
        periods = next(iter(v.values())).shape[0]
        return np.full((periods, columns.size), float(SAMPLES_PER_PERIOD))
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


def object_name(
    model: NetworkModel, ems: Ems, counters: DayCounters, group: MeasGroup, key: int, element: str
) -> str:
    """How the EMS's format names a measured object.

    Args:
        model: The network.
        ems: The EMS.
        counters: The day's counters of the object's technology.
        group: The measInfo or measurement type.
        key: Cell index, or relation position in counters.relations.
        element: Managed element of the object.

    Returns:
        measObjLdn relative to the managed element (3GPP XML) or the full
        DN (OMeS).
    """
    cells = model.world.cells
    if group.objects == "cell":
        if ems.file_format == "omes":
            return nokia_dn(model, key)
        return relative_ldn(cells[key].dn, element)
    source, target = counters.relations[key]
    if ems.file_format == "omes":
        return nokia_relation_dn(model, source, target)
    relation = "EUtranRelation" if group.technology == "LTE" else "GsmRelation"
    return f"{relative_ldn(cells[source].dn, element)},{relation}={cells[target].cell_name}"


def nokia_dn(model: NetworkModel, cell: int) -> str:
    """Nokia-style DN of a cell (numbering ASSUMPTION, rule P4).

    Args:
        model: The network.
        cell: Global cell index.

    Returns:
        PLMN-PLMN/MRBTS-s/LNBTS-s/LNCEL-k for LTE (k counts the site's LTE
        cells from 1) or PLMN-PLMN/BSC-b/BCF-s/BTS-(10 s + sector) for GSM.
    """
    world_cell = model.world.cells[cell]
    site = world_cell.site_id
    if world_cell.technology == "LTE":
        return f"PLMN-PLMN/MRBTS-{site}/LNBTS-{site}/LNCEL-{nokia_local_cell(model, cell)}"
    bsc = int(world_cell.managed_element.removeprefix("BSC"))
    return f"PLMN-PLMN/BSC-{bsc}/BCF-{site}/BTS-{10 * site + world_cell.sector}"


def nokia_local_cell(model: NetworkModel, cell: int) -> int:
    """A cell's LTE cell number within its site, from 1 in world order.

    Args:
        model: The network.
        cell: Global cell index of an LTE cell.

    Returns:
        The number.
    """
    site = model.world.cells[cell].site_id
    same = [
        i
        for i, c in enumerate(model.world.cells)
        if c.site_id == site and c.technology == "LTE" and i <= cell
    ]
    return len(same)


def nokia_relation_dn(model: NetworkModel, source: int, target: int) -> str:
    """Nokia-style DN of a neighbour relation, named by its target (ASSUMPTION).

    Args:
        model: The network.
        source: Global index of the source cell.
        target: Global index of the target cell.

    Returns:
        The source DN plus LNREL-(100 t_site + t_cell) for LTE or
        ADJS-(10 t_site + t_sector) for GSM, stable when other relations
        change.
    """
    t = model.world.cells[target]
    if t.technology == "LTE":
        return (
            f"{nokia_dn(model, source)}/LNREL-{100 * t.site_id + nokia_local_cell(model, target)}"
        )
    return f"{nokia_dn(model, source)}/ADJS-{10 * t.site_id + t.sector}"


def day_files(model: NetworkModel, day: Day, ems: Ems) -> Iterator[tuple[str, bytes]]:
    """The EMS's PM files for one simulated day.

    Args:
        model: The network the day was simulated on.
        day: The day's counters.
        ems: The EMS.

    Yields:
        (file name, gzip bytes) per granularity period.

    Raises:
        ValueError: For an unknown file format.
    """
    state = model.state
    world_cells = model.world.cells
    region = f"{ROOT},SubNetwork={REGION[ems.dialect.vendor]}"
    elements: dict[str, list[int]] = {}
    for c in np.flatnonzero(state.vendor == ems.dialect.vendor):
        elements.setdefault(world_cells[c].managed_element, []).append(int(c))
    prepared = []
    for element in sorted(elements):
        cells = np.array(elements[element])
        technology = str(state.technology[cells[0]])
        counters = day.lte if technology == "LTE" else day.gsm
        infos = []
        for group in ems.dialect.groups:
            if group.technology != technology:
                continue
            keys, values = group_values(group, counters, cells, model)
            names = [object_name(model, ems, counters, group, k, element) for k in keys]
            infos.append((group, names, values))
        prepared.append((element, infos))
    for p, start in enumerate(day.starts):
        begin = start.replace(tzinfo=WIB).astimezone(ems.tz)
        end = begin + PERIOD
        if ems.file_format == "3gpp-xml":
            data = [
                ElementData(
                    f"ManagedElement={element}",
                    ems.dialect.release,
                    [meas_info(group, names, values[p]) for group, names, values in infos],
                )
                for element, infos in prepared
            ]
            name = file_name("B", begin, end, ems.ems_id)
            yield name, write_file(region, ems.ems_id, ems.vendor_name, begin, end, data)
        elif ems.file_format == "omes":
            targets = [
                omes.Target(
                    measurement_type=group.meas_info_id,
                    counters=[e.name for e in group.entries],
                    dns=[n for n, ok in zip(names, reported(values[p]), strict=True) if ok],
                    values=values[p][reported(values[p])],
                )
                for _, infos in prepared
                for group, names, values in infos
            ]
            yield omes.file_name(ems.ems_id, begin), omes.write_file(begin, 15, targets)
        else:
            raise ValueError(f"unknown PM file format {ems.file_format}")


def reported(rows: np.ndarray) -> np.ndarray:
    """Objects with at least one value (relations not configured are not).

    Args:
        rows: Values, shape (objects, counters).

    Returns:
        Mask per object.
    """
    mask: np.ndarray = ~np.isnan(rows).all(axis=1)
    return mask


def meas_info(group: MeasGroup, names: list[str], rows: np.ndarray) -> MeasInfo:
    """A 3GPP measInfo for one period.

    Args:
        group: The measInfo definition.
        names: measObjLdn per object.
        rows: Values, shape (objects, counters).

    Returns:
        The measInfo with only the reported objects.
    """
    ok = reported(rows)
    return MeasInfo(
        meas_info_id=group.meas_info_id,
        counters=[e.name for e in group.entries],
        objects=[n for n, keep in zip(names, ok, strict=True) if keep],
        values=rows[ok],
        suspect=np.zeros(int(ok.sum()), dtype=bool),
    )


def local_start(day: Day) -> datetime:
    """The first period start of a day in WIB.

    Args:
        day: The day.

    Returns:
        Aware local time.
    """
    return day.starts[0].replace(tzinfo=WIB)
