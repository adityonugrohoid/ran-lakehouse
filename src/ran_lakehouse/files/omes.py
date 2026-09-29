"""Nokia-style PM files (rule P4): an OMeS-shaped XML format, modelled on
public descriptions and samples, not a vendor copy.

Shape (public samples): an OMeS root, PMSetup elements with startTime and
interval (minutes), each holding PMMOResult elements, each with an MO holding a DN
and a PMTarget whose measurementType attribute names the measurement and
whose child elements are counters named by id. Here one PMMOResult carries
one managed object and one measurementType. Timestamps are UTC with the
offset written as +00:00:00, as in the public samples (rule W5: this EMS
writes UTC). There is no suspect flag in this format. A file holds the
15-minute setup of its period and, in the last period of an hour, a
60-minute setup for the distribution measurements (rule P4).

Object DNs use the public naming style (PLMN-PLMN/MRBTS-n/LNBTS-n/LNCEL-n
for LTE, PLMN-PLMN/BSC-n/BCF-n/BTS-n for GSM); the numbering and the
relation objects (LNREL-n, ADJS-n) are ASSUMPTION. File names are
ASSUMPTION: OMeS_<EMS>_<UTC start>.xml.gz.
"""

import gzip
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import numpy as np
from lxml import etree

from ran_lakehouse.files.pm_xml import format_matrix

FILE_NAME = re.compile(r"^OMeS_(?P<ems>[A-Za-z0-9-]+)_(?P<start>\d{8}T\d{4})Z\.xml(?P<gz>\.gz)?$")
START_TIME = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?P<sign>[+-])(?P<hh>\d{2}):(?P<mm>\d{2})(?::\d{2})?$"
)


@dataclass(frozen=True)
class Target:
    """One PMTarget: a measurement type over a list of managed objects.

    Attributes:
        measurement_type: measurementType attribute.
        counters: Counter ids (element names).
        dns: DN of each managed object.
        values: Values, shape (objects, counters); NaN values are omitted.
    """

    measurement_type: str
    counters: list[str]
    dns: list[str]
    values: np.ndarray


@dataclass(frozen=True)
class Setup:
    """One PMSetup: a collection interval and its measurements.

    Attributes:
        begin: Interval start, aware (written in UTC).
        interval_min: Granularity in minutes.
        targets: Measurement types with their objects.
    """

    begin: datetime
    interval_min: int
    targets: list[Target]


def start_text(t: datetime) -> str:
    """PMSetup startTime text in UTC, public-sample style.

    Args:
        t: Aware time.

    Returns:
        "YYYY-MM-DDTHH:MM:SS.000+00:00:00".

    Raises:
        ValueError: If the time is naive.
    """
    if t.tzinfo is None:
        raise ValueError("PM timestamps must carry a UTC offset")
    return f"{t.astimezone(UTC):%Y-%m-%dT%H:%M:%S}.000+00:00:00"


def file_name(ems_id: str, begin: datetime) -> str:
    """Nokia-style file name (ASSUMPTION).

    Args:
        ems_id: EMS name.
        begin: Period start, aware.

    Returns:
        For example "OMeS_EMS-NK-01_20260105T0800Z.xml.gz".
    """
    return f"OMeS_{ems_id}_{begin.astimezone(UTC):%Y%m%dT%H%M}Z.xml.gz"


def parse_file_name(name: str) -> dict[str, Any]:
    """Split a Nokia-style file name.

    Args:
        name: File name.

    Returns:
        EMS name, UTC begin and whether it is gzip-compressed.

    Raises:
        ValueError: If the name does not follow the convention.
    """
    match = FILE_NAME.match(name)
    if match is None:
        raise ValueError(f"not a Nokia-style PM file name: {name!r}")
    begin = datetime.strptime(match["start"], "%Y%m%dT%H%M").replace(tzinfo=UTC)
    return {"ems": match["ems"], "begin": begin, "gzip": match["gz"] is not None}


