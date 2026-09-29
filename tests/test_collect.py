"""Delivery, collector and bronze (rules P7, L1, D1-D5) on the tiny profile.

Collector tests run on an in-memory DuckDB catalog named like the lake;
the test marked `lake` runs against the local compose stack.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from ran_lakehouse.collect.backfill import EMS_LIST, drive
from ran_lakehouse.collect.collector import RETENTION_DAYS, Collector, pm_table
from ran_lakehouse.collect.delivery import (
    PER_WEEK,
    DeliveryPlan,
    deliveries,
    plan_delivery,
    release_of,
)
from ran_lakehouse.files.dialects import HUAWEI_R1, HUAWEI_R2, Dialect
from ran_lakehouse.files.ems import WIB, Adjustment, PreparedDay, prepare_day, render_period
from ran_lakehouse.files.pm_xml import ElementData, MeasInfo, file_name, write_file
from ran_lakehouse.lake.bronze import BRONZE, TABLES
from ran_lakehouse.model import RUN_START, NetworkModel, default_model, simulate_days
from ran_lakehouse.world import build_world

HW, NK = EMS_LIST
ARRIVAL = datetime(2026, 1, 5, 11, 20, tzinfo=WIB)


@pytest.fixture(scope="module")
def tiny() -> NetworkModel:
    return default_model(build_world("tiny"))


@pytest.fixture(scope="module")
def plan(tiny: NetworkModel) -> DeliveryPlan:
    return plan_delivery(tiny, EMS_LIST, 42)


@pytest.fixture(scope="module")
def prepared(tiny: NetworkModel) -> dict[str, PreparedDay]:
    day = next(simulate_days(tiny, 0, 1))
    return {ems.ems_id: prepare_day(tiny, day, ems) for ems in EMS_LIST}


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("ATTACH ':memory:' AS lk")
    connection.execute("CREATE SCHEMA lk.bronze")
    connection.execute("CREATE SCHEMA lk.evaluation")
    for table, columns in TABLES.items():
        connection.execute(f"CREATE TABLE {table} {columns}")
    return connection


def base_release(prepared: PreparedDay) -> dict[str, Dialect]:
    return {element: prepared.ems.dialect for element, _ in prepared.elements}


def count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def test_plan_plants_the_weekly_counts(plan: DeliveryPlan) -> None:
    for ems in EMS_LIST:
        for kind, per_week in PER_WEEK.items():
            planted = len(plan.of(kind, ems.ems_id))
            expected = 0 if kind == "D4" and ems is NK else 6 * per_week
            assert planted == expected, (ems.ems_id, kind)
    slots = [(a.ems_id, a.period) for a in plan.anomalies if a.kind != "D5"]
    assert len(slots) == len(set(slots))


def test_plan_is_deterministic(tiny: NetworkModel, plan: DeliveryPlan) -> None:
    assert plan_delivery(tiny, EMS_LIST, 42).anomalies == plan.anomalies


def test_missing_file_is_never_delivered(plan: DeliveryPlan) -> None:
    for period in plan.of("D3", HW.ems_id):
        assert deliveries(plan, 0, HW, period) == []


def test_late_file_arrives_after_its_successors(plan: DeliveryPlan) -> None:
    late_periods = plan.of("D1", NK.ems_id)
    for period in late_periods:
        late = deliveries(plan, 1, NK, period)[0].arrival
        successor = deliveries(plan, 1, NK, period + 1)
        if successor and period + 1 not in late_periods:
            assert late > successor[0].arrival


def test_duplicates_are_delivered_twice(plan: DeliveryPlan) -> None:
    for kind, variant in (("D2_same", False), ("D2_conflict", True)):
        for period in plan.of(kind, HW.ems_id):
            first, second = deliveries(plan, 0, HW, period)
            assert not first.variant and second.variant is variant
            assert second.arrival > first.arrival


def test_upgrade_moves_part_of_the_network_to_r2(plan: DeliveryPlan) -> None:
    assert plan.upgraded
    element = sorted(plan.upgraded)[0]
    before = plan.upgrade_time - timedelta(minutes=15)
    assert release_of(plan, HW, element, before) is HUAWEI_R1
    assert release_of(plan, HW, element, plan.upgrade_time) is HUAWEI_R2
    assert len(plan.of("D5", HW.ems_id)) == 1
    assert sum(a.kind == "D5" for a in plan.anomalies) == len(plan.upgraded)


def test_same_content_loads_once_and_a_conflict_loads_again(
    con: duckdb.DuckDBPyConnection, prepared: dict[str, PreparedDay], tmp_path: Path
) -> None:
    day = prepared[HW.ems_id]
    releases = base_release(day)
    name, content = render_period(day, 40, releases, {})
    element = day.elements[0][0]
    fixed = render_period(day, 40, releases, {element: Adjustment(1.03, False)})[1]
    collector = Collector(con, tmp_path, "test")
    collector.receive(HW.ems_id, name, content, ARRIVAL)
    collector.receive(HW.ems_id, name, content, ARRIVAL + timedelta(minutes=30))
    collector.receive(HW.ems_id, name, fixed, ARRIVAL + timedelta(minutes=60))
    collector.flush()
    assert collector.stats["skipped_same_file"] == 1
    arrivals = con.execute(
        f"SELECT file_name, file_hash, loaded, size_bytes FROM {BRONZE}.file_arrivals "
        "ORDER BY arrival_time"
    ).fetchall()
    assert [a[2] for a in arrivals] == [True, False, True]
    assert arrivals[0][1] == arrivals[1][1] != arrivals[2][1]
    assert {a[0] for a in arrivals} == {name}
    hashes = count(con, f"SELECT count(DISTINCT file_hash) FROM {BRONZE}.pm_values")
    assert hashes == 2
    # A new collector on the same bronze still skips what was loaded.
    again = Collector(con, tmp_path, "test-2")
    again.receive(HW.ems_id, name, content, ARRIVAL + timedelta(days=1))
    assert again.stats["loaded"] == 0
    # Another file with the same bytes (an empty day's log) is still loaded.
    other = render_period(day, 41, releases, {})[0]
    again.receive(HW.ems_id, other, content, ARRIVAL + timedelta(days=1))
    assert again.stats["loaded"] == 1


def test_landing_keeps_three_days(
    con: duckdb.DuckDBPyConnection, prepared: dict[str, PreparedDay], tmp_path: Path
) -> None:
    day = prepared[NK.ems_id]
    collector = Collector(con, tmp_path, "test")
    for p in (0, 1):
        name, content = render_period(day, p, base_release(day), {})
        collector.receive(NK.ems_id, name, content, ARRIVAL + timedelta(hours=p))
    collector.flush()
    rows = count(con, f"SELECT count(*) FROM {BRONZE}.pm_values")
    kept = ARRIVAL + timedelta(days=RETENTION_DAYS, minutes=30)
    assert collector.retain(kept) == 1
    assert len(list(tmp_path.glob("*/*"))) == 1
    assert count(con, f"SELECT count(*) FROM {BRONZE}.pm_values") == rows
    landed = next(tmp_path.glob("*/*"))
    assert os.stat(landed).st_mtime == (ARRIVAL + timedelta(hours=1)).timestamp()


def test_rows_carry_their_block_period(prepared: dict[str, PreparedDay]) -> None:
    for ems_id, hourly in ((HW.ems_id, "LTE.CQI"), (NK.ems_id, "LTE_Quality_DL")):
        day = prepared[ems_id]
        name, content = render_period(day, 43, base_release(day), {})
        rows = pm_table(content, ems_id, name, "h", ARRIVAL, "test").to_pylist()
        end = (RUN_START + timedelta(minutes=15 * 44)).replace(tzinfo=WIB)
        spans = {
            r["meas_group"]: (r["period_start"], r["period_end"])
            for r in rows
            if r["meas_group"] in (hourly, "LTE.Cell", "LTE_Cell_Avail")
        }
        assert spans[hourly] == (end - timedelta(hours=1), end)
        others = [span for group, span in spans.items() if group != hourly]
        assert others and all(span == (end - timedelta(minutes=15), end) for span in others)
        name, content = render_period(day, 42, base_release(day), {})
        groups = {
            r["meas_group"] for r in pm_table(content, ems_id, name, "h", ARRIVAL, "t").to_pylist()
        }
        assert hourly not in groups


def test_suspect_flag_reaches_bronze(prepared: dict[str, PreparedDay]) -> None:
    day = prepared[HW.ems_id]
    element = day.elements[0][0]
    name, content = render_period(
        day,
        40,
        base_release(day),
        {element: Adjustment(0.5, True)},
    )
    rows = pm_table(content, HW.ems_id, name, "h", ARRIVAL, "test").to_pylist()
    flagged = {r["managed_element"] for r in rows if r["suspect"]}
    assert flagged == {f"ManagedElement={element}"}


def test_type_a_file_loads() -> None:
    begin = datetime(2026, 1, 5, 15, 0, tzinfo=WIB)
    end = begin + timedelta(minutes=15)
    info = MeasInfo(
        "LTE.Cell",
        ["L.RRC.ConnReq.Att"],
        ["EUtranCellFDD=X_1"],
        np.array([[7.0]]),
        np.array([False]),
        900,
    )
    content = write_file(
        "SubNetwork=RanLake",
        "ENB0001",
        "test",
        begin,
        end,
        [ElementData("ManagedElement=ENB0001", "R1", [info])],
    )
    name = file_name("A", begin, end, "ENB0001")
    rows = pm_table(content, "EMS-HW-01", name, "h", ARRIVAL, "test").to_pylist()
    assert len(rows) == 1 and rows[0]["value"] == 7.0
    assert rows[0]["period_start"] == begin.astimezone(UTC)


@pytest.mark.lake
def test_backfill_into_iceberg(tmp_path: Path) -> None:
    from ran_lakehouse.lake.catalog import connect, pyiceberg

    warehouse = f"test-{uuid.uuid4().hex[:8]}"
    stats = drive("tiny", 1, 0, 1, warehouse, tmp_path, None)
    con = connect(warehouse)
    rows = count(con, f"SELECT count(*) FROM {BRONZE}.pm_values")
    assert rows == stats["collector"]["pm_rows"] > 0
    table = pyiceberg(warehouse).load_table("bronze.pm_values")
    assert table.scan().to_arrow().num_rows == rows
    with pytest.raises(RuntimeError, match="not empty"):
        drive("tiny", 1, 0, 1, warehouse, tmp_path, None)
