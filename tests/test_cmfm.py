"""CM and FM exports (rules C1, C2, F2) on the tiny profile."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from ran_lakehouse.faults.plant import (
    MISTAKEN_TILT_DEG,
    Fault,
    detail,
    eligible,
    plan_faults,
    uplink_rise_db,
)
from ran_lakehouse.faults.traces import INTERFERENCE_ALARM_RISE_DB
from ran_lakehouse.files import oss_report
from ran_lakehouse.files.cmfm import alarm_notifications, change_log, day_exports, snapshot
from ran_lakehouse.files.dialects import HUAWEI_R1, NOKIA_R1
from ran_lakehouse.files.ems import WIB, Ems
from ran_lakehouse.model import RUN_START, NetworkModel, default_model
from ran_lakehouse.world import build_world

HW = Ems("EMS-HW-01", HUAWEI_R1, WIB, "Huawei-style synthetic EMS", "3gpp-xml")
NK = Ems("EMS-NK-01", NOKIA_R1, UTC, "Nokia-style synthetic EMS", "omes")


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


def fault_on(model: NetworkModel, kind: str, vendor: str, hours: float) -> Fault:
    cells = [c for c in eligible(model, kind) if model.state.vendor[c] == vendor]
    start = RUN_START + timedelta(hours=10)
    return detail(model, "F9999", kind, int(cells[0]), start, start + timedelta(hours=hours))


def test_snapshot_carries_cells_and_relations(tiny: NetworkModel) -> None:
    records = snapshot(tiny, [], HW, RUN_START.date())
    classes = {r["objectClass"] for r in records}
    assert classes <= {"EUtranCellFDD", "EUtranCellTDD", "GsmCell", "EUtranRelation", "GsmRelation"}
    cells = [r for r in records if "Relation" not in r["objectClass"]]
    assert len(cells) == int((tiny.state.vendor == "huawei").sum())
    assert all("cellIndividualOffsetDb" in r["attributes"] for r in cells)
    assert records[0]["snapshotTime"].endswith("+07:00")


def test_tilt_fault_is_logged_and_in_the_next_snapshot(tiny: NetworkModel) -> None:
    fault = fault_on(tiny, "F1c", "huawei", 48)
    tilt = Fault("F9999", "F1a", fault.cell, fault.start, fault.end, -1, float("nan"), float("nan"))
    log = change_log(tiny, [tilt], HW, tilt.start.date())
    assert log[0]["attribute"] == "electricalTiltDeg"
    assert log[0]["newValue"] == f"{MISTAKEN_TILT_DEG:g}"
    next_day = snapshot(tiny, [tilt], HW, tilt.start.date() + timedelta(days=1))
    dn = tiny.world.cells[fault.cell].dn
    record = next(r for r in next_day if r["dn"] == dn)
    assert record["attributes"]["electricalTiltDeg"] == MISTAKEN_TILT_DEG
    restored = change_log(tiny, [tilt], HW, tilt.end.date())
    assert restored[-1]["oldValue"] == f"{MISTAKEN_TILT_DEG:g}"


def test_outage_raises_and_clears(tiny: NetworkModel) -> None:
    outage = fault_on(tiny, "F1f", "nokia", 6)
    notes = alarm_notifications(tiny, [outage], NK, outage.start.date())
    kinds = [n["notificationType"] for n in notes]
    assert kinds == ["notifyNewAlarm", "notifyClearedAlarm"]
    assert notes[0]["probableCause"] == "Transmitter Failure"
    assert notes[1]["perceivedSeverity"] == "Cleared"
    raised = datetime.fromisoformat(notes[0]["eventTime"])
    assert raised.utcoffset() == timedelta(0)
    assert raised == outage.start.replace(tzinfo=WIB)
    assert notes[0]["objectInstance"].startswith("PLMN-PLMN/")


def test_only_the_huawei_style_ems_raises_interference_alarms(tiny: NetworkModel) -> None:
    for ems, vendor in ((HW, "huawei"), (NK, "nokia")):
        cells = [c for c in eligible(tiny, "F1e") if tiny.state.vendor[c] == vendor]
        if not cells:
            continue
        start = RUN_START + timedelta(hours=10)
        fault = detail(tiny, "F9999", "F1e", int(cells[0]), start, start + timedelta(days=2))
        notes = alarm_notifications(tiny, [fault], ems, start.date())
        rise = float(uplink_rise_db(tiny, fault)[fault.cell])
        raised = vendor == "huawei" and rise >= INTERFERENCE_ALARM_RISE_DB
        assert [n["notificationType"] for n in notes] == (["notifyNewAlarm"] if raised else [])


def test_exports_are_gzip_json_lines(tiny: NetworkModel) -> None:
    faults = plan_faults(tiny, 2)
    names = []
    for name, content, count in day_exports(tiny, faults, HW, RUN_START.date()):
        lines = gzip.decompress(content).decode().splitlines()
        assert len(lines) == count
        assert all(isinstance(json.loads(line), dict) for line in lines)
        names.append(name)
    assert names == [f"{k}_EMS-HW-01_20260105.jsonl.gz" for k in ("CM", "CMLOG", "FM")]


def test_report_markdown_matches_record() -> None:
    record = json.loads(oss_report.RECORD_JSON.read_text())
    assert oss_report.RECORD_MD.read_text() == oss_report.render_markdown(record)


def test_report_exports_match_the_schedule() -> None:
    record = json.loads(oss_report.RECORD_JSON.read_text())
    for ems in record["ems"].values():
        for exported, implied in ems["consistency"].values():
            assert exported == implied
