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
HOUR = timedelta(hours=1)
HOUR_PERIODS = 4  # 15-minute periods per hour
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


@dataclass(frozen=True)
class Adjustment:
    """A change to one network element's values in one period's file.

    Attributes:
        factor: Multiplier on every value of the element (counts rounded).
        suspect: Flag the element's values as suspect (TS 32.432 suspectFlag;
            the OMeS-shaped format has no flag).
    """

    factor: float
    suspect: bool


@dataclass(frozen=True)
class PreparedDay:
    """One day's values arranged per network element, ready to render.

    Attributes:
        ems: The EMS.
        starts: Local (WIB) period starts.
        elements: (managed element, [(group, object names, values)]) with
            values of shape (periods, objects, counters).
    """

    ems: Ems
    starts: list[datetime]
    elements: list[tuple[str, list[tuple[MeasGroup, list[str], np.ndarray]]]]


def prepare_day(model: NetworkModel, day: Day, ems: Ems) -> PreparedDay:
    """Arrange one day's counters for the EMS's region.

    Args:
        model: The network the day was simulated on.
        day: The day's counters.
        ems: The EMS.

    Returns:
        The prepared day.
    """
    state = model.state
    world_cells = model.world.cells
    by_element: dict[str, list[int]] = {}
    for c in np.flatnonzero(state.vendor == ems.dialect.vendor):
        by_element.setdefault(world_cells[c].managed_element, []).append(int(c))
    elements = []
    for element in sorted(by_element):
        cells = np.array(by_element[element])
        technology = str(state.technology[cells[0]])
        counters = day.lte if technology == "LTE" else day.gsm
        infos = []
        for group in ems.dialect.groups:
            if group.technology != technology:
                continue
            keys, values = group_values(group, counters, cells, model)
            names = [object_name(model, ems, counters, group, k, element) for k in keys]
            infos.append((group, names, values))
        elements.append((element, infos))
    return PreparedDay(ems, day.starts, elements)


def release_names(dialect: Dialect, group: MeasGroup) -> list[str]:
    """Counter names of a group in one release of the dialect.

    Args:
        dialect: The release.
        group: The group, from the EMS's base release.

    Returns:
        The names in the release (releases keep the group and counter order).

    Raises:
        ValueError: If the release has no such group.
    """
    for g in dialect.groups:
        if g.meas_info_id == group.meas_info_id:
            return [e.name for e in g.entries]
    raise ValueError(f"release {dialect.release} has no measInfo {group.meas_info_id}")


def render_period(
    prepared: PreparedDay,
    p: int,
    releases: dict[str, Dialect],
    adjustments: dict[str, Adjustment],
) -> tuple[str, bytes]:
    """One period's file.

    Args:
        prepared: The prepared day.
        p: Period index in the day.
        releases: Dictionary release per managed element in this period.
        adjustments: Adjustments per managed element in this period.

    Returns:
        (file name, gzip bytes).

    Raises:
        ValueError: For an unknown file format.
    """
    ems = prepared.ems
    begin = prepared.starts[p].replace(tzinfo=WIB).astimezone(ems.tz)
    end = begin + PERIOD
    closes_hour = p % HOUR_PERIODS == HOUR_PERIODS - 1

    def rows(element: str, group: MeasGroup, values: np.ndarray) -> tuple[np.ndarray, bool]:
        period_values = values[p] if group.granularity_min == 15 else hour_values(values, p)
        adjust = adjustments.get(element)
        if adjust is None:
            return period_values, False
        return np.rint(period_values * adjust.factor), adjust.suspect

    def included(group: MeasGroup) -> bool:
        return group.granularity_min == 15 or closes_hour

    if ems.file_format == "3gpp-xml":
        region = f"{ROOT},SubNetwork={REGION[ems.dialect.vendor]}"
        data = []
        for element, infos in prepared.elements:
            dialect = releases[element]
            meas_infos = []
            for group, names, values in infos:
                if not included(group):
                    continue
                period_rows, suspect = rows(element, group, values)
                info = meas_info(group, names, period_rows)
                meas_infos.append(
                    MeasInfo(
                        info.meas_info_id,
                        release_names(dialect, group),
                        info.objects,
                        info.values,
                        np.full(len(info.objects), suspect),
                        info.duration_s,
                    )
                )
            data.append(ElementData(f"ManagedElement={element}", dialect.release, meas_infos))
        name = file_name("B", begin, end, ems.ems_id)
        return name, write_file(region, ems.ems_id, ems.vendor_name, begin, end, data)
    if ems.file_format == "omes":
        targets: dict[int, list[omes.Target]] = {15: [], 60: []}
        for element, infos in prepared.elements:
            dialect = releases[element]
            for group, names, values in infos:
                if not included(group):
                    continue
                period_rows, _ = rows(element, group, values)
                ok = reported(period_rows)
                targets[group.granularity_min].append(
                    omes.Target(
                        measurement_type=group.meas_info_id,
                        counters=release_names(dialect, group),
                        dns=[n for n, keep in zip(names, ok, strict=True) if keep],
                        values=period_rows[ok],
                    )
                )
        setups = [omes.Setup(begin, 15, targets[15])]
        if closes_hour:
            setups.append(omes.Setup(end - HOUR, 60, targets[60]))
        return omes.file_name(ems.ems_id, begin), omes.write_file(setups)
    raise ValueError(f"unknown PM file format {ems.file_format}")


def day_files(model: NetworkModel, day: Day, ems: Ems) -> Iterator[tuple[str, bytes]]:
    """The EMS's PM files for one simulated day, every element on the base release.

    Args:
        model: The network the day was simulated on.
        day: The day's counters.
        ems: The EMS.

    Yields:
        (file name, gzip bytes) per granularity period.
    """
    prepared = prepare_day(model, day, ems)
    releases = {element: ems.dialect for element, _ in prepared.elements}
    for p in range(len(day.starts)):
        yield render_period(prepared, p, releases, {})


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
        duration_s=60 * group.granularity_min,
    )


def hour_values(values: np.ndarray, p: int) -> np.ndarray:
    """A 60-minute measurement over the hour ending with period p.

    Args:
        values: Values of the day, shape (periods, objects, counters); the
            hourly groups are distribution bins, so the hour is their sum.
        p: Last period of the hour (p % 4 == 3).

    Returns:
        Values, shape (objects, counters); NaN where the object reported in
        none of the four periods.
    """
    window = values[p - HOUR_PERIODS + 1 : p + 1]
    summed: np.ndarray = np.nansum(window, axis=0)
    summed[np.isnan(window).all(axis=0)] = np.nan
    return summed


def local_start(day: Day) -> datetime:
    """The first period start of a day in WIB.

    Args:
        day: The day.

    Returns:
        Aware local time.
    """
    return day.starts[0].replace(tzinfo=WIB)
