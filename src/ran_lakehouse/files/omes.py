"""Nokia-style PM files (rule P4): an OMeS-shaped XML format, modelled on
public descriptions and samples, not a vendor copy.

Shape (public samples): an OMeS root, one PMSetup with startTime and
interval (minutes), then PMMOResult elements, each with an MO holding a DN
and a PMTarget whose measurementType attribute names the measurement and
whose child elements are counters named by id. Here one PMMOResult carries
one managed object and one measurementType. Timestamps are UTC with the
offset written as +00:00:00, as in the public samples (rule W5: this EMS
writes UTC). There is no suspect flag in this format.

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

from ran_lakehouse.files.pm_xml import number

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


def write_file(begin: datetime, interval_min: int, targets: list[Target]) -> bytes:
    """An OMeS-shaped PM file, gzip-compressed.

    Args:
        begin: Period start, aware (written in UTC).
        interval_min: Granularity in minutes.
        targets: Measurement types with their objects.

    Returns:
        The compressed file.
    """
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n<OMeS>\n',
        f'<PMSetup startTime="{start_text(begin)}" interval="{interval_min}">\n',
    ]
    for target in targets:
        for dn, row in zip(target.dns, target.values, strict=True):
            counters = "".join(
                f"<{c}>{number(v)}</{c}>"
                for c, v in zip(target.counters, row, strict=True)
                if not np.isnan(v)
            )
            parts.append(
                f"<PMMOResult><MO><DN>{dn}</DN></MO>"
                f'<PMTarget measurementType="{target.measurement_type}">{counters}</PMTarget>'
                "</PMMOResult>\n"
            )
    parts.append("</PMSetup>\n</OMeS>\n")
    return gzip.compress("".join(parts).encode("utf-8"), compresslevel=6)


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


def parse_file(content: bytes) -> dict[str, Any]:
    """Parse an OMeS-shaped PM file (plain or gzip) into long-format columns.

    Args:
        content: File bytes.

    Returns:
        begin (aware), interval in minutes, and columns dn,
        measurement_type, counter, value.

    Raises:
        ValueError: If the structure is not OMeS / PMSetup / PMMOResult.
    """
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False))
    if root.tag != "OMeS":
        raise ValueError(f"root is {root.tag}, not OMeS")
    setups = root.findall("PMSetup")
    if len(setups) != 1:
        raise ValueError(f"expected one PMSetup, found {len(setups)}")
    setup = setups[0]
    columns: dict[str, list[Any]] = {k: [] for k in ("dn", "measurement_type", "counter", "value")}
    for result in setup.iterfind("PMMOResult"):
        dn = result.findtext("MO/DN")
        target = result.find("PMTarget")
        if dn is None or target is None or target.get("measurementType") is None:
            raise ValueError("PMMOResult needs MO/DN and PMTarget with measurementType")
        for counter in target:
            columns["dn"].append(dn)
            columns["measurement_type"].append(target.get("measurementType"))
            columns["counter"].append(counter.tag)
            columns["value"].append(float(counter.text or "nan"))
    return {
        "begin": parse_start(setup.get("startTime", "")),
        "interval_min": int(setup.get("interval", "0")),
        "columns": columns,
    }
