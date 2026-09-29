"""Configuration and alarm exports of the simulated EMS (rules C1, C2, F2).

CM (rule C1): a daily snapshot of every cell and neighbour relation of the
EMS's region, and a change log of every configuration change (the planted
F1a-F1c changes, their restoration by operations, and cell individual
offset changes). Object classes use the solution-set spellings (TS 28.659
for E-UTRAN, TS 28.656 for GERAN; rule C1 line), DNs per TS 32.300, in the
EMS's naming style (3GPP DNs for the Huawei-style EMS, Nokia-style DNs for
the Nokia-style one); every object carries its cell name as userLabel so
the two can be joined. Attribute names are this model's own (ASSUMPTION):
the NRMs place tilt and power outside the cell classes.

FM (rule C2): the alarm log as TS 32.111-2 V19.0.0 notifications,
notifyNewAlarm at alarmRaisedTime and notifyClearedAlarm (perceivedSeverity
Cleared) at alarmClearedTime, with the clause 5.5.1 attribute names, for
LTE and GSM alike.

Timestamps follow the EMS (rule W5): Huawei-style local +0700, Nokia-style
UTC. Files are gzip JSON lines named <KIND>_<EMS>_<YYYYMMDD>.jsonl.gz
(ASSUMPTION: a project format; vendor CM and FM exports vary and 3GPP Bulk
CM XML is not modelled).
"""

import gzip
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from ran_lakehouse.faults.plant import MISTAKEN_TILT_DEG, POWER_DROP_DB, Fault, uplink_rise_db
from ran_lakehouse.faults.traces import fm_alarms
from ran_lakehouse.files.ems import WIB, Ems, nokia_dn, nokia_relation_dn
from ran_lakehouse.model import NetworkModel

KINDS = ("CM", "CMLOG", "FM")


def object_class(model: NetworkModel, cell: int) -> str:
    """Solution-set class of a cell (rule C1).

    Args:
        model: The network.
        cell: Global cell index.

    Returns:
        EUtranCellFDD, EUtranCellTDD or GsmCell.
    """
    return model.world.cells[cell].object_class


def relation_class(model: NetworkModel, source: int) -> str:
    """Solution-set class of a neighbour relation of a cell.

    Args:
        model: The network.
        source: Global index of the source cell.

    Returns:
        EUtranRelation or GsmRelation.
    """
    return "EUtranRelation" if model.world.cells[source].technology == "LTE" else "GsmRelation"


def cell_dn(model: NetworkModel, ems: Ems, cell: int) -> str:
    """A cell's DN in the EMS's naming style.

    Args:
        model: The network.
        ems: The EMS.
        cell: Global cell index.

    Returns:
        The DN.
    """
    return nokia_dn(model, cell) if ems.file_format == "omes" else model.world.cells[cell].dn


def relation_dn(model: NetworkModel, ems: Ems, source: int, target: int) -> str:
    """A neighbour relation's DN in the EMS's naming style.

    Args:
        model: The network.
        ems: The EMS.
        source: Source cell.
        target: Target cell.

    Returns:
        The DN.
    """
    if ems.file_format == "omes":
        return nokia_relation_dn(model, source, target)
    name = model.world.cells[target].cell_name
    return f"{model.world.cells[source].dn},{relation_class(model, source)}={name}"


def stamp(ems: Ems, local: datetime) -> str:
    """A local (WIB) time in the EMS's time zone.

    Args:
        ems: The EMS.
        local: Naive WIB time.

    Returns:
        ISO 8601 with offset.
    """
    return local.replace(tzinfo=WIB).astimezone(ems.tz).isoformat()


