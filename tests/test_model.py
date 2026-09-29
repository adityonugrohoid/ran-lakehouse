"""The network model (rules M1-M5): radio functions, counters and report."""

import json

import numpy as np
import pytest

from ran_lakehouse.model import NetworkModel, build_model, default_model, simulate_days
from ran_lakehouse.model import report as model_report
from ran_lakehouse.model.counters import noise_block
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
    env = np.array(["urban"] * 4)
    loss_900 = hata_path_loss_db(940.0, d, np.full(4, 30.0), env)
    loss_1800 = hata_path_loss_db(1840.0, d, np.full(4, 30.0), env)
    loss_2300 = hata_path_loss_db(2350.0, d, np.full(4, 30.0), env)
    assert np.all(np.diff(loss_900) > 0)
    assert np.all(loss_1800 > loss_900)
    assert np.all(loss_2300 > loss_1800)


def test_path_loss_orders_environments() -> None:
    d = np.full(3, 2.0)
    loss = hata_path_loss_db(940.0, d, np.full(3, 30.0), np.array(["urban", "suburban", "rural"]))
    assert loss[0] > loss[1] > loss[2]


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
    changed = build_model(tiny.world, tiny.state.with_values(down=down), frozenset())
    day = next(simulate_days(changed, 0, 1))
    column = int(np.flatnonzero(day.lte.cells == target)[0])
    assert np.all(day.lte.values["RRU.CellUnavailableTime.sum"][:, column] == 900.0)
    assert np.all(day.lte.values["RRC.ConnEstabAtt.sum"][:, column] == 0)


def test_missing_neighbours_raise_drops(tiny: NetworkModel) -> None:
    src, tgt, _ = tiny.serving.relations
    lte = tiny.state.technology[src] == "LTE"
    cell = int(src[lte][0])
    removed = frozenset((int(s), int(t)) for s, t in zip(src, tgt, strict=True) if int(s) == cell)
    changed = build_model(tiny.world, tiny.state, removed)
    base = next(simulate_days(tiny, 0, 1)).lte
    worse = next(simulate_days(changed, 0, 1)).lte
    column = int(np.flatnonzero(base.cells == cell)[0])
    drops = "ERAB.RelActNbr.sum"
    assert worse.values[drops][:, column].sum() > base.values[drops][:, column].sum()
    ho = "HO.IntraFreqOutSucc"
    assert worse.values[ho][:, column].sum() < base.values[ho][:, column].sum()


def test_report_markdown_matches_record() -> None:
    record = json.loads(model_report.RECORD_JSON.read_text())
    assert model_report.RECORD_MD.read_text() == model_report.render_markdown(record)


def test_committed_record_matches_a_fresh_run() -> None:
    committed = json.loads(model_report.RECORD_JSON.read_text())
    fresh, _, _, _ = model_report.build()
    fresh = json.loads(json.dumps(fresh))
    committed.pop("timing_s")
    fresh.pop("timing_s")
    assert committed == fresh
    checks = committed["demo"]["relationship_checks"]
    assert all(c["pass"] for c in checks), [c["check"] for c in checks if not c["pass"]]
    assert not any(committed["demo"]["invariant_violations"].values())
