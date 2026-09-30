"""The API's reads of the lake (rule A2): gold first, silver and bronze
where gold has no table yet (CM, alarms, quality events).

Every query names its tables through the catalog "lk" and never the
evaluation schema (rule A3); a test walks every route to prove it.
"""

from datetime import date, datetime, timedelta
from typing import Any

import duckdb

GRANULARITIES = ("15m", "hour", "day", "week")
PERIOD_COLUMN = {"15m": "period_start", "hour": "period_start", "day": "day", "week": "week_start"}
# Largest KPI answer served in one response (ASSUMPTION); a wider query is
# refused and asks for a narrower range.
MAX_KPI_ROWS = 20_000

# Cells with vendor, band and bandwidth from their latest CM snapshot. The
# site is the cell name before its first underscore (rule C1 naming).
CELLS_SQL = """
WITH cm AS (
    SELECT ems,
        json_extract_string(record, '$.dn') AS dn,
        json_extract_string(record, '$.attributes.band') AS band,
        TRY_CAST(json_extract_string(record, '$.attributes.bandwidthMhz') AS DOUBLE)
            AS bandwidth_mhz,
        CAST(json_extract_string(record, '$.snapshotTime') AS TIMESTAMPTZ) AS snapshot_time
    FROM lk.bronze.cm_records
    WHERE kind = 'CM'
        AND json_extract_string(record, '$.objectClass')
            IN ('EUtranCellFDD', 'EUtranCellTDD', 'GsmCell')
),
latest AS (
    SELECT * FROM cm
    QUALIFY row_number() OVER (PARTITION BY ems, dn ORDER BY snapshot_time DESC) = 1
),
vendors AS (
    SELECT DISTINCT cell_name, vendor FROM lk.gold.lte_kpi_week
    UNION SELECT DISTINCT cell_name, vendor FROM lk.gold.gsm_kpi_week
)
SELECT c.cell_name, split_part(c.cell_name, '_', 1) AS site, c.ems, v.vendor, c.technology,
    l.band, l.bandwidth_mhz, c.n_rb, c.dn
FROM lk.gold.cells c
JOIN latest l ON l.ems = c.ems AND l.dn = c.dn
LEFT JOIN vendors v ON v.cell_name = c.cell_name
ORDER BY c.cell_name
"""

RELATIONS_SQL = """
WITH rel AS (
    SELECT ems, json_extract_string(record, '$.attributes.userLabel') AS label,
        CAST(json_extract_string(record, '$.snapshotTime') AS TIMESTAMPTZ) AS snapshot_time
    FROM lk.bronze.cm_records
    WHERE kind = 'CM'
        AND json_extract_string(record, '$.objectClass') IN ('EUtranRelation', 'GsmRelation')
),
latest AS (SELECT ems, max(snapshot_time) AS t FROM rel GROUP BY ems)
SELECT DISTINCT split_part(label, '->', 1) AS source, split_part(label, '->', 2) AS target
FROM rel JOIN latest USING (ems)
WHERE rel.snapshot_time = latest.t
ORDER BY source, target
"""

# Object records of one cell (the cell and its relations) in the latest
# snapshot at or before a time.
CM_SNAPSHOT_SQL = """
WITH c AS (SELECT ems, dn FROM lk.gold.cells WHERE cell_name = $cell),
rec AS (
    SELECT r.ems, r.record,
        CAST(json_extract_string(r.record, '$.snapshotTime') AS TIMESTAMPTZ) AS snapshot_time
    FROM lk.bronze.cm_records r JOIN c ON r.ems = c.ems
    WHERE r.kind = 'CM'
        AND (json_extract_string(r.record, '$.dn') = c.dn
            OR starts_with(json_extract_string(r.record, '$.dn'), c.dn || ',')
            OR starts_with(json_extract_string(r.record, '$.dn'), c.dn || '/'))
),
latest AS (SELECT max(snapshot_time) AS t FROM rec WHERE snapshot_time <= $at)
SELECT DISTINCT rec.snapshot_time, json_extract_string(rec.record, '$.objectClass') AS object_class,
    json_extract_string(rec.record, '$.dn') AS dn,
    json_extract(rec.record, '$.attributes') AS attributes
FROM rec, latest WHERE rec.snapshot_time = latest.t
ORDER BY object_class, dn
"""

