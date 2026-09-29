"""3GPP PM XML files (rules P1-P3): writing, parsing, naming and checking.

Format per TS 32.435 V19.0.0 clause 4 (measCollecFile; the schema in
clause 4.2.2 is unchanged since Rel-8): one measData per network element,
one measInfo per measured object class and counter set, the list form
(measTypes / measResults), an optional suspect per measValue (TS 32.432
V19.0.0 clause 4.1 suspectFlag, omitted when false). File names per TS
32.432 V19.0.0 clause 5.1.2: type B (multiple network elements, one
granularity period) is written; types A and B are parsed. Timestamps carry
their UTC offset.

The 3GPP schema is not vendored: the 3GPP FAQ asks for permission to
redistribute verbatim code from a specification. `check_structure` checks
the elements, attributes and nesting of clause 4.2.2 as described there;
an optional test validates against the schema fetched from the 3GPP
archive.
"""

import gzip
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from lxml import etree

NAMESPACE = "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec"
# TS 32.432 V19.0.0 Table 4.1: the abridged number and version of TS 32.435
# V19.0.0 (derived by the rule stated there).
FILE_FORMAT_VERSION = "32.435 V19.0"
GRANULARITY = "PT900S"  # TS 32.435 truncated representation PTnS
# TS 32.435 V19.0.0 clause 4.2.3: header of every measurement file.
HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>\n'
)
# TS 32.432 V19.0.0 clause 5.1.2 file name, types A and B (one period):
# <Type><Startdate>.<Starttime>-<Endtime>[_-<jobId>][_<UniqueId>][_-_<RC>],
# times HHMMshhmm; plus the ".xml" and ".gz" extensions (the TS 28.532
# V20.2.0 clause 11.3.2.1.4 convention for processing steps).
FILE_NAME = re.compile(
    r"^(?P<type>[AB])(?P<date>\d{8})\.(?P<start>\d{4}[+-]\d{4})-(?P<end>\d{4}[+-]\d{4})"
    r"(?:_-(?P<job>[^_]+))?(?:_(?P<unique>[^.]+?))?(?:_-_(?P<rc>\d+))?\.xml(?P<gz>\.gz)?$"
)


@dataclass(frozen=True)
class MeasInfo:
    """One measInfo: one counter set over a list of measured objects.

    Attributes:
        meas_info_id: measInfoId.
        counters: Counter names (measTypes), each an XML Name.
        objects: measObjLdn of each measValue, relative to the managed
            element.
        values: Values, shape (objects, counters); NaN is written as NIL.
        suspect: Suspect flag per object.
    """

    meas_info_id: str
    counters: list[str]
    objects: list[str]
    values: np.ndarray
    suspect: np.ndarray


@dataclass(frozen=True)
class ElementData:
    """One measData: a managed element and its measInfos.

    Attributes:
        local_dn: managedElement localDn.
        sw_version: managedElement swVersion.
        meas_infos: The element's measInfos.
    """

    local_dn: str
    sw_version: str
    meas_infos: list[MeasInfo]


def iso(t: datetime) -> str:
    """xs:dateTime with its UTC offset.

    Args:
        t: Aware time.

    Returns:
        "YYYY-MM-DDTHH:MM:SS+HH:MM".

    Raises:
        ValueError: If the time is naive.
    """
    if t.tzinfo is None:
        raise ValueError("PM timestamps must carry a UTC offset")
    return t.isoformat()


def offset_text(t: datetime) -> str:
    """The shhmm part of a TS 32.432 file name time.

    Args:
        t: Aware time.

    Returns:
        "+0700" style offset.
    """
    delta = t.utcoffset()
    if delta is None:
        raise ValueError("PM timestamps must carry a UTC offset")
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}{abs(minutes) % 60:02d}"


def file_name(kind: str, begin: datetime, end: datetime, unique_id: str) -> str:
    """TS 32.432 file name of a single-period file, gzip-compressed.

    Args:
        kind: "A" (one network element) or "B" (several).
        begin: Start of the granularity period, aware local time.
        end: End of the period, aware, same offset.
        unique_id: Name of the network element (A) or EMS (B).

    Returns:
        For example "B20260105.1500+0700-1515+0700_EMS-HW-01.xml.gz".

    Raises:
        ValueError: For an unknown type or mismatched offsets.
    """
    if kind not in ("A", "B"):
        raise ValueError(f"file type {kind!r} is not written; A or B")
    if begin.utcoffset() != end.utcoffset():
        raise ValueError("begin and end must share one UTC offset")
    start = f"{begin:%H%M}{offset_text(begin)}"
    stop = f"{end:%H%M}{offset_text(end)}"
    return f"{kind}{begin:%Y%m%d}.{start}-{stop}_{unique_id}.xml.gz"


