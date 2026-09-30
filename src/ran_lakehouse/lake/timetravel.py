"""Time travel (rule D7): what a gold KPI said at an earlier time.

Every gold write is an Iceberg snapshot, so "what did KPI X for cell Y at
period T say as of time S" is a read of the table as of S (DuckDB
`AT (TIMESTAMP => S)`), where S is a wall-clock time of the lake's commits.

The planted case: a late file (rule D1) arrives after its day was built
into silver and published in gold. demonstrate() stages it on the tiny
profile in a fresh warehouse: bronze gets the whole week; silver is built
up to a minute before the late file arrives, and gold is published from
it; then silver merges the late file and gold rebuilds the day. The hour
of the late file's period changes; read as of the first publication it
has its old value, read now its new one.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from ran_lakehouse.collect.backfill import EMS_LIST, anomalies_table, drive_into
from ran_lakehouse.collect.delivery import Anomaly, DeliveryPlan, deliveries, plan_delivery
from ran_lakehouse.lake import bronze, silver
from ran_lakehouse.lake.catalog import connect
from ran_lakehouse.lake.gold import KPI_REVISION, GoldBuild, Target
from ran_lakehouse.model import RUN_START, NetworkModel, default_model
from ran_lakehouse.world import build_world

PROFILE = "tiny"
WEEKS = 6
FIRST_DAY = 35
DAYS = 7
KPI = ("LTE_RRC_SSR", 1)
# Commits after the publication wait this long, so a wall-clock step on
# the build machine (about 1.5 s, lake.catalog.write) cannot order them
# before it (ASSUMPTION).
SETTLE_S = 3.0


@dataclass(frozen=True)
class LateFile:
    """A planted late file that arrives after its silver day was built.

    Attributes:
        anomaly: The planted D1 answer.
        ems_index: Position of its EMS in EMS_LIST.
        period_start: UTC start of the file's 15-minute period.
        arrival: UTC arrival.
    """

    anomaly: Anomaly
    ems_index: int
    period_start: datetime
    arrival: datetime


def late_after_cutoff(plan: DeliveryPlan, first_day: int, days: int) -> LateFile:
    """The first planted late file of some days that misses its silver cutoff.

    Args:
        plan: The delivery plan.
        first_day: First day (WIB day index).
        days: Days.

    Returns:
        The file.

    Raises:
        RuntimeError: If no late file of those days misses its cutoff.
    """
    for index, ems in enumerate(EMS_LIST):
        for period, anomaly in sorted(plan.of("D1", ems.ems_id).items()):
            if not first_day * 96 <= period < (first_day + days) * 96:
                continue
            arrival = deliveries(plan, index, ems, period)[0].arrival
            arrival_utc = (arrival - timedelta(hours=7)).replace(tzinfo=UTC)
            start = (RUN_START + timedelta(minutes=15 * period) - timedelta(hours=7)).replace(
                tzinfo=UTC
            )
            day_end = datetime(start.year, start.month, start.day, tzinfo=UTC) + timedelta(days=1)
            if arrival_utc > day_end + silver.GRACE:
                return LateFile(anomaly, index, start, arrival_utc)
    raise RuntimeError("no planted late file misses its silver cutoff in these days")


def as_of(
    con: duckdb.DuckDBPyConnection, table: str, where: str, at: datetime
) -> list[tuple[Any, ...]]:
    """Rows of a lake table as it was at a time.

    Args:
        con: DuckDB with the lake attached as "lk".
        table: Fully qualified table.
        where: Filter on the table's columns.
        at: Aware wall-clock time.

    Returns:
        The rows.
    """
    return con.execute(
        f"SELECT * FROM (SELECT * FROM {table} AT (TIMESTAMP => TIMESTAMPTZ "
        f"'{at.astimezone(UTC).isoformat()}')) WHERE {where}"
    ).fetchall()


def value_row(rows: list[tuple[Any, ...]], columns: list[str]) -> dict[str, Any]:
    """The one KPI row of a read, as a dict.

    Args:
        rows: Rows read.
        columns: Their column names.

    Returns:
        The row.

    Raises:
        RuntimeError: Unless there is exactly one row.
    """
    if len(rows) != 1:
        raise RuntimeError(f"expected one KPI row, got {len(rows)}")
    return dict(zip(columns, rows[0], strict=True))


def demonstrate(warehouse: str, landing: Path) -> dict[str, Any]:
    """Stage the planted late-file case and read the changed value both ways.

    Args:
        warehouse: A new Lakekeeper warehouse.
        landing: Landing folder for the staged run.

    Returns:
        The late file, the cell and hour, the value and coverage as first
        published and now, the publication time and the snapshot counts.
    """
    model: NetworkModel = default_model(build_world(PROFILE))
    plan = plan_delivery(model, EMS_LIST, 7 * WEEKS)
    late = late_after_cutoff(plan, FIRST_DAY, DAYS)
    ems = EMS_LIST[late.ems_index]
    con = connect(warehouse)
    bronze.create_tables(con)
    bronze.require_empty(con)
    bronze.append(con, f"{bronze.EVALUATION}.delivery_anomalies", anomalies_table(plan))
    drive_into(con, PROFILE, WEEKS, FIRST_DAY, DAYS, landing, None)
    silver.create_tables(con, True)
    silver.SilverBuild(con, "staged", silver.GRACE).run(late.arrival - timedelta(minutes=1))
    target = Target("lake", warehouse)
    GoldBuild(target, "published", KPI_REVISION).run()
    published = datetime.now(UTC)
    time.sleep(SETTLE_S)
    silver.SilverBuild(con, "after-late-file", silver.GRACE).run(None)
    GoldBuild(target, "after-late-file", KPI_REVISION).run()
    cell = next(
        c.cell_name
        for c, vendor, tech in zip(
            model.world.cells, model.state.vendor, model.state.technology, strict=True
        )
        if vendor == ems.dialect.vendor and tech == "LTE"
    )
    hour = late.period_start.replace(minute=0)
    table = "lk.gold.lte_kpi_hour"
    where = (
        f"cell_name = '{cell}' AND kpi_id = '{KPI[0]}' AND formula_version = {KPI[1]} "
        f"AND period_start = TIMESTAMPTZ '{hour.isoformat()}'"
    )
    columns = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    before = value_row(as_of(con, table, where, published), columns)
    now = value_row(con.execute(f"SELECT * FROM {table} WHERE {where}").fetchall(), columns)
    snapshots = con.execute(
        "SELECT count(*) FROM iceberg_snapshots(lk.gold.lte_kpi_hour)"
    ).fetchone()
    con.close()
    return {
        "profile": PROFILE,
        "late_file": {
            "ems": ems.ems_id,
            "period_start": late.period_start.isoformat(),
            "arrival": late.arrival.isoformat(),
            "detail": late.anomaly.detail,
        },
        "cell": cell,
        "kpi": f"{KPI[0]} v{KPI[1]}",
        "hour": hour.isoformat(),
        "published_at": published.isoformat(),
        "as_of_publication": {
            "value": before["value"],
            "coverage": before["coverage"],
            "periods_reported": before["periods_reported"],
        },
        "now": {
            "value": now["value"],
            "coverage": now["coverage"],
            "periods_reported": now["periods_reported"],
        },
        "snapshots_of_lte_kpi_hour": None if snapshots is None else int(snapshots[0]),
    }