CM_CHANGES_SQL = """
WITH log AS (
    SELECT ems,
        CAST(json_extract_string(record, '$.time') AS TIMESTAMPTZ) AS time,
        json_extract_string(record, '$.objectClass') AS object_class,
        json_extract_string(record, '$.dn') AS dn,
        json_extract_string(record, '$.attribute') AS attribute,
        json_extract_string(record, '$.oldValue') AS old_value,
        json_extract_string(record, '$.newValue') AS new_value
    FROM lk.bronze.cm_records WHERE kind = 'CMLOG'
)
SELECT DISTINCT log.time, c.cell_name, log.ems, log.object_class, log.dn, log.attribute,
    log.old_value, log.new_value
FROM log JOIN lk.gold.cells c
    ON c.ems = log.ems
    AND (log.dn = c.dn OR starts_with(log.dn, c.dn || ',') OR starts_with(log.dn, c.dn || '/'))
WHERE log.time >= $start AND log.time < $end AND ($cell IS NULL OR c.cell_name = $cell)
ORDER BY log.time, log.dn, log.attribute
"""

ALARMS_SQL = """
WITH n AS (
    SELECT ems,
        json_extract_string(record, '$.notificationId') AS notification_id,
        json_extract_string(record, '$.notificationType') AS notification_type,
        json_extract_string(record, '$.alarmId') AS alarm_id,
        CAST(json_extract_string(record, '$.eventTime') AS TIMESTAMPTZ) AS event_time,
        CAST(json_extract_string(record, '$.alarmRaisedTime') AS TIMESTAMPTZ) AS raised_time,
        CAST(json_extract_string(record, '$.alarmClearedTime') AS TIMESTAMPTZ) AS cleared_time,
        json_extract_string(record, '$.alarmType') AS alarm_type,
        json_extract_string(record, '$.perceivedSeverity') AS perceived_severity,
        json_extract_string(record, '$.probableCause') AS probable_cause,
        json_extract_string(record, '$.specificProblem') AS specific_problem,
        json_extract_string(record, '$.objectInstance') AS object_instance
    FROM lk.bronze.fm_records
)
SELECT DISTINCT n.notification_id, n.notification_type, n.alarm_id, n.event_time,
    n.raised_time, n.cleared_time, n.alarm_type, n.perceived_severity, n.probable_cause,
    n.specific_problem, c.cell_name, n.ems, n.object_instance
FROM n LEFT JOIN lk.gold.cells c ON c.ems = n.ems AND c.dn = n.object_instance
WHERE n.event_time >= $start AND n.event_time < $end AND ($cell IS NULL OR c.cell_name = $cell)
ORDER BY n.event_time, n.notification_id
"""

# What the pipeline itself saw go wrong in delivery (rule D): missing
# periods, files that came late, and files delivered more than once or
# changed on redelivery.
QUALITY_SQL = """
SELECT 'missing_period' AS kind, ems, managed_element, period_start, period_end,
    NULL::VARCHAR AS file_name, granularity_min || '-minute period with no file' AS detail
FROM lk.silver.pm_gaps
WHERE period_start >= $start AND period_start < $end
UNION ALL
SELECT 'late_file', ems, NULL, period_start, period_end, file_name,
    'first arrived ' || strftime(first_arrival, '%Y-%m-%dT%H:%M:%SZ')
FROM lk.silver.pm_files
WHERE late AND period_start >= $start AND period_start < $end
UNION ALL
SELECT CASE WHEN versions > 1 THEN 'changed_redelivery' ELSE 'duplicate_delivery' END,
    ems, NULL, period_start, period_end, file_name,
    deliveries || ' deliveries, ' || versions || ' distinct versions'
FROM lk.silver.pm_files
WHERE deliveries > 1 AND period_start >= $start AND period_start < $end
ORDER BY period_start, kind, ems, managed_element, file_name
"""


