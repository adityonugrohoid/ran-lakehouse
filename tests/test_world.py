"""The synthetic world (rule W): determinism, structure and the committed report."""

import json
import re

import pytest

from ran_lakehouse.world import World, build_world
from ran_lakehouse.world import report as world_report
from ran_lakehouse.world.network import BANDS, TDD_BANDS
from ran_lakehouse.world.profiles import AZIMUTH_JITTER_SD_DEG, SECTOR_AZIMUTHS_DEG

# TS 32.300 clause 7.3: RDN "Class=value", class starting with a capital
# letter, values without separators or surrounding spaces.
RDN = re.compile(r"^[A-Z][A-Za-z0-9-]*=[A-Za-z0-9_.-]+$")


@pytest.fixture(scope="module")
def tiny() -> World:
    return build_world("tiny")


@pytest.fixture(scope="module")
def demo() -> World:
    return build_world("demo")


def test_same_profile_same_world() -> None:
    first = build_world("tiny")
    second = build_world("tiny")
    assert world_report.fingerprint(first) == world_report.fingerprint(second)


def test_committed_record_matches_a_fresh_build() -> None:
    committed = json.loads(world_report.RECORD_JSON.read_text())
    assert committed == json.loads(json.dumps(world_report.build_record()))


def test_report_markdown_matches_record() -> None:
    record = json.loads(world_report.RECORD_JSON.read_text())
    assert world_report.RECORD_MD.read_text() == world_report.render_markdown(record)


def test_unknown_profile_is_an_error() -> None:
    with pytest.raises(KeyError, match="unknown profile"):
        build_world("huge")


@pytest.mark.parametrize("name", ["tiny", "demo"])
def test_ids_and_dns(name: str) -> None:
    world = build_world(name)
    assert len({s.site_id for s in world.sites}) == len(world.sites)
    assert len({c.cell_name for c in world.cells}) == len(world.cells)
    assert len({c.dn for c in world.cells}) == len(world.cells)
    for cell in world.cells:
        rdns = cell.dn.split(",")
        assert rdns[0] == "SubNetwork=RanLake"
        assert all(RDN.match(r) for r in rdns), cell.dn
        assert rdns[-1] == f"{cell.object_class}={cell.cell_name}"


def test_cell_classes_follow_band_and_technology(demo: World) -> None:
    for cell in demo.cells:
        assert cell.band in BANDS
        if cell.technology == "GSM":
            assert cell.object_class == "GsmCell"
            assert ",BssFunction=1,BtsSiteMgr=" in cell.dn
        elif cell.band in TDD_BANDS:
            assert cell.object_class == "EUtranCellTDD"
        else:
            assert cell.object_class == "EUtranCellFDD"


def test_sectors_near_nominal_azimuths(demo: World) -> None:
    for site in demo.sites:
        for nominal, azimuth in zip(SECTOR_AZIMUTHS_DEG, site.azimuths_deg, strict=True):
            off = (azimuth - nominal + 180.0) % 360.0 - 180.0
            assert abs(off) <= 5 * AZIMUTH_JITTER_SD_DEG


def test_sites_only_in_served_region(demo: World) -> None:
    assert all(0 <= s.x_km < demo.profile.served_width_km for s in demo.sites)
    assert all(0 <= s.y_km < demo.profile.height_km for s in demo.sites)


def test_expansion_area_has_only_villages(demo: World) -> None:
    kinds = {s.kind for s in demo.population.settlements if s.area == "expansion"}
    assert kinds == {"village"}


def test_band_rules(demo: World) -> None:
    for site in demo.sites:
        if site.has_lte:
            assert "B3" in site.lte_bands
        else:
            assert site.area_class == "rural" and site.has_gsm
        if site.area_class == "rural":
            assert not {"B1", "B40"} & set(site.lte_bands)
        else:
            assert not {"B8", "B28"} & set(site.lte_bands)


def test_demo_counts_within_start_ranges(demo: World) -> None:
    """Rule W6 and W7 START targets: about 300 sites, cell shares 80-85 / 15-20."""
    lte = sum(c.technology == "LTE" for c in demo.cells)
    share = lte / len(demo.cells)
    assert 250 <= len(demo.sites) <= 350
    assert 0.80 <= share <= 0.85
    gsm_sites = sum(s.has_gsm for s in demo.sites)
    assert 0.25 <= gsm_sites / len(demo.sites) <= 0.40
    assert sum(not s.has_lte for s in demo.sites) <= 10
