"""Silver (rules L2, D1 to D5) on the tiny profile, in an in-memory catalog.

One week of the run (days 35 to 41, the D5 upgrade on day 37) is
delivered into bronze and built into silver; every planted delivery
anomaly must come out flagged exactly where it was planted.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from ran_lakehouse.collect.backfill import EMS_LIST, anomalies_table, drive_into
from ran_lakehouse.collect.delivery import DeliveryPlan, plan_delivery
from ran_lakehouse.files.ems import WIB, nokia_dn
from ran_lakehouse.lake import bronze, silver
from ran_lakehouse.lake.silver import GRACE, MEASUREMENTS, SilverBuild, counter_map
from ran_lakehouse.lake.silver_eval import KINDS, evaluate
from ran_lakehouse.model import NetworkModel, default_model, simulate_days
from ran_lakehouse.world import build_world

WEEKS = 6
FIRST_DAY = 35
DAYS = 7
HW, NK = EMS_LIST


def memory_lake() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("ATTACH ':memory:' AS lk")
    con.execute("CREATE SCHEMA lk.bronze")
    con.execute("CREATE SCHEMA lk.evaluation")
    for table, columns in bronze.TABLES.items():
        con.execute(f"CREATE TABLE {table} {columns}")
    return con


def copy_lake(source: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    con = memory_lake()
    for table in bronze.TABLES:
        data = source.execute(f"SELECT * FROM {table}").to_arrow_table()
        con.register("rows_in", data)
        con.execute(f"INSERT INTO {table} SELECT * FROM rows_in")
        con.unregister("rows_in")
    return con


def count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


@pytest.fixture(scope="module")
def plan(tiny: NetworkModel) -> DeliveryPlan:
    return plan_delivery(tiny, EMS_LIST, 7 * WEEKS)


@pytest.fixture(scope="module")
def bronze_week(
    tiny: NetworkModel, plan: DeliveryPlan, tmp_path_factory: pytest.TempPathFactory
) -> duckdb.DuckDBPyConnection:
    con = memory_lake()
    bronze.append(con, f"{bronze.EVALUATION}.delivery_anomalies", anomalies_table(plan))
    landing = tmp_path_factory.mktemp("landing")
    drive_into(con, "tiny", WEEKS, FIRST_DAY, DAYS, landing, None)
    return con


@pytest.fixture(scope="module")
def lake(bronze_week: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    con = copy_lake(bronze_week)
    silver.create_tables(con, False)
    SilverBuild(con, "test", GRACE).run(None)
    yield con


def test_every_vendor_counter_maps_once() -> None:
    table = counter_map().to_pylist()
    keys = [(r["release"], r["meas_group"], r["vendor_counter"]) for r in table]
    assert len(keys) == len(set(keys))
    factor = {(r["release"], r["vendor_counter"]): r["factor"] for r in table}
    assert factor[("HW-R1", "L.Thrp.bits.DL")] == pytest.approx(0.001)  # bit to kbit
    assert factor[("HW-R2", "L.Thrp.bits.DL.Total")] == pytest.approx(0.001)
    assert factor[("NK-R1", "M8012C20")] == pytest.approx(8.0)  # kByte to kbit


@pytest.mark.parametrize("kind", KINDS)
def test_planted_anomaly_is_flagged_where_planted(
    lake: duckdb.DuckDBPyConnection, tiny: NetworkModel, plan: DeliveryPlan, kind: str
) -> None:
    outcome = evaluate(lake, tiny, plan)[kind]
    assert outcome.planted, f"no {kind} planted in the test week"
    assert outcome.found == outcome.planted


def test_units_match_the_model(lake: duckdb.DuckDBPyConnection, tiny: NetworkModel) -> None:
    day = next(simulate_days(tiny, FIRST_DAY + 1, 1))
    p = 40
    start = day.starts[p].replace(tzinfo=WIB).astimezone(UTC)
    got = {
        (dn, m): v
        for dn, m, v in lake.execute(
            f"SELECT object_dn, measurement, value FROM {MEASUREMENTS} "
            "WHERE period_start = ? AND granularity_min = 15 AND measurement IN "
            "('DRB.IPVolDl.sum', 'RRU.PrbTotDl', 'RRU.CellUnavailableTime.sum')",
            [start],
        ).fetchall()
    }
    lte = day.lte
    checked = set()
    for column, cell in enumerate(lte.cells):
        vendor = tiny.state.vendor[cell]
        dn = tiny.world.cells[int(cell)].dn if vendor == "huawei" else nokia_dn(tiny, int(cell))
        if (dn, "DRB.IPVolDl.sum") not in got:
            continue  # the file of this period was not delivered (rule D3)
        values = lte.values
        assert got[(dn, "DRB.IPVolDl.sum")] == pytest.approx(values["DRB.IPVolDl.sum"][p, column])
        assert got[(dn, "RRU.PrbTotDl")] == values["RRU.PrbTotDl"][p, column]
        unavailable = values["RRU.CellUnavailableTime.sum"][p, column]
        # Nokia-style availability is sampled every 10 s (rule P4 ASSUMPTION).
        assert abs(got[(dn, "RRU.CellUnavailableTime.sum")] - unavailable) <= 5.0 + 1e-9
        checked.add(vendor)
    assert checked == {"huawei", "nokia"}


def test_gaps_are_never_zero_filled(lake: duckdb.DuckDBPyConnection) -> None:
    assert count(lake, f"SELECT count(*) FROM {silver.GAPS}") > 0
    filled = count(
        lake,
        f"SELECT count(*) FROM {silver.GAPS} g JOIN {MEASUREMENTS} m "
        "ON m.ems = g.ems AND m.managed_element = g.managed_element "
        "AND m.period_start = g.period_start AND m.granularity_min = g.granularity_min",
    )
    assert filled == 0


def test_hourly_and_quarter_hour_rows_coexist(lake: duckdb.DuckDBPyConnection) -> None:
    granularities = dict(
        lake.execute(
            f"SELECT granularity_min, count(DISTINCT measurement) FROM {MEASUREMENTS} GROUP BY 1"
        ).fetchall()
    )
    assert set(granularities) == {15, 60}
    hourly = lake.execute(
        f"SELECT DISTINCT measurement FROM {MEASUREMENTS} WHERE granularity_min = 60"
    ).fetchall()
    assert {m for (m,) in hourly} == {"CARR.WBCQIDist.Bin", "TA distance bins (vendor-style)"}


def test_a_second_run_changes_nothing(lake: duckdb.DuckDBPyConnection) -> None:
    before = count(lake, f"SELECT count(*) FROM {MEASUREMENTS}")
    stats = SilverBuild(lake, "again", GRACE).run(None)
    assert stats["partitions"] == 0 and stats["catch_up_files"] == 0
    assert count(lake, f"SELECT count(*) FROM {MEASUREMENTS}") == before


def test_late_file_merges_into_its_own_hour(bronze_week: duckdb.DuckDBPyConnection) -> None:
    con = copy_lake(bronze_week)
    name, period_start = con.execute(
        f"SELECT file_name, min(period_start) AS p FROM {bronze.BRONZE}.pm_values "
        f"WHERE ems = '{HW.ems_id}' AND period_start >= TIMESTAMPTZ '2026-02-11 10:00:00+00' "
        "AND period_end - period_start = INTERVAL 15 MINUTE "
        "GROUP BY file_name HAVING date_part('minute', p) = 0 ORDER BY p LIMIT 1"
    ).fetchone() or ("", None)
    assert isinstance(period_start, datetime) and period_start.minute == 0
    day_end = datetime(period_start.year, period_start.month, period_start.day, tzinfo=UTC)
    day_end += timedelta(days=1)
    arrival = day_end + GRACE + timedelta(hours=2)
    for table in ("pm_values", "file_arrivals"):
        con.execute(
            f"UPDATE {bronze.BRONZE}.{table} SET arrival_time = ? WHERE file_name = ?",
            [arrival, name],
        )
    silver.create_tables(con, False)
    SilverBuild(con, "first", GRACE).run(day_end + GRACE + timedelta(hours=1))
    missing = (
        f"ems = '{HW.ems_id}' AND granularity_min = 15 "
        f"AND period_start = TIMESTAMPTZ '{period_start.isoformat()}'"
    )
    assert count(con, f"SELECT count(*) FROM {MEASUREMENTS} WHERE {missing}") == 0
    assert count(con, f"SELECT count(*) FROM {silver.GAPS} WHERE {missing}") > 0
    stats = SilverBuild(con, "second", GRACE).run(None)
    assert stats["catch_up_files"] >= 1
    window = con.execute(
        f"SELECT window_start, window_end FROM {silver.LOADS} "
        f"WHERE kind = 'catch-up' AND load_id = 'second' AND ems = '{HW.ems_id}' "
        "AND window_start <= ? AND window_end > ?",
        [period_start, period_start],
    ).fetchall()
    assert window == [(period_start, period_start + timedelta(minutes=15))]
    rows = con.execute(
        f"SELECT count(*), bool_and(late) FROM {MEASUREMENTS} WHERE {missing}"
    ).fetchone()
    assert rows is not None and rows[0] > 0 and rows[1]
    assert count(con, f"SELECT count(*) FROM {silver.GAPS} WHERE {missing}") == 0
    # Merging late equals building with the file present from the start.
    fresh = copy_lake(con)
    silver.create_tables(fresh, False)
    SilverBuild(fresh, "fresh", GRACE).run(None)
    columns = "* EXCLUDE (load_id)"
    merged = con.execute(f"SELECT {columns} FROM {MEASUREMENTS} ORDER BY ALL").fetchall()
    rebuilt = fresh.execute(f"SELECT {columns} FROM {MEASUREMENTS} ORDER BY ALL").fetchall()
    assert merged == rebuilt
