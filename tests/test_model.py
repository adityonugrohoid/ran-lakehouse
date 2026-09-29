"""The network model (rules M1-M5): radio functions, counters and report."""

import json
from typing import Any

import numpy as np
import pytest

from ran_lakehouse.model import NetworkModel, default_model, derive, simulate_days
from ran_lakehouse.model import report as model_report
from ran_lakehouse.model.counters import noise_block
from ran_lakehouse.model.coverage import environment_weights
from ran_lakehouse.model.erlang import erlang_b
from ran_lakehouse.model.radio import (
    ANTENNA_GAIN_DBI,
    FRONT_TO_BACK_DB,
    MAX_SPECTRAL_EFFICIENCY,
    TA_STEP_M,
    antenna_gain_db,
    cqi_index,
    hata_path_loss_db,
    spectral_efficiency,
)
from ran_lakehouse.world import build_world


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


def test_erlang_b_known_values() -> None:
    assert erlang_b(np.array([1.0]), np.array([1]))[0] == pytest.approx(0.5)
    assert erlang_b(np.array([2.0]), np.array([2]))[0] == pytest.approx(0.4)
    assert erlang_b(np.array([0.0]), np.array([5]))[0] == 0.0
    with pytest.raises(ValueError, match="non-negative"):
        erlang_b(np.array([-1.0]), np.array([1]))


def test_path_loss_grows_with_distance_and_frequency() -> None:
    d = np.array([0.5, 1.0, 2.0, 5.0])
    hb, urban, none = np.full(4, 30.0), np.ones(4), np.zeros(4)
    loss_900 = hata_path_loss_db(940.0, d, hb, urban, none)
    loss_1800 = hata_path_loss_db(1840.0, d, hb, urban, none)
    loss_2300 = hata_path_loss_db(2350.0, d, hb, urban, none)
    assert np.all(np.diff(loss_900) > 0)
    assert np.all(loss_1800 > loss_900)
    assert np.all(loss_2300 > loss_1800)


def test_path_loss_orders_environments() -> None:
    d = np.full(3, 2.0)
    loss = hata_path_loss_db(
        940.0, d, np.full(3, 30.0), np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])
    )
    assert loss[0] > loss[1] > loss[2]


def test_environment_blend_is_continuous() -> None:
    density = np.linspace(0.0, 4000.0, 4001)
    urban, suburban = environment_weights(density)
    loss = hata_path_loss_db(
        940.0, np.full(density.size, 2.0), np.full(density.size, 30.0), urban, suburban
    )
    assert np.all(np.diff(loss) >= 0)
    assert np.max(np.abs(np.diff(loss))) < 0.1


def test_antenna_pattern() -> None:
    assert antenna_gain_db(np.array(0.0), np.array(0.0)) == pytest.approx(ANTENNA_GAIN_DBI)
    back = antenna_gain_db(np.array(180.0), np.array(0.0))
    assert back == pytest.approx(ANTENNA_GAIN_DBI - FRONT_TO_BACK_DB)


def test_link_mappings() -> None:
    assert spectral_efficiency(np.array(-20.0)) == 0.0
    assert spectral_efficiency(np.array(40.0)) == pytest.approx(MAX_SPECTRAL_EFFICIENCY)
    cqi = cqi_index(np.linspace(-15, 30, 50))
    assert cqi[0] == 0 and cqi[-1] == 15 and np.all(np.diff(cqi) >= 0)
    assert abs(TA_STEP_M - 78.07) < 0.01


def test_noise_is_fixed_per_cell_and_day() -> None:
    cells = np.array([3, 7])
    assert np.array_equal(noise_block(cells, 0, 96), noise_block(cells, 0, 96))
    assert not np.array_equal(noise_block(cells, 0, 96), noise_block(cells, 1, 96))


def test_same_model_same_counters(tiny: NetworkModel) -> None:
    first = next(simulate_days(tiny, 0, 1))
    second = next(simulate_days(default_model(build_world("tiny")), 0, 1))
    for name, value in first.lte.values.items():
        assert np.array_equal(value, second.lte.values[name]), name
    for name, value in first.gsm.values.items():
        assert np.array_equal(value, second.gsm.values[name]), name


def test_successes_never_exceed_attempts(tiny: NetworkModel) -> None:
    for day in simulate_days(tiny, 0, 2):
        for att, succ in model_report.LTE_PAIRS:
            assert np.all(day.lte.values[succ] <= day.lte.values[att]), succ
        for att, succ in model_report.GSM_PAIRS:
            assert np.all(day.gsm.values[succ] <= day.gsm.values[att]), succ


