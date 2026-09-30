"""Planted faults, their traces and the what-if (rules F1-F4, M6)."""

import json
from datetime import timedelta

import numpy as np
import pytest

from ran_lakehouse.faults import report as fault_report
from ran_lakehouse.faults.evaluate import evaluate, right_fix
from ran_lakehouse.faults.locality import MAX_SHARE, reach
from ran_lakehouse.faults.plant import (
    KINDS,
    MISTAKEN_TILT_DEG,
    POWER_DROP_DB,
    Fault,
    apply_faults,
    detail,
    eligible,
    plan_faults,
)
from ran_lakehouse.faults.simulate import simulate_with_faults
from ran_lakehouse.faults.traces import cm_changes, fm_alarms
from ran_lakehouse.faults.whatif import Change, apply_changes, what_if
from ran_lakehouse.model import DAY_TRACKER, RUN_START, NetworkModel, default_model, simulate_days
from ran_lakehouse.world import build_world

WEEK = timedelta(days=7)


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


def planted(model: NetworkModel, kind: str, hours: float) -> Fault:
    cells = eligible(model, kind)
    return detail(
        model, "F9999", kind, int(cells[0]), RUN_START, RUN_START + timedelta(hours=hours)
    )


def test_plan_is_deterministic_and_never_doubles_a_cell(tiny: NetworkModel) -> None:
    first = plan_faults(tiny, 4)
    assert [repr(f) for f in first] == [repr(f) for f in plan_faults(tiny, 4)]
    assert {f.kind for f in first} <= set(KINDS)
    for i, a in enumerate(first):
        for b in first[i + 1 :]:
            if a.cell == b.cell:
                assert a.end <= b.start or b.end <= a.start


def test_faults_change_the_model(tiny: NetworkModel) -> None:
    for kind in ("F1a", "F1c", "F1f"):
        cell = int(eligible(tiny, "F1c")[0])
        fault = detail(tiny, "F9999", kind, cell, RUN_START, RUN_START + WEEK)
        faulty = apply_faults(tiny, [fault])
        if kind == "F1a":
            assert faulty.state.tilt_deg[cell] == MISTAKEN_TILT_DEG
        if kind == "F1c":
            assert faulty.state.power_dbm[cell] == tiny.state.power_dbm[cell] + POWER_DROP_DB
        if kind == "F1f":
            assert faulty.state.down[cell]


def test_missing_neighbour_is_not_reported(tiny: NetworkModel) -> None:
    fault = planted(tiny, "F1b", 24 * 7)
    day = next(simulate_with_faults(tiny, [fault], 0, 1))
    column = day.lte.relations.index((fault.cell, fault.target))
    assert np.isnan(day.lte.relation_values["HO.OutAttTarget.sum"][:, column]).all()


def test_outage_is_spliced_by_period(tiny: NetworkModel) -> None:
    fault = planted(tiny, "F1f", 6)
    day = next(simulate_with_faults(tiny, [fault], 0, 1))
    counters = day.lte if tiny.state.technology[fault.cell] == "LTE" else day.gsm
    column = int(np.flatnonzero(counters.cells == fault.cell)[0])
    if counters is day.lte:
        unavailable = counters.values["RRU.CellUnavailableTime.sum"][:, column]
        assert np.all(unavailable[:24] == 900.0) and np.all(unavailable[24:] == 0.0)


def test_cm_and_fm_traces(tiny: NetworkModel) -> None:
    tilt = planted(tiny, "F1a" if eligible(tiny, "F1a").size else "F1c", 48)
    log = cm_changes(tiny, [tilt])
    assert len(log) == 2 and log[0].time == tilt.start and log[1].time == tilt.end
    outage = planted(tiny, "F1f", 6)
    alarms = fm_alarms(tiny, [outage], {})
    assert alarms[0].probable_cause == "Transmitter Failure"
    assert alarms[0].alarm_raised_time == outage.start
    assert alarms[0].alarm_cleared_time == outage.end


def test_what_if_bounds(tiny: NetworkModel) -> None:
    cell = int(eligible(tiny, "F1c")[0])
    with pytest.raises(ValueError, match="tilt step"):
        apply_changes(tiny, [Change("tilt", cell, 3.0, -1)])
    with pytest.raises(ValueError, match="power step"):
        apply_changes(tiny, [Change("power", cell, -4.0, -1)])
    with pytest.raises(ValueError, match="offset"):
        apply_changes(tiny, [Change("cio", cell, -3.0, -1)] * 3)


def test_what_if_uses_common_noise(tiny: NetworkModel) -> None:
    cell = int(eligible(tiny, "F1c")[0])
    result = what_if(tiny, [Change("tilt", cell, 0.0, -1)], 0)
    assert result.before == result.after


def test_right_fix_restores_a_configuration_fault(tiny: NetworkModel) -> None:
    fault = planted(tiny, "F1c", 24 * 7)
    evaluation = evaluate(tiny, fault, try_parameters=False)
    assert evaluation.fixed["area"] == evaluation.clean["area"]
    assert right_fix(tiny, fault)


def test_report_markdown_matches_record() -> None:
    record = json.loads(fault_report.RECORD_JSON.read_text())
    assert fault_report.RECORD_MD.read_text() == fault_report.render_markdown(record)


def test_report_holds_no_planted_answers(tiny: NetworkModel) -> None:
    text = fault_report.RECORD_JSON.read_text()
    demo = default_model(build_world("demo"))
    for fault in plan_faults(demo, fault_report.WEEKS):
        assert fault.fault_id not in text
        assert fault.start.isoformat() not in text


def test_runs_stream_one_day_at_a_time(tiny: NetworkModel) -> None:
    """No run keeps its days: at most the day being made and the one in use."""
    faults = plan_faults(tiny, 2)
    lte_cells = np.flatnonzero(tiny.state.technology == "LTE")
    DAY_TRACKER.reset_peak()
    fault_report.run_kpis(simulate_with_faults(tiny, faults, 0, 14), lte_cells)
    fault_report.run_kpis(simulate_days(tiny, 0, 14), lte_cells)
    evaluate(tiny, planted(tiny, "F1e", 24 * 7), try_parameters=True)
    evaluate(tiny, planted(tiny, "F1f", 6), try_parameters=False)
    fault_report.daily_series(tiny, planted(tiny, "F1c", 24 * 7), "F1c")
    assert DAY_TRACKER.peak <= 2


def test_no_planted_fault_reaches_beyond_its_area() -> None:
    demo = default_model(build_world("demo"))
    reaches = [reach(demo, f) for f in plan_faults(demo, 12)]
    assert reaches
    worst = max(reaches, key=lambda r: r.share)
    assert worst.share <= MAX_SHARE, worst