def parse_file_name(name: str) -> dict[str, Any]:
    """Split a TS 32.432 type A or B file name.

    Args:
        name: File name.

    Returns:
        Type, begin and end (aware), unique id, job id, running count and
        whether it is gzip-compressed.

    Raises:
        ValueError: If the name does not follow the convention.
    """
    match = FILE_NAME.match(name)
    if match is None:
        raise ValueError(f"not a TS 32.432 type A or B file name: {name!r}")

    def moment(date: str, hhmm_offset: str) -> datetime:
        sign = 1 if hhmm_offset[4] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(hhmm_offset[5:7]), minutes=int(hhmm_offset[7:9])))
        return datetime.strptime(date + hhmm_offset[:4], "%Y%m%d%H%M").replace(tzinfo=tz)

    begin = moment(match["date"], match["start"])
    end = moment(match["date"], match["end"])
    if end <= begin:
        end += timedelta(days=1)
    return {
        "type": match["type"],
        "begin": begin,
        "end": end,
        "unique_id": match["unique"],
        "job_id": match["job"],
        "running_count": int(match["rc"]) if match["rc"] else None,
        "gzip": match["gz"] is not None,
    }


def number(value: float) -> str:
    """A measResults item: integer text for whole numbers, NIL for NaN.

    Args:
        value: The value.

    Returns:
        Its text.
    """
    if np.isnan(value):
        return "NIL"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


def write_file(
    sender_dn_prefix: str,
    sender_ldn: str,
    vendor_name: str,
    begin: datetime,
    end: datetime,
    elements: list[ElementData],
) -> bytes:
    """A measCollecFile, gzip-compressed.

    Args:
        sender_dn_prefix: fileHeader dnPrefix.
        sender_ldn: fileSender localDn.
        vendor_name: fileHeader vendorName.
        begin: Collection begin (measCollec beginTime), aware.
        end: Collection end (fileFooter endTime, granPeriod endTime), aware.
        elements: One entry per network element.

    Returns:
        The compressed file.
    """
    parts = [
        HEADER,
        f'<measCollecFile xmlns="{NAMESPACE}">\n',
        f'<fileHeader fileFormatVersion="{FILE_FORMAT_VERSION}" vendorName="{vendor_name}" '
        f'dnPrefix="{sender_dn_prefix}">\n',
        f'<fileSender localDn="{sender_ldn}" elementType="EMS"/>\n',
        f'<measCollec beginTime="{iso(begin)}"/>\n',
        "</fileHeader>\n",
    ]
    for element in elements:
        parts.append(
            f'<measData>\n<managedElement localDn="{element.local_dn}" '
            f'swVersion="{element.sw_version}"/>\n'
        )
        for info in element.meas_infos:
            if not info.objects:
                continue
            parts.append(
                f'<measInfo measInfoId="{info.meas_info_id}">\n'
                f'<granPeriod duration="{GRANULARITY}" endTime="{iso(end)}"/>\n'
                f'<repPeriod duration="{GRANULARITY}"/>\n'
                f"<measTypes>{' '.join(info.counters)}</measTypes>\n"
            )
            for obj, row, suspect in zip(info.objects, info.values, info.suspect, strict=True):
                results = " ".join(number(v) for v in row)
                flag = "<suspect>true</suspect>" if suspect else ""
                parts.append(
                    f'<measValue measObjLdn="{obj}"><measResults>{results}</measResults>'
                    f"{flag}</measValue>\n"
                )
            parts.append("</measInfo>\n")
        parts.append("</measData>\n")
    parts.append(f'<fileFooter>\n<measCollec endTime="{iso(end)}"/>\n</fileFooter>\n')
    parts.append("</measCollecFile>\n")
    return gzip.compress("".join(parts).encode("utf-8"), compresslevel=6)


def child(parent: Any, tag: str) -> Any:
    """A required child element.

    Args:
        parent: Parent element.
        tag: Local name of the child.

    Returns:
        The first such child.

    Raises:
        ValueError: If there is none.
    """
    found = parent.find(q(tag))
    if found is None:
        raise ValueError(f"{parent.tag} has no {tag}")
    return found


def q(tag: str) -> str:
    """A tag in the measCollec namespace.

    Args:
        tag: Local name.

    Returns:
        Clark-notation name.
    """
    return f"{{{NAMESPACE}}}{tag}"


