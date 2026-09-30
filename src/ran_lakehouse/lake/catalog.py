"""The lake's Iceberg catalog (rules S1, S3): Lakekeeper over SeaweedFS.

Connects DuckDB (1.5.x, iceberg extension) to a Lakekeeper warehouse on
the local compose stack, creating the warehouse on first use. STS
credential vending lets DuckDB and PyIceberg read and write the SeaweedFS
bucket without keys of their own (results/stack_spike.md).
"""

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import duckdb
from pyiceberg.catalog import load_catalog

CATALOG_URL = "http://127.0.0.1:18181"
S3_ENDPOINT = "http://localhost:8333"
# Placeholders from compose/seaweedfs-iam.json; local stack only.
S3_ACCESS_KEY = "local-dev-key"
S3_SECRET_KEY = "local-dev-secret-not-a-real-credential"
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
CATALOG_ALIAS = "lk"
DUCKDB_MEMORY_LIMIT = "1GB"
# DuckDB spills past its memory limit here (rule 6: generated data in data/).
SPILL_DIR = Path(__file__).resolve().parents[3] / "data" / "duckdb-spill"
# Commit conflicts from a wall-clock step (see write()): wait longer than the
# steps seen on the build machine (about 1.5 s, WSL2 time sync), bounded.
COMMIT_RETRY_WAIT_S = 2.0  # ASSUMPTION
COMMIT_ATTEMPTS = 5  # ASSUMPTION
COMMIT_CONFLICT = "CatalogCommitConflicts"

logger = logging.getLogger(__name__)
commit_retries = {"count": 0}


def management(method: str, path: str, body: dict[str, Any] | None) -> Any:
    """Call the Lakekeeper management API.

    Args:
        method: HTTP method.
        path: Path under the catalog URL.
        body: JSON body, or None.

    Returns:
        Decoded JSON, or None for an empty response.

    Raises:
        RuntimeError: On an error status, with the server's message.
    """
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        CATALOG_URL + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()}") from exc
    return json.loads(raw) if raw else None


def ensure_warehouse(name: str) -> None:
    """Bootstrap Lakekeeper if needed and create the warehouse if missing.

    Args:
        name: Warehouse name, also its key prefix in the bucket.
    """
    if not management("GET", "/management/v1/info", None)["bootstrapped"]:
        management("POST", "/management/v1/bootstrap", {"accept-terms-of-use": True})
    listing = management("GET", "/management/v1/warehouse", None)
    if any(w["name"] == name for w in listing["warehouses"]):
        return
    management(
        "POST",
        "/management/v1/warehouse",
        {
            "warehouse-name": name,
            "project-id": DEFAULT_PROJECT_ID,
            "storage-profile": {
                "type": "s3",
                "bucket": "warehouse",
                "key-prefix": name,
                "endpoint": S3_ENDPOINT,
                "sts-endpoint": S3_ENDPOINT,
                "sts-role-arn": "arn:aws:iam::000000000000:role/LakekeeperVendedRole",
                "region": "local-01",
                "path-style-access": True,
                "flavor": "s3-compat",
                "sts-enabled": True,
            },
            "storage-credential": {
                "type": "s3",
                "credential-type": "access-key",
                "access-key-id": S3_ACCESS_KEY,
                "secret-access-key": S3_SECRET_KEY,
            },
            "delete-profile": {"type": "hard"},
        },
    )


def connect(warehouse: str) -> duckdb.DuckDBPyConnection:
    """DuckDB with the warehouse attached as catalog "lk".

    Args:
        warehouse: Warehouse name (created if missing).

    Returns:
        The connection.
    """
    ensure_warehouse(warehouse)
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
    # Lake timestamps are UTC; keep DuckDB from rendering them in the host zone.
    con.execute("SET TimeZone = 'UTC'")
    # Keep a laptop-scale run well inside memory (ASSUMPTION: 1 GB for DuckDB).
    con.execute(f"SET memory_limit = '{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET temp_directory = '{SPILL_DIR}'")
    # Bronze and silver rows carry their own keys; insertion order is not kept.
    con.execute("SET preserve_insertion_order = false")
    con.execute(
        f"ATTACH '{warehouse}' AS {CATALOG_ALIAS} (TYPE iceberg, "
        f"ENDPOINT '{CATALOG_URL}/catalog', AUTHORIZATION_TYPE 'none')"
    )
    return con


def pyiceberg(warehouse: str) -> Any:
    """The warehouse as a PyIceberg REST catalog (rule S1 second reader).

    Args:
        warehouse: Warehouse name.

    Returns:
        The catalog.
    """
    return load_catalog(
        CATALOG_ALIAS, type="rest", uri=f"{CATALOG_URL}/catalog", warehouse=warehouse
    )


def write(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Run one writing statement, retrying a rejected Iceberg commit.

    DuckDB 1.5.x (iceberg extension 890b78a9c) reads a table as of the
    transaction's start time: when the wall clock steps back (WSL2 time sync
    steps it back by about 1.5 s every 30 s on the build machine), a new
    transaction sees its own last commit as "in the future", builds on the
    snapshot before it, and the catalog rejects the commit with 409
    CatalogCommitConflicts. A rejected commit applies nothing, so the
    statement is run again once the clock has passed the step. Each retry
    is logged and counted; after COMMIT_ATTEMPTS the error is raised.
    Upstream DuckDB adds commit retries after 1.5.x (duckdb-iceberg #1115).

    Args:
        con: DuckDB connection.
        sql: An INSERT, MERGE, UPDATE or DELETE statement.

    Raises:
        duckdb.TransactionException: If the commit is still rejected after
            COMMIT_ATTEMPTS, or fails for another reason.
    """
    for attempt in range(1, COMMIT_ATTEMPTS + 1):
        try:
            con.execute(sql)
            return
        except duckdb.TransactionException as exc:
            if COMMIT_CONFLICT not in str(exc) or attempt == COMMIT_ATTEMPTS:
                raise
            commit_retries["count"] += 1
            logger.warning(
                "commit rejected (%s), attempt %d of %d; retrying in %.1f s: %s",
                COMMIT_CONFLICT,
                attempt,
                COMMIT_ATTEMPTS,
                COMMIT_RETRY_WAIT_S,
                sql.split("\n", 1)[0][:80],
            )
            time.sleep(COMMIT_RETRY_WAIT_S)