def rows(con: duckdb.DuckDBPyConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a query and return its rows as dicts.

    Args:
        con: DuckDB with the lake as "lk".
        sql: Query.
        params: Named parameters.

    Returns:
        Rows.
    """
    result = con.execute(sql, params)
    names = [d[0] for d in result.description]
    return [dict(zip(names, row, strict=True)) for row in result.fetchall()]


def cells(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Every cell with vendor, band, bandwidth, N_RB and DN.

    Args:
        con: DuckDB with the lake as "lk".

    Returns:
        Rows, by cell name.
    """
    return rows(con, CELLS_SQL, {})


def relations(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Configured neighbour relations in each EMS's latest CM snapshot.

    Args:
        con: DuckDB with the lake as "lk".

    Returns:
        (source, target) rows.
    """
    return rows(con, RELATIONS_SQL, {})


def kpi_catalog(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """The KPI catalog (rule L3).

    Args:
        con: DuckDB with the lake as "lk".

    Returns:
        Rows.
    """
    return rows(con, "SELECT * FROM lk.gold.kpi_catalog ORDER BY kpi_id, formula_version", {})


def kpis(
    con: duckdb.DuckDBPyConnection,
    technology: str,
    granularity: str,
    cell: str,
    kpi_id: str,
    version: int,
    start: datetime | date,
    end: datetime | date,
) -> list[dict[str, Any]]:
    """KPI values of one cell over a range.

    Args:
        con: DuckDB with the lake as "lk".
        technology: "lte" or "gsm".
        granularity: One of GRANULARITIES.
        cell: Cell name.
        kpi_id: KPI id.
        version: Formula version.
        start: First period (UTC datetime for 15m and hour, WIB date for
            day and week), inclusive.
        end: Last period, exclusive.

    Returns:
        Rows by period, at most MAX_KPI_ROWS + 1 (the caller refuses more).
    """
    column = PERIOD_COLUMN[granularity]
    sql = f"""
        SELECT {column} AS period, vendor, value, numerator, denominator, periods_expected,
            periods_reported, coverage, suspect_share
        FROM lk.gold.{technology}_kpi_{granularity}
        WHERE cell_name = $cell AND kpi_id = $kpi AND formula_version = $version
            AND {column} >= $start AND {column} < $end
        ORDER BY {column}
        LIMIT {MAX_KPI_ROWS + 1}
    """
    params = {"cell": cell, "kpi": kpi_id, "version": version, "start": start, "end": end}
    return rows(con, sql, params)


def worst_cells(
    con: duckdb.DuckDBPyConnection, week_start: date, kpi_id: str, version: int
) -> list[dict[str, Any]]:
    """The worst-cell ranking of one week and KPI.

    Args:
        con: DuckDB with the lake as "lk".
        week_start: Monday (WIB date).
        kpi_id: KPI id.
        version: Formula version.

    Returns:
        Rows by rank.
    """
    sql = """
        SELECT rank, cell_name, week_value, breach_threshold, better, days_judged,
            breach_days, persistence_n, persistence_m
        FROM lk.gold.worst_cells_week
        WHERE week_start = $week AND kpi_id = $kpi AND formula_version = $version
        ORDER BY rank
    """
    return rows(con, sql, {"week": week_start, "kpi": kpi_id, "version": version})


def cm_snapshot(con: duckdb.DuckDBPyConnection, cell: str, at: datetime) -> list[dict[str, Any]]:
    """One cell's CM records in the latest snapshot at or before a time.

    Args:
        con: DuckDB with the lake as "lk".
        cell: Cell name.
        at: Aware time.

    Returns:
        The cell's record and its relations' records.
    """
    return rows(con, CM_SNAPSHOT_SQL, {"cell": cell, "at": at})


def cm_changes(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime, cell: str | None
) -> list[dict[str, Any]]:
    """CM change log entries in a time range.

    Args:
        con: DuckDB with the lake as "lk".
        start: Aware start, inclusive.
        end: Aware end, exclusive.
        cell: Only this cell (and its relations), or None for all.

    Returns:
        Rows by time.
    """
    return rows(con, CM_CHANGES_SQL, {"start": start, "end": end, "cell": cell})


def alarms(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime, cell: str | None
) -> list[dict[str, Any]]:
    """Alarm notifications in a time range.

    Args:
        con: DuckDB with the lake as "lk".
        start: Aware start, inclusive.
        end: Aware end, exclusive.
        cell: Only this cell, or None for all.

    Returns:
        Rows by event time.
    """
    return rows(con, ALARMS_SQL, {"start": start, "end": end, "cell": cell})


def quality_events(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Delivery problems the pipeline recorded in a time range.

    Args:
        con: DuckDB with the lake as "lk".
        start: Aware start of the periods, inclusive.
        end: Aware end, exclusive.

    Returns:
        Rows by period.
    """
    return rows(con, QUALITY_SQL, {"start": start, "end": end})


def lake_clock(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """How far each layer has got.

    Args:
        con: DuckDB with the lake as "lk".

    Returns:
        Latest bronze arrival, silver window end and gold day (WIB).
    """
    return {
        "bronze_latest_arrival": con.execute(
            "SELECT max(arrival_time) FROM lk.bronze.file_arrivals"
        ).fetchone()[0],  # type: ignore[index]
        "silver_complete_to": con.execute("SELECT max(window_end) FROM lk.silver.loads").fetchone()[
            0
        ],  # type: ignore[index]
        "gold_latest_day": con.execute("SELECT max(day) FROM lk.gold.lte_kpi_day").fetchone()[0],  # type: ignore[index]
    }


def planning_table(con: duckdb.DuckDBPyConnection, name: str) -> list[dict[str, Any]]:
    """One gold planning table (rule G7) or the public scenario cards.

    Args:
        con: DuckDB with the lake as "lk".
        name: villages, candidate_sites, coverage, backhaul_power_options or
            planning_scenarios.

    Returns:
        Rows in key order.

    Raises:
        ValueError: For any other name.
    """
    keys = {
        "villages": "village_id",
        "candidate_sites": "site_id",
        "coverage": "site_id, village_id, technology",
        "backhaul_power_options": "site_id, kind",
        "planning_scenarios": "scenario_id",
    }
    if name not in keys:
        raise ValueError(f"no planning table {name}")
    return rows(con, f"SELECT * FROM lk.gold.{name} ORDER BY {keys[name]}", {})


STATUS_EVENTS = 20  # quality events shown on the status page (START)
# Row counts shown on the status page, per layer.
LAYER_TABLES = (
    ("bronze", "file_arrivals"),
    ("bronze", "pm_values"),
    ("bronze", "cm_records"),
    ("bronze", "fm_records"),
    ("silver", "pm_measurements"),
    ("silver", "pm_files"),
    ("silver", "pm_gaps"),
    ("gold", "lte_kpi_15m"),
    ("gold", "lte_kpi_day"),
    ("gold", "gsm_kpi_15m"),
    ("gold", "gsm_kpi_day"),
    ("gold", "worst_cells_week"),
)

FILES_SQL = """
WITH latest AS (SELECT max(arrival_time) AS t FROM lk.bronze.file_arrivals)
SELECT ems, kind, count(*) AS files,
    count(*) FILTER (WHERE arrival_time > latest.t - INTERVAL 1 DAY) AS files_last_day,
    count(*) FILTER (WHERE NOT loaded) AS not_loaded,
    max(arrival_time) AS latest_arrival
FROM lk.bronze.file_arrivals, latest
GROUP BY ems, kind
ORDER BY ems, kind
"""

# The planted data-quality cases (rule D) as the pipeline flagged them:
# what silver and gold recorded, by kind and count only.
D_CASES_SQL = """
SELECT 'D1' AS rule, 'late files' AS kind, count(*) AS count FROM lk.silver.pm_files WHERE late
UNION ALL SELECT 'D1', 'late rows merged', coalesce(sum(late_rows), 0) FROM lk.silver.loads
UNION ALL SELECT 'D2', 'files delivered twice, same content', count(*)
    FROM lk.silver.pm_files WHERE deliveries > 1 AND versions = 1
UNION ALL SELECT 'D2', 'files redelivered with changed content', count(*)
    FROM lk.silver.pm_files WHERE versions > 1
UNION ALL SELECT 'D2', 'conflicting rows flagged', coalesce(sum(conflict_rows), 0)
    FROM lk.silver.loads
UNION ALL SELECT 'D3', 'missing periods per element', count(*) FROM lk.silver.pm_gaps
UNION ALL SELECT 'D4', 'suspect rows carried', coalesce(sum(suspect_rows), 0) FROM lk.silver.loads
UNION ALL SELECT 'D5', 'counters mapped across a rename', count(*) FROM (
    SELECT vendor, measurement, bin FROM lk.silver.counter_map
    GROUP BY ALL HAVING count(DISTINCT vendor_counter) > 1)
UNION ALL SELECT 'D6', 'KPIs with more than one formula version', count(*) FROM (
    SELECT kpi_id FROM lk.gold.kpi_catalog GROUP BY kpi_id HAVING count(*) > 1)
"""


def status(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """What the pipeline has taken in and what it flagged.

    Args:
        con: DuckDB with the lake as "lk".

    Returns:
        Files per EMS and kind, rows per layer table, the flagged D cases
        and the latest day's quality events.
    """
    layers = [
        {
            "layer": schema,
            "table": table,
            "rows": con.execute(f"SELECT count(*) FROM lk.{schema}.{table}").fetchone()[0],  # type: ignore[index]
        }
        for schema, table in LAYER_TABLES
    ]
    end = con.execute("SELECT max(period_end) FROM lk.silver.pm_files").fetchone()[0]  # type: ignore[index]
    events = [] if end is None else quality_events(con, end - timedelta(days=1), end)
    return {
        "files": rows(con, FILES_SQL, {}),
        "layers": layers,
        "d_cases": rows(con, D_CASES_SQL, {}),
        "latest_quality_events": events[-STATUS_EVENTS:],
    }