def test_cell_down_reports_unavailable(tiny: NetworkModel) -> None:
    down = tiny.state.down.copy()
    target = int(np.flatnonzero(tiny.state.technology == "LTE")[0])
    down[target] = True
    changed = derive(
        tiny, tiny.state.with_values(down=down), tiny.neighbours, tiny.persons, tiny.ul_rise_db
    )
    day = next(simulate_days(changed, 0, 1))
    column = int(np.flatnonzero(day.lte.cells == target)[0])
    assert np.all(day.lte.values["RRU.CellUnavailableTime.sum"][:, column] == 900.0)
    assert np.all(day.lte.values["RRC.ConnEstabAtt.sum"][:, column] == 0)


def test_missing_neighbour_moves_demand_to_drops(tiny: NetworkModel) -> None:
    src, tgt, mass = tiny.serving.relations
    lte = np.flatnonzero(tiny.state.technology[src] == "LTE")
    strongest = lte[np.argmax(mass[lte])]
    pair = (int(src[strongest]), int(tgt[strongest]))
    changed = derive(tiny, tiny.state, tiny.neighbours - {pair}, tiny.persons, tiny.ul_rise_db)
    base = next(simulate_days(tiny, 0, 1)).lte
    worse = next(simulate_days(changed, 0, 1)).lte
    column = int(np.flatnonzero(base.cells == pair[0])[0])
    drops = "ERAB.RelActNbr.sum"
    assert worse.values[drops][:, column].sum() > base.values[drops][:, column].sum()
    attempts = "HO.IntraFreqOutAtt"
    assert worse.values[attempts][:, column].sum() < base.values[attempts][:, column].sum()
    assert pair in base.relations and pair not in worse.relations


def test_relation_counters_sum_to_cell_counters(tiny: NetworkModel) -> None:
    day = next(simulate_days(tiny, 0, 1))
    for counters, rel_att, cell_att in (
        (day.lte, "HO.OutAttTarget.sum", "HO.IntraFreqOutAtt"),
        (
            day.gsm,
            "attOutgoingInternalInterCellHDOsPerTargetCell",
            "attOutgoingInternalInterCellHDOs",
        ),
    ):
        per_cell = np.zeros_like(counters.values[cell_att])
        column = {int(c): i for i, c in enumerate(counters.cells)}
        for r, (source, _) in enumerate(counters.relations):
            per_cell[:, column[source]] += np.nan_to_num(counters.relation_values[rel_att][:, r])
        assert np.array_equal(per_cell, counters.values[cell_att])


def test_offset_moves_users(tiny: NetworkModel) -> None:
    lte = np.flatnonzero(tiny.state.technology == "LTE")
    busiest = int(lte[np.argmax(tiny.serving.subscribers[lte])])
    cio = tiny.state.cio_db.copy()
    cio[busiest] = -6.0
    changed = derive(
        tiny, tiny.state.with_values(cio_db=cio), tiny.neighbours, tiny.persons, tiny.ul_rise_db
    )
    assert changed.serving.subscribers[busiest] < tiny.serving.subscribers[busiest]


def test_report_markdown_matches_record() -> None:
    record = json.loads(model_report.RECORD_JSON.read_text())
    assert model_report.RECORD_MD.read_text() == model_report.render_markdown(record)


def differences(a: Any, b: Any, path: str, rel: float) -> list[str]:
    """Paths where two JSON values differ beyond a relative tolerance.

    Args:
        a: First value.
        b: Second value.
        path: Path of the values, for the report.
        rel: Relative tolerance for numbers.

    Returns:
        The differing paths with both values.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return [f"{path}: keys {sorted(a)} != {sorted(b)}"]
        return [d for k in a for d in differences(a[k], b[k], f"{path}.{k}", rel)]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in differences(x, y, f"{path}[{i}]", rel)
        ]
    both_numbers = isinstance(a, int | float) and isinstance(b, int | float)
    if both_numbers and not isinstance(a, bool):
        close = abs(a - b) <= rel * max(abs(a), abs(b)) + 1e-9
        return [] if close else [f"{path}: {a} != {b}"]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def test_committed_record_matches_a_fresh_run() -> None:
    """A fresh 12-week run reproduces the committed record.

    Numbers may differ across machines by floating-point rounding (vectorized
    math differs by CPU), which can move a rounded counter by one; a relative
    tolerance of 1e-4 absorbs that and nothing larger.
    """
    committed = json.loads(model_report.RECORD_JSON.read_text())
    fresh, _, _, _ = model_report.build()
    fresh = json.loads(json.dumps(fresh))
    committed.pop("timing_s")
    fresh.pop("timing_s")
    found = differences(committed, fresh, "record", 1e-4)
    assert not found, "\n".join(found)
    checks = committed["demo"]["relationship_checks"]
    assert all(c["pass"] for c in checks), [c["check"] for c in checks if not c["pass"]]
    assert not any(committed["demo"]["invariant_violations"].values())