def configuration_at(
    base: NetworkModel, faults: list[Fault], t: datetime
) -> tuple[np.ndarray, np.ndarray, np.ndarray, set[tuple[int, int]]]:
    """Configured tilt, power, offset and neighbours at a time.

    Only configuration faults change configuration (F1a-F1c); coverage is
    not needed for a snapshot.

    Args:
        base: The network as built.
        faults: The fault schedule.
        t: Naive WIB time.

    Returns:
        Tilt, power and offset per cell, and the configured relations.
    """
    tilt = base.state.tilt_deg.copy()
    power = base.state.power_dbm.copy()
    cio = base.state.cio_db.copy()
    neighbours = set(base.neighbours)
    for f in faults:
        if not f.active_at(t):
            continue
        if f.kind == "F1a":
            tilt[f.cell] = MISTAKEN_TILT_DEG
        elif f.kind == "F1b":
            neighbours.discard((f.cell, f.target))
        elif f.kind == "F1c":
            power[f.cell] += POWER_DROP_DB
    return tilt, power, cio, neighbours


def snapshot(base: NetworkModel, faults: list[Fault], ems: Ems, day: date) -> list[dict[str, Any]]:
    """The CM snapshot of the EMS's region at 00:00 local of a day.

    Args:
        base: The network as built.
        faults: The fault schedule.
        ems: The EMS.
        day: The day.

    Returns:
        One record per cell and per configured neighbour relation.
    """
    at = datetime(day.year, day.month, day.day)
    tilt, power, cio, neighbours = configuration_at(base, faults, at)
    state = base.state
    mine = state.vendor == ems.dialect.vendor
    records = []
    for c in np.flatnonzero(mine):
        cell = base.world.cells[c]
        attributes: dict[str, Any] = {
            "userLabel": cell.cell_name,
            "band": cell.band,
            "azimuthDeg": cell.azimuth_deg,
            "antennaHeightM": float(state.height_m[c]),
            "electricalTiltDeg": float(tilt[c]),
            "txPowerDbm": round(float(power[c]), 1),
            "cellIndividualOffsetDb": float(cio[c]),
        }
        if cell.technology == "LTE":
            attributes["bandwidthMhz"] = float(state.bandwidth_mhz[c])
        else:
            attributes["trx"] = int(state.trx[c])
        records.append(
            {
                "snapshotTime": stamp(ems, at),
                "objectClass": object_class(base, int(c)),
                "dn": cell_dn(base, ems, int(c)),
                "attributes": attributes,
            }
        )
    names = [c.cell_name for c in base.world.cells]
    for s, t in sorted(neighbours):
        if not mine[s]:
            continue
        records.append(
            {
                "snapshotTime": stamp(ems, at),
                "objectClass": relation_class(base, s),
                "dn": relation_dn(base, ems, s, t),
                "attributes": {
                    "userLabel": f"{names[s]}->{names[t]}",
                    "adjacentCell": names[t],
                },
            }
        )
    return records


def change_log(
    base: NetworkModel, faults: list[Fault], ems: Ems, day: date
) -> list[dict[str, Any]]:
    """CM change log entries of the EMS's region made on a day.

    Args:
        base: The network as built.
        faults: The fault schedule.
        ems: The EMS.
        day: The day.

    Returns:
        Entries in time order.
    """
    out = []
    for f in faults:
        if f.kind not in ("F1a", "F1b", "F1c") or base.state.vendor[f.cell] != ems.dialect.vendor:
            continue
        for when, restore in ((f.start, False), (f.end, True)):
            if when.date() != day:
                continue
            out.append(change_entry(base, ems, f, when, restore))
    return sorted(out, key=lambda e: (e["time"], e["dn"]))


def change_entry(
    base: NetworkModel, ems: Ems, fault: Fault, when: datetime, restore: bool
) -> dict[str, Any]:
    """One change log entry: a configuration fault made or restored.

    Args:
        base: The network as built.
        ems: The EMS.
        fault: An F1a, F1b or F1c fault.
        when: Naive WIB time of the change.
        restore: True for the restoration by operations.

    Returns:
        The entry.
    """
    c = fault.cell
    if fault.kind == "F1b":
        before, after = ("present", "deleted")
        dn = relation_dn(base, ems, c, fault.target)
        attribute, klass = "relation", relation_class(base, c)
    else:
        if fault.kind == "F1a":
            attribute = "electricalTiltDeg"
            before, after = f"{base.state.tilt_deg[c]:g}", f"{MISTAKEN_TILT_DEG:g}"
        else:
            attribute = "txPowerDbm"
            p = float(base.state.power_dbm[c])
            before, after = f"{p:.1f}", f"{p + POWER_DROP_DB:.1f}"
        dn, klass = cell_dn(base, ems, c), object_class(base, c)
    if restore:
        before, after = after, before
    return {
        "time": stamp(ems, when),
        "objectClass": klass,
        "dn": dn,
        "attribute": attribute,
        "oldValue": before,
        "newValue": after,
    }