def check_structure(root: Any) -> None:
    """Check a measCollecFile against the structure of TS 32.435 clause 4.2.2.

    Checks element order and nesting, the required attributes and the
    list-or-positional forms. It does not replace full schema validation.

    Args:
        root: The parsed root element.

    Raises:
        ValueError: With the first breach found.
    """

    def need(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    need(root.tag == q("measCollecFile"), f"root is {root.tag}, not measCollecFile")
    children = [c for c in root if isinstance(c.tag, str)]
    need(len(children) >= 2, "measCollecFile needs fileHeader and fileFooter")
    need(children[0].tag == q("fileHeader"), "first child must be fileHeader")
    need(children[-1].tag == q("fileFooter"), "last child must be fileFooter")
    header = children[0]
    need(header.get("fileFormatVersion") is not None, "fileHeader needs fileFormatVersion")
    head = [c.tag for c in header if isinstance(c.tag, str)]
    need(head == [q("fileSender"), q("measCollec")], "fileHeader needs fileSender, measCollec")
    need(header[1].get("beginTime") is not None, "fileHeader measCollec needs beginTime")
    footer = [c for c in children[-1] if isinstance(c.tag, str)]
    need(
        len(footer) == 1 and footer[0].tag == q("measCollec") and footer[0].get("endTime"),
        "fileFooter needs measCollec with endTime",
    )
    for data in children[1:-1]:
        need(data.tag == q("measData"), f"unexpected {data.tag} between header and footer")
        parts = [c for c in data if isinstance(c.tag, str)]
        need(bool(parts) and parts[0].tag == q("managedElement"), "measData needs managedElement")
        for info in parts[1:]:
            need(info.tag == q("measInfo"), f"unexpected {info.tag} in measData")
            check_meas_info(info)


def check_meas_info(info: Any) -> None:
    """Check one measInfo (TS 32.435 clause 4.2.2).

    Args:
        info: The measInfo element.

    Raises:
        ValueError: With the first breach found.
    """
    tags = [c.tag for c in info if isinstance(c.tag, str)]
    position = 0
    if position < len(tags) and tags[position] == q("job"):
        position += 1
    if tags[position : position + 1] != [q("granPeriod")]:
        raise ValueError("measInfo needs granPeriod")
    period = child(info, "granPeriod")
    if not (period.get("duration") and period.get("endTime")):
        raise ValueError("granPeriod needs duration and endTime")
    position += 1
    if tags[position : position + 1] == [q("repPeriod")]:
        position += 1
    rest = tags[position:]
    list_form = rest[:1] == [q("measTypes")]
    if list_form:
        values = rest[1:]
        counters = len((child(info, "measTypes").text or "").split())
    else:
        count = 0
        while count < len(rest) and rest[count] == q("measType"):
            count += 1
        values = rest[count:]
        counters = count
    if any(t != q("measValue") for t in values):
        raise ValueError("measInfo may hold only measValue after its types")
    for value in info.findall(q("measValue")):
        if value.get("measObjLdn") is None:
            raise ValueError("measValue needs measObjLdn")
        inner = [c.tag for c in value if isinstance(c.tag, str)]
        if inner and inner[-1] == q("suspect"):
            inner = inner[:-1]
        if list_form:
            if inner != [q("measResults")]:
                raise ValueError("list-form measValue needs one measResults")
            results = len((child(value, "measResults").text or "").split())
            if results != counters:
                raise ValueError(f"{results} results for {counters} measTypes")
        elif any(t != q("r") for t in inner):
            raise ValueError("positional measValue may hold only r elements")


def parse_file(content: bytes) -> dict[str, Any]:
    """Parse a measCollecFile (plain or gzip) into long-format columns.

    Accepts the list form and the positional form (measType p / r p).

    Args:
        content: File bytes.

    Returns:
        Header fields and columns: managed_element, meas_info_id,
        meas_obj_ldn, counter, value (NaN for NIL), suspect.

    Raises:
        ValueError: If the structure check fails.
    """
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False))
    check_structure(root)
    header = child(root, "fileHeader")
    begin = datetime.fromisoformat(child(header, "measCollec").get("beginTime"))
    end = datetime.fromisoformat(child(child(root, "fileFooter"), "measCollec").get("endTime"))
    columns: dict[str, list[Any]] = {
        k: []
        for k in ("managed_element", "meas_info_id", "meas_obj_ldn", "counter", "value", "suspect")
    }
    for data in root.iterfind(q("measData")):
        ne = child(data, "managedElement").get("localDn")
        for info in data.iterfind(q("measInfo")):
            types = info.find(q("measTypes"))
            if types is not None:
                names = (types.text or "").split()
            else:
                ordered = sorted(info.iterfind(q("measType")), key=lambda e: int(e.get("p", "0")))
                names = [e.text or "" for e in ordered]
            for value in info.iterfind(q("measValue")):
                suspect_el = value.find(q("suspect"))
                suspect = suspect_el is not None and (suspect_el.text or "").strip() == "true"
                results = value.find(q("measResults"))
                if results is not None:
                    items = (results.text or "").split()
                else:
                    by_p = {int(r.get("p", "0")): r.text or "" for r in value.iterfind(q("r"))}
                    items = [by_p.get(i + 1, "NIL") for i in range(len(names))]
                for name, item in zip(names, items, strict=True):
                    columns["managed_element"].append(ne)
                    columns["meas_info_id"].append(info.get("measInfoId"))
                    columns["meas_obj_ldn"].append(value.get("measObjLdn"))
                    columns["counter"].append(name)
                    columns["value"].append(float("nan") if item == "NIL" else float(item))
                    columns["suspect"].append(suspect)
    return {
        "file_format_version": header.get("fileFormatVersion"),
        "vendor_name": header.get("vendorName"),
        "dn_prefix": header.get("dnPrefix"),
        "begin": begin,
        "end": end,
        "columns": columns,
    }
