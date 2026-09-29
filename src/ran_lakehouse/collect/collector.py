"""The collector (rules P7, L1): landing to bronze.

Files arrive in landing/<warehouse>/<EMS>/ (a delivery with a name already
in landing replaces it, as a re-push does). The collector picks each file
up as it arrives, hashes it, and loads it into bronze unless the same
file (EMS, name and hash) was loaded before: a redelivery of the same
content is skipped, a same-name file with different content is loaded, and
files of different names are loaded even when their bytes match (an empty
day's log is the same every day). Every arrival is recorded
with its time, name, hash, size and the parser version. Landing keeps the
last RETENTION_DAYS days by arrival time, as an EMS does; bronze keeps
everything.
"""

import gzip
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from ran_lakehouse import __version__
from ran_lakehouse.files import omes, pm_xml
from ran_lakehouse.lake.bronze import BRONZE, append, constant

RETENTION_DAYS = 3  # START (rule P7)
PARSER_VERSION = f"ran-lakehouse {__version__}"
FLUSH_ROWS = 1_000_000  # buffered PM rows per bronze insert (ASSUMPTION)
TS = pa.timestamp("us", tz="UTC")


def encoded(indices: np.ndarray, dictionary: Sequence[str | None]) -> pa.DictionaryArray:
    """A dictionary-encoded string column from indices into a value list.

    Args:
        indices: Index per row.
        dictionary: The values (None allowed).

    Returns:
        The column.
    """
    return pa.DictionaryArray.from_arrays(
        pa.array(indices.astype(np.int32)), pa.array(dictionary, pa.string())
    )


def times(indices: np.ndarray, values: Sequence[datetime]) -> pa.Array:
    """A UTC timestamp column from indices into a list of aware times.

    Args:
        indices: Index per row.
        values: The times.

    Returns:
        The column.
    """
    micros = np.array([round(v.timestamp() * 1_000_000) for v in values], dtype=np.int64)
    return pa.array(micros[indices.astype(np.int64)], TS)


def pm_table(
    content: bytes, ems: str, name: str, digest: str, arrival: datetime, load_id: str
) -> pa.Table:
    """A PM file as bronze.pm_values rows.

    Args:
        content: File bytes (gzip).
        ems: EMS name.
        name: File name.
        digest: SHA-256 of the content.
        arrival: Aware arrival time.
        load_id: Load batch id.

    Returns:
        Rows in bronze.pm_values column order, each with the period of its
        own block (a file carries 15- and 60-minute blocks, rule P1).

    Raises:
        ValueError: If the file name matches no known format.
    """
    if name.startswith("OMeS_"):
        parsed = omes.parse_arrays(content)
        starts = [s["begin"] for s in parsed["setups"]]
        ends = [s["begin"] + timedelta(minutes=s["interval_min"]) for s in parsed["setups"]]
        sizes = parsed["sizes"]
        period_index = np.repeat(parsed["setup_index"], sizes)
        per_result = np.arange(sizes.size)
        dn_index = np.repeat(per_result, sizes)
        elements = ["/".join(dn.split("/")[:2]) for dn in parsed["dns"]]
        values = parsed["values"]
        n = values.size
        element_col = encoded(dn_index, elements)
        dn_col = encoded(dn_index, parsed["dns"])
        group_col = encoded(dn_index, parsed["types"])
        counter_col = pa.array(parsed["tags"], pa.string()).dictionary_encode()
        release_col = encoded(np.zeros(n, dtype=np.int32), [None])
        suspect = np.zeros(n, dtype=bool)
    elif name[:1] in ("A", "B"):
        parsed = pm_xml.parse_blocks(content)
        prefix = parsed["dn_prefix"]
        blocks = parsed["blocks"]
        counts = [b.values.size for b in blocks]
        block_index = np.repeat(np.arange(len(blocks)), counts)
        starts = [b.period_end - timedelta(seconds=b.duration_s) for b in blocks]
        ends = [b.period_end for b in blocks]
        period_index = block_index
        dns: list[str | None] = []
        counter_names: list[str | None] = []
        dn_parts, counter_parts = [], []
        for b in blocks:
            k = len(b.counters)
            dn_parts.append(np.repeat(np.arange(len(b.objects)) + len(dns), k))
            dns.extend(f"{prefix},{b.element},{o}" for o in b.objects)
            counter_parts.append(np.tile(np.arange(k) + len(counter_names), len(b.objects)))
            counter_names.extend(b.counters)
        values = np.concatenate([b.values.ravel() for b in blocks]) if blocks else np.zeros(0)
        n = values.size
        element_col = encoded(block_index, [b.element for b in blocks])
        dn_col = encoded(np.concatenate(dn_parts) if blocks else np.zeros(0), dns)
        group_col = encoded(block_index, [b.meas_info_id for b in blocks])
        counter_col = encoded(
            np.concatenate(counter_parts) if blocks else np.zeros(0), counter_names
        )
        release_col = encoded(block_index, [b.sw_version for b in blocks])
        suspect = (
            np.concatenate([np.repeat(b.suspect, len(b.counters)) for b in blocks])
            if blocks
            else np.zeros(0, dtype=bool)
        )
    else:
        raise ValueError(f"unknown PM file format: {name}")
    return pa.table(
        {
            "period_start": times(period_index, starts),
            "period_end": times(period_index, ends),
            "ems": constant(ems, n, pa.string()),
            "managed_element": element_col,
            "object_dn": dn_col,
            "meas_group": group_col,
            "counter": counter_col,
            "value": pa.array(values, pa.float64(), from_pandas=True),
            "suspect": pa.array(suspect, pa.bool_()),
            "dictionary_release": release_col,
            "file_name": constant(name, n, pa.string()),
            "file_hash": constant(digest, n, pa.string()),
            "arrival_time": constant(arrival.astimezone(UTC), n, TS),
            "parser_version": constant(PARSER_VERSION, n, pa.string()),
            "load_id": constant(load_id, n, pa.string()),
        }
    )


