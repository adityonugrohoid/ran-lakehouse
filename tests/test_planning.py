"""Planning geography (rules G1 to G7)."""

import duckdb
import numpy as np
import pytest

from ran_lakehouse.model import NetworkModel, default_model
from ran_lakehouse.planning import backhaul, geography, radio
from ran_lakehouse.planning.build import Plan, build_plan, tables, write_gold
from ran_lakehouse.planning.terrain import RELIEF_M, Terrain, build_terrain
from ran_lakehouse.world import World, build_world
from ran_lakehouse.world.profiles import DEMO


@pytest.fixture(scope="module")
def demo_world() -> World:
    return build_world("demo")


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


@pytest.fixture(scope="module")
def tiny_plan(tiny: NetworkModel) -> Plan:
    return build_plan(tiny)


def flat(height_m: float) -> Terrain:
    return Terrain(0.0, 0.0, 0.1, np.full((101, 301), height_m))


def test_terrain_spans_the_relief_and_is_deterministic() -> None:
    first, second = build_terrain(DEMO), build_terrain(DEMO)
    assert first.heights_m.min() == pytest.approx(RELIEF_M[0])
    assert first.heights_m.max() == pytest.approx(RELIEF_M[1])
    assert np.array_equal(first.heights_m, second.heights_m)
    assert first.x0_km == DEMO.served_width_km


def test_schools_follow_the_start_rule() -> None:
    assert geography.schools(400) == 0
    assert geography.schools(600) == 1
    assert geography.schools(3_000) == 2
    assert geography.schools(8_000) == 5


def test_candidates_sit_on_high_points_near_villages(demo_world: World) -> None:
    terrain = build_terrain(demo_world.profile)
    villages = geography.villages(demo_world, terrain)
    sites = geography.candidates(terrain, villages)
    assert len(villages) == 150 and len(sites) == geography.CANDIDATES
    hx, hy, _ = geography.high_points(terrain)
    high = set(zip(np.round(hx, 3), np.round(hy, 3), strict=True))
    for s in sites:
        assert (round(s.x_km, 3), round(s.y_km, 3)) in high
        assert s.nearest_village_km <= geography.NEAR_VILLAGE_KM
    xy = np.array([(s.x_km, s.y_km) for s in sites])
    gaps = np.hypot(*(xy[:, None, :] - xy[None, :, :]).transpose(2, 0, 1))
    assert gaps[~np.eye(len(sites), dtype=bool)].min() >= geography.MIN_SPACING_KM


def test_knife_edge_loss_matches_p526() -> None:
    # Eq (31) approximates the exact 6.02 dB at grazing incidence to about 0.01 dB.
    assert radio.j_loss_db(np.array([0.0]))[0] == pytest.approx(6.02, abs=0.02)
    assert radio.j_loss_db(np.array([-1.0]))[0] == 0.0
    n = radio.PROFILE_SAMPLES
    ground = np.zeros((1, n))
    ground[0, n // 2] = 100.0
    loss = radio.bullington_db(np.array([10.0]), ground, np.array([50.0]), np.array([50.0]), 900.0)
    wavelength = 299.792458 / 900.0
    v = (100.0 + 500.0 * 25.0 / radio.EFFECTIVE_EARTH_RADIUS_KM - 50.0) * np.sqrt(
        0.002 * 10.0 / (wavelength * 25.0)
    )
    j = radio.j_loss_db(np.array([v]))[0]
    assert loss[0] == pytest.approx(j + (1 - np.exp(-j / 6)) * (10 + 0.02 * 10.0))
    clear = radio.bullington_db(
        np.array([10.0]), np.zeros((1, n)), np.array([30.0]), np.array([30.0]), 900.0
    )
    assert clear[0] == 0.0


def test_fresnel_radius_and_line_of_sight() -> None:
    assert radio.fresnel_radius_m(np.array(5.0), np.array(5.0), 8.0) == pytest.approx(
        17.3 * np.sqrt(25.0 / 80.0)
    )
    clear, _ = radio.line_of_sight(flat(300.0), (1.0, 5.0, 30.0), (29.0, 5.0, 30.0), 8.0, 0.6)
    assert clear
    hill = flat(300.0)
    hill.heights_m[:, 140:160] = 340.0
    blocked, _ = radio.line_of_sight(hill, (1.0, 5.0, 30.0), (29.0, 5.0, 30.0), 8.0, 0.6)
    assert not blocked


def test_indoor_service_today_is_stricter_than_outdoor(tiny_plan: Plan) -> None:
    villages = tables(tiny_plan)["villages"].to_pylist()
    for v in villages:
        if v["covered_today_lte"]:
            assert v["served_lte_rsrp_dbm"] >= radio.LTE_RSRP_THRESHOLD_DBM + radio.INDOOR_MARGIN_DB
        if v["covered_today_gsm"]:
            assert v["served_gsm_rxlev_dbm"] >= (
                radio.GSM_RXLEV_THRESHOLD_DBM + radio.INDOOR_MARGIN_DB
            )


def test_backhaul_and_power_rules(tiny_plan: Plan) -> None:
    by_site: dict[str, dict[str, backhaul.Option]] = {}
    for o in tiny_plan.options:
        by_site.setdefault(o.site_id, {})[o.kind] = o
    assert by_site
    for options in by_site.values():
        assert options["satellite"].available
        assert options["grid_power"].available != options["solar_power"].available
        assert options["grid_power"].available == (
            options["grid_power"].distance_km <= backhaul.GRID_POWER_KM
        )
        if options["microwave"].available:
            assert options["microwave"].distance_km <= backhaul.MAX_HOP_KM
        if options["microwave"].clears_full_fresnel:
            assert options["microwave"].available


def test_gold_tables_are_written(tiny_plan: Plan) -> None:
    data = tables(tiny_plan)
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lk")
    write_gold(con, data)
    write_gold(con, data)  # rewritten whole, not appended
    for name, table in data.items():
        row = con.execute(f"SELECT count(*) FROM lk.gold.{name}").fetchone()
        assert row is not None and row[0] == table.num_rows
    sites = len(tiny_plan.candidates)
    assert data["coverage"].num_rows == sites * len(tiny_plan.villages) * 2
    assert data["backhaul_power_options"].num_rows == sites * 5
