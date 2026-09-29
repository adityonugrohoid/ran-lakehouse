"""CM and FM traces of planted faults (rule F2).

CM: the change log records each configuration fault (F1a-F1c) when it is
made and again when operations restores it. FM: an alarm log with the
TS 32.111-2 V19.0.0 alarm fields (alarmRaisedTime, alarmClearedTime,
eventType, probableCause, perceivedSeverity, specificProblem) for outages
(F1f) and, in the Huawei-style dialect only, for uplink interference
(F1e). The file formats and NRM attribute names come with rule C.
"""

from dataclasses import dataclass
from datetime import datetime

from ran_lakehouse.faults.plant import MISTAKEN_TILT_DEG, POWER_DROP_DB, Fault
from ran_lakehouse.model import NetworkModel

# Outage alarm: probable cause "Transmitter Failure", TS 32.111-2 V19.0.0
# Annex B Table B.2, paired there with the Equipment event type (Annex A
# Table A.1 "Equipment Alarm"); severity Critical, X.733 clause 8.1.2.3
# (object totally out of service).
OUTAGE_ALARM = ("Equipment Alarm", "Transmitter Failure", "Critical", "cell out of service")
# Uplink interference alarm: no 3GPP or X.733 probable cause names
# interference; "Degraded Signal" (TS 32.111-2 Annex B Table B.2,
# Communications event type) is the nearest value. Severity Major
# (service-affecting degradation, X.733 clause 8.1.2.3). The
# specificProblem text is vendor-style (ASSUMPTION).
INTERFERENCE_ALARM = (
    "Communications Alarm",
    "Degraded Signal",
    "Major",
    "uplink interference detected",
)
# Only the Huawei-style dialect raises an uplink interference alarm, when
# the rise reaches this level (ASSUMPTION, rule F2 "where a vendor raises
# one").
INTERFERENCE_ALARM_VENDOR = "huawei"
INTERFERENCE_ALARM_RISE_DB = 6.0


@dataclass(frozen=True)
class CmChange:
    """One entry of the CM change log.

    Attributes:
        time: Local time of the change.
        dn: Distinguished name of the changed object.
        attribute: What changed.
        old_value: Value before.
        new_value: Value after.
    """

    time: datetime
    dn: str
    attribute: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Alarm:
    """One alarm of the FM alarm log (TS 32.111-2 V19.0.0 clause 5.5.1 names).

    Attributes:
        alarm_id: Alarm id.
        dn: objectInstance, the alarmed cell's DN.
        alarm_raised_time: alarmRaisedTime.
        alarm_cleared_time: alarmClearedTime.
        event_type: eventType (Annex A).
        probable_cause: probableCause (Annex B).
        perceived_severity: perceivedSeverity.
        specific_problem: specificProblem (vendor text).
    """

    alarm_id: str
    dn: str
    alarm_raised_time: datetime
    alarm_cleared_time: datetime
    event_type: str
    probable_cause: str
    perceived_severity: str
    specific_problem: str


def cell_dn(model: NetworkModel, cell: int) -> str:
    """DN of a cell.

    Args:
        model: The network.
        cell: Global cell index.

    Returns:
        The DN from the world.
    """
    return model.world.cells[cell].dn


def cm_changes(model: NetworkModel, faults: list[Fault]) -> list[CmChange]:
    """CM change log entries of configuration faults and their restoration.

    Args:
        model: The network as built.
        faults: Planted faults.

    Returns:
        Entries in time order.
    """
    state = model.state
    out: list[CmChange] = []
    for f in faults:
        dn = cell_dn(model, f.cell)
        if f.kind == "F1a":
            old, new = state.tilt_deg[f.cell], MISTAKEN_TILT_DEG
            out.append(CmChange(f.start, dn, "electricalTiltDeg", f"{old:g}", f"{new:g}"))
            out.append(CmChange(f.end, dn, "electricalTiltDeg", f"{new:g}", f"{old:g}"))
        elif f.kind == "F1b":
            relation = f"{dn},EUtranRelation={model.world.cells[f.target].cell_name}"
            out.append(CmChange(f.start, relation, "EUtranRelation", "present", "deleted"))
            out.append(CmChange(f.end, relation, "EUtranRelation", "deleted", "present"))
        elif f.kind == "F1c":
            old, new = state.power_dbm[f.cell], state.power_dbm[f.cell] + POWER_DROP_DB
            out.append(CmChange(f.start, dn, "txPowerDbm", f"{old:.1f}", f"{new:.1f}"))
            out.append(CmChange(f.end, dn, "txPowerDbm", f"{new:.1f}", f"{old:.1f}"))
    return sorted(out, key=lambda c: (c.time, c.dn, c.attribute))


def fm_alarms(model: NetworkModel, faults: list[Fault], rise_db: dict[str, float]) -> list[Alarm]:
    """FM alarms of outages and vendor-raised interference.

    Args:
        model: The network as built.
        faults: Planted faults.
        rise_db: Uplink noise rise at the faulty cell per F1e fault id.

    Returns:
        Alarms in raise-time order.
    """
    out: list[Alarm] = []
    for f in faults:
        dn = cell_dn(model, f.cell)
        if f.kind == "F1f":
            fields = OUTAGE_ALARM
        elif (
            f.kind == "F1e"
            and model.state.vendor[f.cell] == INTERFERENCE_ALARM_VENDOR
            and rise_db[f.fault_id] >= INTERFERENCE_ALARM_RISE_DB
        ):
            fields = INTERFERENCE_ALARM
        else:
            continue
        out.append(Alarm(f"A{f.fault_id[1:]}", dn, f.start, f.end, *fields))
    return sorted(out, key=lambda a: (a.alarm_raised_time, a.alarm_id))
