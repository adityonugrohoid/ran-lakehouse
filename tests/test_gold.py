"""Gold (rules L3, L5, D6) on the tiny profile, through dbt, in a DuckDB file.

One week of the run (days 35 to 41) goes through bronze and silver into a
DuckDB file, then gold is built by the dbt project one UTC day at a time,
with the planted formula revision inside the week.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from ran_lakehouse.collect.backfill import EMS_LIST, anomalies_table, drive_into
from ran_lakehouse.collect.delivery import DeliveryPlan, plan_delivery
from ran_lakehouse.faults.plant import plan_faults
from ran_lakehouse.lake import bronze, gold, kpi_catalog, silver
from ran_lakehouse.lake.gold import GoldBuild, Revision, Target
from ran_lakehouse.lake.gold_check import compare
from ran_lakehouse.lake.lineage import lineage
from ran_lakehouse.model import RUN_START, NetworkModel, default_model
from ran_lakehouse.world import build_world

WEEKS = 6
FIRST_DAY = 35
DAYS = 7
REVISION = Revision("LTE_RRC_SSR", 2, (RUN_START + timedelta(days=38)).date())
# A low PRB threshold so the tiny week has persistent worst cells to rank.
PRB_THRESHOLD = 15.0


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


@pytest.fixture(scope="module")
def plan(tiny: NetworkModel) -> DeliveryPlan:
    return plan_delivery(tiny, EMS_LIST, 7 * WEEKS)


@pytest.fixture(scope="module")
def lake_file(
    tiny: NetworkModel, plan: DeliveryPlan, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    folder = tmp_path_factory.mktemp("gold")
    path = folder / "lake.duckdb"
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"ATTACH '{path}' AS lk")
    con.execute("CREATE SCHEMA lk.bronze")
    con.execute("CREATE SCHEMA lk.evaluation")
    for table, columns in bronze.TABLES.items():
        con.execute(f"CREATE TABLE {table} {columns}")
    bronze.append(con, f"{bronze.EVALUATION}.delivery_anomalies", anomalies_table(plan))
    drive_into(con, "tiny", WEEKS, FIRST_DAY, DAYS, folder / "landing", None)
    silver.create_tables(con, False)
    silver.SilverBuild(con, "test", silver.GRACE).run(None)
    con.close()
    return path


@pytest.fixture(scope="module")
def built(lake_file: Path) -> dict[str, Any]:
    patched = tuple(
        replace(k, breach_threshold=PRB_THRESHOLD) if k.kpi_id == "LTE_PRB_UTIL" else k
        for k in kpi_catalog.KPIS
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gold, "KPIS", patched)
        return GoldBuild(Target("file", str(lake_file)), "test", REVISION).run()


@pytest.fixture
def con(lake_file: Path, built: dict[str, Any]) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = Target("file", str(lake_file)).connect()
    yield connection
    connection.close()


def rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    return con.execute(sql).fetchall()


def test_every_daily_kpi_agrees_with_the_model(
    con: duckdb.DuckDBPyConnection, tiny: NetworkModel, plan: DeliveryPlan
) -> None:
    faults = plan_faults(tiny, WEEKS)
    result = compare(con, tiny, faults, plan, range(FIRST_DAY, FIRST_DAY + DAYS))
    expected = {f"{k.kpi_id} v{k.formula_version}" for k in kpi_catalog.KPIS}
    expected.add(f"{REVISION.kpi_id} v{REVISION.version}")
    # The tiny network has no Nokia-style GSM cells.
    expected.discard("GSM_SDCCH_DROP v1")
    assert set(result) == expected
    for key, c in result.items():
        assert c.compared > 0, key
        assert c.missing_in_gold == 0, key
        assert c.agreeing == c.compared, f"{key}: max difference {c.max_abs_diff}"


def test_revision_keeps_both_versions_over_the_whole_history(
    con: duckdb.DuckDBPyConnection,
) -> None:
    per_version = dict(
        rows(
            con,
            "SELECT formula_version, count(*) FROM lk.gold.lte_kpi_15m "
            "WHERE kpi_id = 'LTE_RRC_SSR' GROUP BY 1",
        )
    )
    assert set(per_version) == {1, 2} and per_version[1] == per_version[2]
    worse = rows(
        con,
        "SELECT count(*) FROM lk.gold.lte_kpi_day a JOIN lk.gold.lte_kpi_day b "
        "USING (cell_name, day, kpi_id) WHERE kpi_id = 'LTE_RRC_SSR' "
        "AND a.formula_version = 1 AND b.formula_version = 2 AND b.value > a.value + 1e-9",
    )
    assert worse == [(0,)]
    assert rows(
        con,
        "SELECT kpi_id, from_version, to_version, effective_day FROM lk.evaluation.kpi_revisions",
    ) == [(REVISION.kpi_id, 1, 2, REVISION.effective_day)]
    assert rows(
        con,
        "SELECT effective_from FROM lk.gold.kpi_catalog "
        "WHERE kpi_id = 'LTE_RRC_SSR' AND formula_version = 2",
    ) == [(REVISION.effective_day,)]
    reprocessed = rows(con, "SELECT count(*) FROM lk.gold.loads WHERE kind = 'reprocess'")
    assert reprocessed == [(1,)]


def test_day_values_are_ratios_of_sums(con: duckdb.DuckDBPyConnection) -> None:
    mismatch = rows(
        con,
        """WITH q AS (
            SELECT cell_name, CAST(period_start + INTERVAL 7 HOUR AS DATE) AS day,
                sum(numerator) AS n, sum(denominator) AS d, avg(value) AS mean_of_ratios
            FROM lk.gold.lte_kpi_15m WHERE kpi_id = 'LTE_ERAB_DROP' GROUP BY ALL
        )
        SELECT count(*) FILTER (WHERE abs(100 * q.n / q.d - d.value) > 1e-9),
            count(*) FILTER (WHERE abs(q.mean_of_ratios - d.value) > 1e-6)
        FROM q JOIN lk.gold.lte_kpi_day d USING (cell_name, day)
        WHERE d.kpi_id = 'LTE_ERAB_DROP' AND d.coverage = 1""",
    )
    ratio_of_sums_breaks, differs_from_mean_of_ratios = mismatch[0]
    assert ratio_of_sums_breaks == 0
    assert differs_from_mean_of_ratios > 0


def test_coverage_and_suspect_share_show_planted_gaps(
    con: duckdb.DuckDBPyConnection, tiny: NetworkModel, plan: DeliveryPlan
) -> None:
    vendor_of = {e.ems_id: e.dialect.vendor for e in EMS_LIST}
    in_week = range(FIRST_DAY * 96, (FIRST_DAY + DAYS) * 96)
    missing = [a for a in plan.anomalies if a.kind == "D3" and a.period in in_week]
    suspect = [a for a in plan.anomalies if a.kind == "D4" and a.period in in_week]
    assert missing and suspect
    for a in missing:
        day = (RUN_START + timedelta(days=a.period // 96)).date()
        cells = [
            c.cell_name
            for c, v in zip(tiny.world.cells, tiny.state.vendor, strict=True)
            if v == vendor_of[a.ems_id] and c.technology == "LTE"
        ]
        coverage = rows(
            con,
            f"SELECT DISTINCT coverage FROM lk.gold.lte_kpi_day WHERE day = DATE '{day}' "
            f"AND kpi_id = 'LTE_ERAB_ACC' AND cell_name IN ('{"', '".join(cells)}')",
        )
        assert coverage and all(c <= 95 / 96 + 1e-12 for (c,) in coverage)
    for a in suspect:
        day = (RUN_START + timedelta(days=a.period // 96)).date()
        cells = [c.cell_name for c in tiny.world.cells if c.managed_element == a.element]
        share = rows(
            con,
            "SELECT min(suspect_share) FROM (SELECT * FROM lk.gold.lte_kpi_day "
            "UNION ALL SELECT * FROM lk.gold.gsm_kpi_day) "
            f"WHERE day = DATE '{day}' AND cell_name IN ('{"', '".join(cells)}') "
            # The hourly CQI block is suspect only when the interrupted
            # collection was the hour's last quarter.
            "AND kpi_id <> 'LTE_CQI_MEAN'",
        )
        assert share[0][0] is not None and share[0][0] > 0


def test_worst_cells_follow_the_persistence_rule(con: duckdb.DuckDBPyConnection) -> None:
    ranked = rows(
        con,
        "SELECT week_start, cell_name, breach_days, rank FROM lk.gold.worst_cells_week "
        "WHERE kpi_id = 'LTE_PRB_UTIL' ORDER BY week_start, rank",
    )
    assert ranked
    expected = rows(
        con,
        f"""SELECT CAST(date_trunc('week', day) AS DATE), cell_name,
            count(*) FILTER (WHERE value > {PRB_THRESHOLD})
        FROM lk.gold.lte_kpi_day
        WHERE kpi_id = 'LTE_PRB_UTIL' AND coverage >= {gold.MIN_COVERAGE}
        GROUP BY ALL HAVING count(*) FILTER (WHERE value > {PRB_THRESHOLD})
            >= {gold.PERSISTENCE_N}""",
    )
    assert {(w, c, n) for w, c, n, _ in ranked} == set(expected)
    for week in {w for w, *_ in ranked}:
        ranks = [r for w, _, _, r in ranked if w == week]
        assert ranks == list(range(1, len(ranks) + 1))


def test_catalog_lists_exactly_the_kpis_gold_computes(con: duckdb.DuckDBPyConnection) -> None:
    computed = set(
        rows(
            con,
            "SELECT DISTINCT kpi_id, formula_version FROM lk.gold.lte_kpi_day "
            "UNION SELECT DISTINCT kpi_id, formula_version FROM lk.gold.gsm_kpi_day",
        )
    )
    listed = set(rows(con, "SELECT kpi_id, formula_version FROM lk.gold.kpi_catalog"))
    assert computed == listed - {("GSM_SDCCH_DROP", 1)}
    quarter_hour = {k for (k,) in rows(con, "SELECT DISTINCT kpi_id FROM lk.gold.lte_kpi_15m")}
    assert "LTE_CQI_MEAN" not in quarter_hour


def test_a_second_run_builds_nothing(lake_file: Path, built: dict[str, Any]) -> None:
    stats = GoldBuild(Target("file", str(lake_file)), "again", REVISION).run()
    assert stats["days"] == 0 and stats["dbt_runs"] == 0


class Finished:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


@pytest.mark.parametrize(
    ("failure", "rerun"),
    [
        (gold.DBT_PARSE_MISS, True),
        ("409 CatalogCommitConflicts", True),
        ("Binder Error: column not found", False),
    ],
)
def test_dbt_runs_again_only_after_a_clock_step(
    monkeypatch: pytest.MonkeyPatch, failure: str, rerun: bool
) -> None:
    outcomes = [Finished(1, f"Compilation Error: {failure}"), Finished(0, "")]
    monkeypatch.setattr("subprocess.run", lambda *a, **k: outcomes.pop(0))
    monkeypatch.setattr(gold, "COMMIT_RETRY_WAIT_S", 0.0)
    build = GoldBuild(Target("file", "unused.duckdb"), "test", REVISION)
    window = gold.window_vars(RUN_START.date(), RUN_START.date())
    if rerun:
        build.dbt(window, {}, "", [])
        assert build.stats["dbt_retries"] == 1 and not outcomes
    else:
        with pytest.raises(RuntimeError, match="column not found"):
            build.dbt(window, {}, "", [])


def test_cells_carry_the_resource_blocks_of_their_bandwidth(
    con: duckdb.DuckDBPyConnection, tiny: NetworkModel
) -> None:
    stored = dict(rows(con, "SELECT cell_name, n_rb FROM lk.gold.cells"))
    for cell, technology, n_rb in zip(
        tiny.world.cells, tiny.state.technology, tiny.state.n_rb, strict=True
    ):
        expected = int(n_rb) if technology == "LTE" else None
        assert stored[cell.cell_name] == expected, cell.cell_name


def factors() -> dict[tuple[str, str], float]:
    table = silver.counter_map().to_pylist()
    return {(r["release"], r["vendor_counter"]): r["factor"] for r in table}


@pytest.mark.parametrize(
    ("kpi_id", "vendor", "technology", "granularity"),
    [
        ("LTE_ERAB_DROP", "huawei", "LTE", "hour"),
        ("LTE_ERAB_DROP", "nokia", "LTE", "day"),
        ("GSM_TCH_BLOCK", "huawei", "GSM", "day"),
    ],
)
def test_lineage_walks_a_value_back_to_its_files(
    con: duckdb.DuckDBPyConnection,
    tiny: NetworkModel,
    kpi_id: str,
    vendor: str,
    technology: str,
    granularity: str,
) -> None:
    cell = next(
        c.cell_name
        for c, v, t in zip(tiny.world.cells, tiny.state.vendor, tiny.state.technology, strict=True)
        if v == vendor and t == technology
    )
    day = RUN_START.date() + timedelta(days=FIRST_DAY + 1)
    period = datetime(day.year, day.month, day.day, 4, tzinfo=UTC) if granularity == "hour" else day
    found = lineage(con, kpi_id, 1, cell, granularity, period)
    assert found
    assert all(r["file_name"] and r["arrival_time"] for r in found)
    assert all(r["bronze_value"] is not None for r in found if r["silver_value"] is not None)
    factor = factors()
    for r in found:
        if r["silver_value"] is not None and not r["derived"]:
            expected = r["bronze_value"] * factor[(r["dictionary_release"], r["bronze_counter"])]
            assert r["silver_value"] == pytest.approx(expected)
    totals: dict[str, float] = {}
    for r in found:
        totals[r["measurement"]] = totals.get(r["measurement"], 0.0) + (r["silver_value"] or 0.0)
    if kpi_id == "LTE_ERAB_DROP":
        value = 100 * totals["ERAB.RelActNbr.sum"] / totals["ERAB.EstabInitSuccNbr.sum"]
    else:
        value = (
            100
            * totals["attTCHSeizuresMeetingTCHBlockedState"]
            / totals["attTCHSeizures + attTCHSeizuresMeetingTCHBlockedState"]
        )
    assert value == pytest.approx(found[0]["kpi_value"])


def test_lineage_follows_a_derived_value_to_both_counters(
    con: duckdb.DuckDBPyConnection, tiny: NetworkModel
) -> None:
    cell = next(
        c.cell_name
        for c, v, t in zip(tiny.world.cells, tiny.state.vendor, tiny.state.technology, strict=True)
        if v == "huawei" and t == "LTE"
    )
    day = RUN_START.date() + timedelta(days=FIRST_DAY + 1)
    found = lineage(
        con, "LTE_PRB_UTIL", 1, cell, "15m", datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
    )
    assert {r["bronze_counter"] for r in found} == {
        "L.ChMeas.PRB.DL.Used.Avg",
        "L.ChMeas.PRB.DL.Avail",
    }
    assert all(r["derived"] for r in found)
