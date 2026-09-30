"""A small in-memory lake for the API tests: every table the API reads,
attached as catalog "lk" like the real warehouse, plus evaluation tables
holding sentinel values that must never reach a response (rule A3).
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import pyarrow as pa

SENTINEL = "EVAL-SENTINEL-4d1f"
HW_ROOT = "SubNetwork=RanLake,SubNetwork=West,ManagedElement=ENB0001,ENBFunction=1"
DN1 = f"{HW_ROOT},EUtranCellFDD=ENB0001_B3_1"
DN2 = f"{HW_ROOT},EUtranCellFDD=ENB0001_B3_2"
NK_DN = "PLMN-PLMN/MRBTS-7/LNBTS-7/LNCEL-1"
GSM_DN = "PLMN-PLMN/BSC-2/BCF-11/BTS-111"
T0 = datetime(2026, 1, 5, 17, 0, tzinfo=UTC)  # 2026-01-06 00:00 WIB
DAY = date(2026, 1, 6)
WEEK = date(2026, 1, 5)
FILE_HASH = "a" * 64
FILE_NAME = "A20260106.0000+0700-0015+0700_EMS-HW-01.xml.gz"
LOAD = "load-fixture"


def cm(dn: str, object_class: str, attributes: dict[str, Any], at: datetime) -> str:
    """A CM snapshot record as the collector stores it.

    Args:
        dn: Object DN.
        object_class: Object class.
        attributes: Attributes.
        at: Snapshot time.

    Returns:
        The JSON record.
    """
    return json.dumps(
        {
            "attributes": attributes,
            "dn": dn,
            "objectClass": object_class,
            "snapshotTime": at.isoformat(),
        }
    )


def cell_attributes(name: str, band: str, bandwidth: float | None) -> dict[str, Any]:
    """CM attributes of a cell.

    Args:
        name: Cell name.
        band: Band.
        bandwidth: Channel bandwidth in MHz (None for GSM).

    Returns:
        Attributes.
    """
    out: dict[str, Any] = {
        "antennaHeightM": 30.0,
        "azimuthDeg": 0.0,
        "band": band,
        "cellIndividualOffsetDb": 0.0,
        "electricalTiltDeg": 6.0,
        "txPowerDbm": 49.0,
        "userLabel": name,
    }
    if bandwidth is not None:
        out["bandwidthMhz"] = bandwidth
    return out


def kpi_rows(
    cell: str, vendor: str, kpi: str, column: str, periods: list[Any]
) -> list[dict[str, Any]]:
    """KPI rows of one cell.

    Args:
        cell: Cell name.
        vendor: Vendor.
        kpi: KPI id.
        column: Period column.
        periods: Periods.

    Returns:
        Rows.
    """
    return [
        {
            "cell_name": cell,
            column: p,
            "vendor": vendor,
            "kpi_id": kpi,
            "formula_version": 1,
            "value": 1.0 + k,
            "numerator": 1.0 + k,
            "denominator": 100.0,
            "periods_expected": 4,
            "periods_reported": 4,
            "coverage": 1.0,
            "suspect_share": 0.0,
        }
        for k, p in enumerate(periods)
    ]


def tables() -> dict[str, list[dict[str, Any]]]:
    """Rows of every fixture table.

    Returns:
        "schema.table" to rows.
    """
    hours = [T0 + timedelta(hours=h) for h in range(3)]
    quarters = [T0 + timedelta(minutes=15 * q) for q in range(4)]
    cells: list[dict[str, Any]] = [
        {
            "ems": "EMS-HW-01",
            "dn": DN1,
            "cell_name": "ENB0001_B3_1",
            "technology": "LTE",
            "n_rb": 100,
        },
        {
            "ems": "EMS-HW-01",
            "dn": DN2,
            "cell_name": "ENB0001_B3_2",
            "technology": "LTE",
            "n_rb": 100,
        },
        {
            "ems": "EMS-NK-01",
            "dn": NK_DN,
            "cell_name": "ENB0007_B8_1",
            "technology": "LTE",
            "n_rb": 50,
        },
        {
            "ems": "EMS-NK-01",
            "dn": GSM_DN,
            "cell_name": "BTS0011_G900_1",
            "technology": "GSM",
            "n_rb": None,
        },
    ]
    snapshot = [
        cm(DN1, "EUtranCellFDD", cell_attributes("ENB0001_B3_1", "B3", 20.0), T0),
        cm(DN2, "EUtranCellFDD", cell_attributes("ENB0001_B3_2", "B3", 20.0), T0),
        cm(
            f"{DN1},EUtranRelation=ENB0001_B3_2",
            "EUtranRelation",
            {"adjacentCell": "ENB0001_B3_2", "userLabel": "ENB0001_B3_1->ENB0001_B3_2"},
            T0,
        ),
    ]
    nokia = [
        cm(NK_DN, "EUtranCellFDD", cell_attributes("ENB0007_B8_1", "B8", 10.0), T0),
        cm(
            f"{NK_DN}/LNREL-1",
            "EUtranRelation",
            {"adjacentCell": "ENB0001_B3_1", "userLabel": "ENB0007_B8_1->ENB0001_B3_1"},
            T0,
        ),
        cm(GSM_DN, "GsmCell", cell_attributes("BTS0011_G900_1", "G900", None), T0),
    ]
    log = [
        json.dumps(
            {
                "attribute": "electricalTiltDeg",
                "dn": DN1,
                "newValue": "4",
                "objectClass": "EUtranCellFDD",
                "oldValue": "6",
                "time": (T0 + timedelta(hours=1)).isoformat(),
            }
        ),
        json.dumps(
            {
                "attribute": "relation",
                "dn": f"{NK_DN}/LNREL-1",
                "newValue": "deleted",
                "objectClass": "EUtranRelation",
                "oldValue": "present",
                "time": (T0 + timedelta(hours=2)).isoformat(),
            }
        ),
    ]

    def cm_row(ems: str, kind: str, record: str) -> dict[str, Any]:
        return {
            "arrival_time": T0,
            "ems": ems,
            "kind": kind,
            "file_name": "CM.json",
            "file_hash": "c" * 64,
            "record": record,
            "load_id": LOAD,
        }

    alarm = {
        "alarmId": "A0001",
        "alarmRaisedTime": (T0 + timedelta(hours=1)).isoformat(),
        "alarmType": "Equipment Alarm",
        "eventTime": (T0 + timedelta(hours=1)).isoformat(),
        "notificationId": "A0001-1",
        "notificationType": "notifyNewAlarm",
        "objectClass": "EUtranCellFDD",
        "objectInstance": DN1,
        "perceivedSeverity": "Major",
        "probableCause": "Transmitter Failure",
        "specificProblem": "cell out of service",
    }
    lte_week = kpi_rows("ENB0001_B3_1", "huawei", "LTE_ERAB_DROP", "week_start", [WEEK])
    lte_week += kpi_rows("ENB0001_B3_2", "huawei", "LTE_ERAB_DROP", "week_start", [WEEK])
    lte_week += kpi_rows("ENB0007_B8_1", "nokia", "LTE_ERAB_DROP", "week_start", [WEEK])
    measurement = {
        "period_start": T0,
        "period_end": T0 + timedelta(minutes=15),
        "granularity_min": 15,
        "ems": "EMS-HW-01",
        "vendor": "huawei",
        "managed_element": "ManagedElement=ENB0001",
        "object_dn": DN1,
        "bin": -1,
        "derived": False,
        "suspect": False,
        "late": False,
        "versions": 1,
        "conflict": False,
        "dictionary_release": "R1",
        "file_name": FILE_NAME,
        "file_hash": FILE_HASH,
        "arrival_time": T0 + timedelta(minutes=20),
        "load_id": LOAD,
    }
    silver_rows = [
        measurement | {"measurement": "ERAB.RelActNbr.sum", "value": 1.0, "vendor_counter": "C1"},
        measurement
        | {"measurement": "ERAB.EstabInitSuccNbr.sum", "value": 100.0, "vendor_counter": "C2"},
    ]
    bronze_rows = [
        {
            "period_start": T0,
            "period_end": T0 + timedelta(minutes=15),
            "ems": "EMS-HW-01",
            "managed_element": "ManagedElement=ENB0001",
            "object_dn": DN1,
            "meas_group": "G",
            "counter": counter,
            "value": value,
            "suspect": False,
            "dictionary_release": "R1",
            "file_name": FILE_NAME,
            "file_hash": FILE_HASH,
            "arrival_time": T0 + timedelta(minutes=20),
            "parser_version": "1",
            "load_id": LOAD,
        }
        for counter, value in (("C1", 1.0), ("C2", 100.0))
    ]
    villages = [
        {
            "village_id": v,
            "x_km": 45.0 + v,
            "y_km": 30.0,
            "population": 1000 * v,
            "schools": v % 2,
            "elevation_m": 300.0,
            "served_lte_rsrp_dbm": None,
            "served_gsm_rxlev_dbm": -95.0,
            "covered_today_lte": False,
            "covered_today_gsm": False,
        }
        for v in (1, 2)
    ]
    sites = [
        {
            "site_id": s,
            "x_km": 45.0 + k,
            "y_km": 31.0,
            "elevation_m": 400.0,
            "nearest_village_km": 1.0,
            "persons_nearby": 3000,
            "build_cost_idr": 2_000_000_000,
            "grid_distance_km": 2.0 + 5 * k,
            "fiber_distance_km": 4.0,
        }
        for k, s in enumerate(("C01", "C02"))
    ]
    coverage = [
        {
            "site_id": s["site_id"],
            "village_id": v["village_id"],
            "technology": t,
            "distance_km": 1.0,
            "hata_db": 120.0,
            "diffraction_db": 0.0,
            "signal_dbm": -90.0,
            "covered": k == j,
        }
        for k, s in enumerate(sites)
        for j, v in enumerate(villages)
        for t in ("LTE", "GSM")
    ]
    costs = {
        "fiber": (300_000_000, 3_000_000),
        "microwave": (350_000_000, 2_000_000),
        "satellite": (150_000_000, 25_000_000),
        "grid_power": (500_000_000, 6_000_000),
        "solar_power": (650_000_000, 1_500_000),
    }
    options = [
        {
            "site_id": s["site_id"],
            "kind": kind,
            "available": True,
            "distance_km": 1.0,
            "capacity_mbps": 100.0,
            "capex_idr": capex,
            "monthly_idr": monthly,
            "detail": "SITE0001" if kind == "microwave" else "",
            "clears_full_fresnel": kind == "microwave",
        }
        for s in sites
        for kind, (capex, monthly) in costs.items()
    ]
    cards = [
        {
            "scenario_id": f"S0{k}",
            "area": "whole",
            "x_min_km": 40.0,
            "x_max_km": 60.0,
            "y_min_km": 0.0,
            "y_max_km": 40.0,
            "request": f"Plan request {k}.",
        }
        for k in (1, 2)
    ]
    return {
        "gold.cells": cells,
        "bronze.cm_records": [cm_row("EMS-HW-01", "CM", r) for r in snapshot]
        + [cm_row("EMS-NK-01", "CM", r) for r in nokia]
        + [cm_row("EMS-HW-01", "CMLOG", log[0]), cm_row("EMS-NK-01", "CMLOG", log[1])],
        "bronze.fm_records": [
            {
                "arrival_time": T0,
                "ems": "EMS-HW-01",
                "file_name": "FM.jsonl.gz",
                "file_hash": "f" * 64,
                "record": json.dumps(alarm),
                "load_id": LOAD,
            }
        ],
        "bronze.file_arrivals": [
            {
                "arrival_time": T0 + timedelta(minutes=20),
                "ems": "EMS-HW-01",
                "kind": "PM",
                "file_name": FILE_NAME,
                "file_hash": FILE_HASH,
                "size_bytes": 1000,
                "loaded": True,
                "rows": 2,
                "parser_version": "1",
                "load_id": LOAD,
            }
        ],
        "bronze.pm_values": bronze_rows,
        "silver.pm_measurements": silver_rows,
        "silver.loads": [
            {
                "load_id": LOAD,
                "kind": "partition",
                "ems": "EMS-HW-01",
                "window_start": T0,
                "window_end": T0 + timedelta(days=1),
                "cutoff": T0 + timedelta(days=1, minutes=30),
                "files": 96,
                "bronze_rows": 2,
                "silver_rows": 2,
                "derived_rows": 0,
                "suspect_rows": 0,
                "conflict_rows": 0,
                "late_rows": 0,
                "gaps": 1,
                "seconds": 1.0,
            }
        ],
        "silver.pm_gaps": [
            {
                "ems": "EMS-HW-01",
                "managed_element": "ManagedElement=ENB0001",
                "period_start": T0 + timedelta(hours=1),
                "period_end": T0 + timedelta(hours=1, minutes=15),
                "granularity_min": 15,
                "load_id": LOAD,
            }
        ],
        "silver.pm_files": [
            {
                "ems": "EMS-HW-01",
                "file_name": FILE_NAME,
                "period_start": T0,
                "period_end": T0 + timedelta(minutes=15),
                "first_arrival": T0 + timedelta(hours=3),
                "deliveries": 2,
                "versions": 2,
                "latest_hash": FILE_HASH,
                "late": True,
                "load_id": LOAD,
            }
        ],
        "silver.counter_map": [
            {
                "vendor": "huawei",
                "release": release,
                "meas_group": "G",
                "vendor_counter": counter,
                "rule": "sum",
                "measurement": "ERAB.RelActNbr.sum",
                "bin": -1,
                "factor": 1.0,
                "attestation": "modelled on public descriptions",
            }
            for release, counter in (("R1", "C1"), ("R2", "C1.Renamed"))
        ],
        "gold.kpi_catalog": [
            {
                "kpi_id": "LTE_ERAB_DROP",
                "formula_version": 1,
                "name": "E-RAB drop rate",
                "technology": "LTE",
                "formula": "100 * ERAB.RelActNbr.sum / ERAB.EstabInitSuccNbr.sum",
                "source": "operator practice",
                "unit": "%",
                "granularities": "15m,hour,day,week",
                "vendors": "huawei,nokia",
                "better": "lower",
                "breach_threshold": 2.0,
                "effective_from": None,
                "operators_differ": None,
            }
        ],
        "gold.lte_kpi_15m": kpi_rows(
            "ENB0001_B3_1", "huawei", "LTE_ERAB_DROP", "period_start", quarters
        ),
        "gold.lte_kpi_hour": kpi_rows(
            "ENB0001_B3_1", "huawei", "LTE_ERAB_DROP", "period_start", hours
        ),
        "gold.lte_kpi_day": kpi_rows("ENB0001_B3_1", "huawei", "LTE_ERAB_DROP", "day", [DAY]),
        "gold.lte_kpi_week": lte_week,
        "gold.gsm_kpi_week": kpi_rows(
            "BTS0011_G900_1", "nokia", "GSM_TCH_BLOCK", "week_start", [WEEK]
        ),
        "gold.worst_cells_week": [
            {
                "week_start": WEEK,
                "kpi_id": "LTE_ERAB_DROP",
                "formula_version": 1,
                "cell_name": "ENB0001_B3_1",
                "days_judged": 7,
                "breach_days": 4,
                "persistence_n": 3,
                "persistence_m": 7,
                "week_value": 2.5,
                "breach_threshold": 2.0,
                "better": "lower",
                "rank": 1,
            }
        ],
        "gold.villages": villages,
        "gold.candidate_sites": sites,
        "gold.coverage": coverage,
        "gold.backhaul_power_options": options,
        "gold.planning_scenarios": cards,
        "evaluation.planning_answers": [
            {
                "scenario_id": "S01",
                "difficulty": SENTINEL,
                "constraints": SENTINEL,
                "sites": SENTINEL,
            }
        ],
        "evaluation.delivery_anomalies": [
            {"kind": SENTINEL, "ems": "EMS-HW-01", "period_start": T0, "detail": SENTINEL}
        ],
        "evaluation.kpi_revisions": [
            {"kpi_id": "LTE_ERAB_DROP", "from_version": 1, "to_version": 2, "detail": SENTINEL}
        ],
    }


def lake() -> duckdb.DuckDBPyConnection:
    """DuckDB with the fixture lake attached as "lk".

    Returns:
        The connection.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("ATTACH ':memory:' AS lk")
    for schema in ("bronze", "silver", "gold", "evaluation"):
        con.execute(f"CREATE SCHEMA lk.{schema}")
    for name, rows in tables().items():
        con.register("fixture_rows", pa.Table.from_pylist(rows))
        con.execute(f"CREATE TABLE lk.{name} AS SELECT * FROM fixture_rows")
        con.unregister("fixture_rows")
    # Tables the API reads that the fixture leaves empty.
    for name, like in (
        ("gold.gsm_kpi_15m", "gold.lte_kpi_15m"),
        ("gold.gsm_kpi_hour", "gold.lte_kpi_hour"),
        ("gold.gsm_kpi_day", "gold.lte_kpi_day"),
    ):
        con.execute(f"CREATE TABLE lk.{name} AS SELECT * FROM lk.{like} WHERE false")
    return con