def write_file(setups: list[Setup]) -> bytes:
    """An OMeS-shaped PM file, gzip-compressed.

    Args:
        setups: The file's PMSetup elements, in order.

    Returns:
        The compressed file.
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<OMeS>\n']
    for setup in setups:
        parts.append(
            f'<PMSetup startTime="{start_text(setup.begin)}" interval="{setup.interval_min}">\n'
        )
        write_targets(parts, setup.targets)
        parts.append("</PMSetup>\n")
    parts.append("</OMeS>\n")
    return gzip.compress("".join(parts).encode("utf-8"), compresslevel=6, mtime=0)


def write_targets(parts: list[str], targets: list[Target]) -> None:
    """Append the PMMOResult elements of some targets.

    Args:
        parts: Text parts of the file, extended in place.
        targets: Measurement types with their objects.
    """
    for target in targets:
        opens = [f"<{c}>" for c in target.counters]
        closes = [f"</{c}>" for c in target.counters]
        values = np.asarray(target.values, dtype=float)
        texts = format_matrix(values)
        for dn, row, present in zip(target.dns, texts, ~np.isnan(values), strict=True):
            counters = "".join(
                o + t + c for o, t, c, ok in zip(opens, row, closes, present, strict=True) if ok
            )
            parts.append(
                f"<PMMOResult><MO><DN>{dn}</DN></MO>"
                f'<PMTarget measurementType="{target.measurement_type}">{counters}</PMTarget>'
                "</PMMOResult>\n"
            )


def parse_start(text: str) -> datetime:
    """Parse a PMSetup startTime, with its hh:mm[:ss] offset.

    Args:
        text: The attribute.

    Returns:
        Aware time.

    Raises:
        ValueError: If it is not a timestamp with an offset.
    """
    match = START_TIME.match(text)
    if match is None:
        raise ValueError(f"startTime {text!r} is not a timestamp with an offset")
    sign = 1 if match["sign"] == "+" else -1
    tz = timezone(sign * timedelta(hours=int(match["hh"]), minutes=int(match["mm"])))
    return datetime.strptime(match["stamp"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=tz)


def parse_arrays(content: bytes) -> dict[str, Any]:
    """Parse an OMeS-shaped PM file (plain or gzip) into flat arrays.

    Args:
        content: File bytes.

    Returns:
        setups (begin, aware, and interval_min per PMSetup), and per
        PMMOResult its setup index, dn, measurement type and counter count,
        plus the flat counter names and values in document order.

    Raises:
        ValueError: If the structure is not OMeS / PMSetup / PMMOResult.
    """
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False))
    if root.tag != "OMeS":
        raise ValueError(f"root is {root.tag}, not OMeS")
    setups = root.findall("PMSetup")
    if not setups:
        raise ValueError("OMeS needs a PMSetup")
    setup_index: list[int] = []
    dns: list[str] = []
    types: list[str] = []
    sizes: list[int] = []
    tags: list[str] = []
    texts: list[str] = []
    for index, setup in enumerate(setups):
        interval = setup.get("interval")
        if interval is None:
            raise ValueError("PMSetup needs interval")
        for result in setup.iterfind("PMMOResult"):
            dn = result.findtext("MO/DN")
            target = result.find("PMTarget")
            if dn is None or target is None or target.get("measurementType") is None:
                raise ValueError("PMMOResult needs MO/DN and PMTarget with measurementType")
            counters = list(target)
            setup_index.append(index)
            dns.append(dn)
            types.append(target.get("measurementType"))
            sizes.append(len(counters))
            tags.extend(c.tag for c in counters)
            texts.extend(c.text or "nan" for c in counters)
    return {
        "setups": [
            {
                "begin": parse_start(s.get("startTime", "")),
                "interval_min": int(s.get("interval", "")),
            }
            for s in setups
        ],
        "setup_index": np.array(setup_index, dtype=np.int64),
        "dns": dns,
        "types": types,
        "sizes": np.array(sizes, dtype=np.int64),
        "tags": tags,
        "values": np.array(texts, dtype=float),
    }


def parse_file(content: bytes) -> dict[str, Any]:
    """Parse an OMeS-shaped PM file (plain or gzip) into long-format columns.

    Args:
        content: File bytes.

    Returns:
        setups (begin and interval_min per PMSetup) and columns begin,
        interval_min, dn, measurement_type, counter, value.

    Raises:
        ValueError: If the structure is not OMeS / PMSetup / PMMOResult.
    """
    parsed = parse_arrays(content)
    sizes = parsed["sizes"]
    per_row = np.repeat(parsed["setup_index"], sizes)
    setups = parsed["setups"]
    return {
        "setups": setups,
        "columns": {
            "begin": [setups[i]["begin"] for i in per_row],
            "interval_min": [setups[i]["interval_min"] for i in per_row],
            "dn": list(np.repeat(np.array(parsed["dns"], dtype=object), sizes)),
            "measurement_type": list(np.repeat(np.array(parsed["types"], dtype=object), sizes)),
            "counter": parsed["tags"],
            "value": parsed["values"].tolist(),
        },
    }