def alarm_notifications(
    base: NetworkModel, faults: list[Fault], ems: Ems, day: date
) -> list[dict[str, Any]]:
    """TS 32.111-2 notifications of the EMS's region emitted on a day.

    Args:
        base: The network as built.
        faults: The fault schedule.
        ems: The EMS.
        day: The day.

    Returns:
        notifyNewAlarm and notifyClearedAlarm records in time order.
    """
    mine = [f for f in faults if base.state.vendor[f.cell] == ems.dialect.vendor]
    # fm_alarms raises interference alarms only in the vendor style that has
    # them and only above the threshold rise (faults.traces).
    rises = {f.fault_id: float(uplink_rise_db(base, f)[f.cell]) for f in mine if f.kind == "F1e"}
    by_id = {f"A{f.fault_id[1:]}": f for f in mine}
    out = []
    for alarm in fm_alarms(base, mine, rises):
        fault = by_id[alarm.alarm_id]
        common = {
            "alarmId": alarm.alarm_id,
            "objectClass": object_class(base, fault.cell),
            "objectInstance": cell_dn(base, ems, fault.cell),
            "alarmType": alarm.event_type,
            "probableCause": alarm.probable_cause,
            "specificProblem": alarm.specific_problem,
            "alarmRaisedTime": stamp(ems, alarm.alarm_raised_time),
        }
        if alarm.alarm_raised_time.date() == day:
            out.append(
                {
                    **common,
                    "notificationType": "notifyNewAlarm",
                    "notificationId": f"{alarm.alarm_id}-1",
                    "eventTime": common["alarmRaisedTime"],
                    "perceivedSeverity": alarm.perceived_severity,
                }
            )
        if alarm.alarm_cleared_time.date() == day:
            cleared = stamp(ems, alarm.alarm_cleared_time)
            out.append(
                {
                    **common,
                    "notificationType": "notifyClearedAlarm",
                    "notificationId": f"{alarm.alarm_id}-2",
                    "eventTime": cleared,
                    "alarmClearedTime": cleared,
                    "perceivedSeverity": "Cleared",
                }
            )
    return sorted(out, key=lambda n: (n["eventTime"], n["notificationId"]))


def jsonl(records: list[dict[str, Any]]) -> bytes:
    """Records as gzip JSON lines.

    Args:
        records: The records.

    Returns:
        Compressed bytes.
    """
    text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    return gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)


def day_exports(
    base: NetworkModel, faults: list[Fault], ems: Ems, day: date
) -> Iterator[tuple[str, bytes, int]]:
    """The EMS's CM snapshot, CM change log and FM log files for a day.

    Args:
        base: The network as built.
        faults: The fault schedule.
        ems: The EMS.
        day: The day.

    Yields:
        (file name, gzip bytes, record count) for CM, CMLOG and FM.
    """
    parts = {
        "CM": snapshot(base, faults, ems, day),
        "CMLOG": change_log(base, faults, ems, day),
        "FM": alarm_notifications(base, faults, ems, day),
    }
    for kind in KINDS:
        records = parts[kind]
        yield f"{kind}_{ems.ems_id}_{day:%Y%m%d}.jsonl.gz", jsonl(records), len(records)


def run_days(first: date, n_days: int) -> list[date]:
    """Consecutive dates.

    Args:
        first: First date.
        n_days: Number of days.

    Returns:
        The dates.
    """
    return [first + timedelta(days=i) for i in range(n_days)]