class Collector:
    """Picks up delivered files and loads them into bronze."""

    def __init__(self, con: duckdb.DuckDBPyConnection, landing: Path, load_id: str) -> None:
        """Start a collector.

        Args:
            con: DuckDB with the warehouse attached and the tables created.
            landing: Landing folder of this warehouse.
            load_id: Id of this load run.
        """
        self.con = con
        self.landing = landing
        self.load_id = load_id
        rows = con.execute(
            f"SELECT ems, file_name, file_hash FROM {BRONZE}.file_arrivals WHERE loaded"
        ).fetchall()
        self.loaded: set[tuple[str, str, str]] = {(r[0], r[1], r[2]) for r in rows}
        self.buffers: dict[str, list[pa.Table]] = {}
        self.buffered_rows = 0
        self.arrivals: list[dict[str, object]] = []
        self.stats = {"deliveries": 0, "loaded": 0, "skipped_same_file": 0, "pm_rows": 0}

    def receive(self, ems: str, name: str, content: bytes, arrival: datetime) -> None:
        """A file arrives in landing; land it and load it unless already loaded.

        Args:
            ems: EMS name.
            name: File name.
            content: File bytes.
            arrival: Aware arrival time.
        """
        folder = self.landing / ems
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(content)
        stamp = arrival.timestamp()
        os.utime(path, (stamp, stamp))
        digest = hashlib.sha256(content).hexdigest()
        kind = name.split("_", 1)[0] if name.startswith(("CM", "FM")) else "PM"
        self.stats["deliveries"] += 1
        rows = 0
        key = (ems, name, digest)
        loaded = key not in self.loaded
        if loaded:
            rows = self.load(kind, ems, name, digest, content, arrival)
            self.loaded.add(key)
            self.stats["loaded"] += 1
        else:
            self.stats["skipped_same_file"] += 1
        self.arrivals.append(
            {
                "arrival_time": arrival.astimezone(UTC),
                "ems": ems,
                "kind": kind,
                "file_name": name,
                "file_hash": digest,
                "size_bytes": len(content),
                "loaded": loaded,
                "rows": rows,
                "parser_version": PARSER_VERSION,
                "load_id": self.load_id,
            }
        )
        if self.buffered_rows >= FLUSH_ROWS:
            self.flush()

    def load(
        self, kind: str, ems: str, name: str, digest: str, content: bytes, arrival: datetime
    ) -> int:
        """Parse a file into its bronze buffer.

        Args:
            kind: PM, CM, CMLOG or FM.
            ems: EMS name.
            name: File name.
            digest: Content hash.
            content: File bytes.
            arrival: Aware arrival time.

        Returns:
            Rows parsed.
        """
        if kind == "PM":
            table = pm_table(content, ems, name, digest, arrival, self.load_id)
            target = f"{BRONZE}.pm_values"
            self.stats["pm_rows"] += table.num_rows
        else:
            lines = gzip.decompress(content).decode().splitlines()
            for line in lines:
                json.loads(line)  # fail loudly on a malformed record
            n = len(lines)
            columns = {
                "arrival_time": constant(arrival.astimezone(UTC), n, TS),
                "ems": constant(ems, n, pa.string()),
            }
            if kind != "FM":
                columns["kind"] = constant(kind, n, pa.string())
            columns |= {
                "file_name": constant(name, n, pa.string()),
                "file_hash": constant(digest, n, pa.string()),
                "record": pa.array(lines, pa.string()),
                "load_id": constant(self.load_id, n, pa.string()),
            }
            table = pa.table(columns)
            target = f"{BRONZE}.{'fm_records' if kind == 'FM' else 'cm_records'}"
        self.buffers.setdefault(target, []).append(table)
        self.buffered_rows += table.num_rows
        return int(table.num_rows)

    def flush(self) -> None:
        """Write buffered rows and arrivals to bronze."""
        for target, tables in self.buffers.items():
            append(self.con, target, pa.concat_tables(tables, promote_options="permissive"))
        self.buffers = {}
        self.buffered_rows = 0
        if self.arrivals:
            append(self.con, f"{BRONZE}.file_arrivals", pa.Table.from_pylist(self.arrivals))
            self.arrivals = []

    def retain(self, now: datetime) -> int:
        """Delete landing files that arrived more than RETENTION_DAYS ago.

        Args:
            now: Aware current time.

        Returns:
            Files deleted.
        """
        cutoff = (now - timedelta(days=RETENTION_DAYS)).timestamp()
        removed = 0
        for path in self.landing.glob("*/*"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed
