"""Lineage (rule D8): one query from a gold KPI value to its sources.

For one KPI value (KPI, formula version, cell, granularity, period) the
query returns every silver row the value was computed from (3GPP
measurement, value, dictionary release, vendor counter and the D-case
flags: derived, suspect, late, conflict, versions), joined to the bronze
row each came from (vendor counter, raw value, network element, parser
version, load) and the file that carried it (name, hash, arrival, size).
A derived silver row (lake.silver.derive) joins to each vendor counter it
was derived from. DuckDB's Iceberg catalog has no views, so the query is
this module's SQL, run by `ranlake lineage`.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb

from ran_lakehouse.lake.kpi_catalog import HOURLY_ONLY, INPUTS

WIB = timedelta(hours=7)  # rule W5
PERIOD_COLUMN = {"15m": "period_start", "hour": "period_start", "day": "day", "week": "week_start"}
SPAN = {
    "15m": timedelta(minutes=15),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
}

LINEAGE_SQL = """
WITH k AS (
    SELECT value, coverage, suspect_share, periods_reported, periods_expected
    FROM lk.gold.{technology}_kpi_{granularity}
    WHERE cell_name = $cell AND kpi_id = $kpi AND formula_version = $version
        AND {period_column} = $period
),
c AS (SELECT ems, dn FROM lk.gold.cells WHERE cell_name = $cell),
s AS (
    SELECT m.*
    FROM lk.silver.pm_measurements m JOIN c ON m.ems = c.ems
        AND (m.object_dn = c.dn OR starts_with(m.object_dn, c.dn || ',')
            OR starts_with(m.object_dn, c.dn || '/'))
    WHERE m.period_start >= $lo AND m.period_start < $hi
        AND m.granularity_min = $granularity_min AND list_contains($measurements, m.measurement)
),
b AS (SELECT s.*, unnest(string_split(s.vendor_counter, ' / ')) AS bronze_counter FROM s)
SELECT
    k.value AS kpi_value, k.coverage, k.suspect_share,
    b.period_start, b.object_dn, b.measurement, b.bin, b.value AS silver_value,
    b.derived, b.suspect, b.late, b.conflict, b.versions, b.dictionary_release,
    b.bronze_counter, p.value AS bronze_value, p.managed_element, p.parser_version, p.load_id,
    f.file_name, f.file_hash, f.arrival_time, f.size_bytes
FROM b CROSS JOIN k
LEFT JOIN lk.bronze.pm_values p ON p.ems = b.ems AND p.file_hash = b.file_hash
    AND p.object_dn = b.object_dn AND p.counter = b.bronze_counter
    AND p.period_start = b.period_start AND p.period_end = b.period_end
    AND p.period_start >= $lo AND p.period_start < $hi
LEFT JOIN lk.bronze.file_arrivals f ON f.file_hash = p.file_hash AND f.loaded
ORDER BY b.period_start, b.object_dn, b.measurement, b.bin, b.bronze_counter
"""


def window(granularity: str, period: datetime | date) -> tuple[datetime, datetime]:
    """The UTC window of silver periods a KPI value covers.

    Args:
        granularity: "15m", "hour", "day" or "week".
        period: The value's period: an aware UTC start for 15m and hour, a
            WIB date (a Monday for week) otherwise.

    Returns:
        (start, end) in UTC.

    Raises:
        ValueError: For an unknown granularity or a period of the wrong type.
    """
    if granularity not in SPAN:
        raise ValueError(f"unknown granularity {granularity}")
    if granularity in ("15m", "hour"):
        if not isinstance(period, datetime):
            raise ValueError(f"{granularity} periods are UTC datetimes")
        return period, period + SPAN[granularity]
    if isinstance(period, datetime):
        raise ValueError(f"{granularity} periods are WIB dates")
    start = datetime(period.year, period.month, period.day, tzinfo=UTC) - WIB
    return start, start + SPAN[granularity]


def lineage(
    con: duckdb.DuckDBPyConnection,
    kpi_id: str,
    version: int,
    cell: str,
    granularity: str,
    period: datetime | date,
) -> list[dict[str, Any]]:
    """The lineage of one gold KPI value.

    Args:
        con: DuckDB with bronze, silver and gold in catalog "lk".
        kpi_id: KPI id.
        version: Formula version.
        cell: Cell name.
        granularity: "15m", "hour", "day" or "week".
        period: The value's period (see window()).

    Returns:
        One row per silver row and source counter, with the KPI value on
        every row; empty when gold has no such value.

    Raises:
        ValueError: For a KPI version with no recorded inputs, or a
            granularity the KPI does not have.
    """
    if (kpi_id, version) not in INPUTS:
        raise ValueError(f"no inputs recorded for {kpi_id} v{version}")
    hourly = kpi_id == "LTE_CQI_MEAN"
    if hourly and granularity not in HOURLY_ONLY:
        raise ValueError(f"{kpi_id} has no {granularity} values")
    lo, hi = window(granularity, period)
    sql = LINEAGE_SQL.format(
        technology=kpi_id.split("_")[0].lower(),
        granularity=granularity,
        period_column=PERIOD_COLUMN[granularity],
    )
    result = con.execute(
        sql,
        {
            "cell": cell,
            "kpi": kpi_id,
            "version": version,
            "period": period,
            "lo": lo,
            "hi": hi,
            "granularity_min": 60 if hourly else 15,
            "measurements": list(INPUTS[(kpi_id, version)]),
        },
    )
    names = [d[0] for d in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]
