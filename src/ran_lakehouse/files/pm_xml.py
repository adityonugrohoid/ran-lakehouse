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
        duration_s: granPeriod duration in seconds (900 or 3600); its
            endTime is the file's end.
    """

    meas_info_id: str
    counters: list[str]
    objects: list[str]
    values: np.ndarray
    suspect: np.ndarray
    duration_s: int


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
    return f"{value:.10g}"


def format_matrix(values: np.ndarray) -> list[list[str]]:
    """Format a block of values as measResults items, vectorized.

    Whole numbers are written as integers, NaN as NIL, other values with 10
    significant digits (the same text as number()).

    Args:
        values: Values, shape (rows, columns).

    Returns:
        Text per row and column.
    """
    out = np.empty(values.shape, dtype=object)
    nan = np.isnan(values)
    whole = ~nan & (values == np.floor(values)) & (np.abs(values) < 2.0**53)
    out[whole] = values[whole].astype(np.int64).astype(str)
    rest = ~nan & ~whole
    if rest.any():
        out[rest] = [f"{v:.10g}" for v in values[rest]]
    out[nan] = "NIL"
    rows: list[list[str]] = out.tolist()
    return rows


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
                f'<granPeriod duration="PT{info.duration_s}S" endTime="{iso(end)}"/>\n'
                f'<repPeriod duration="PT{info.duration_s}S"/>\n'
                f"<measTypes>{' '.join(info.counters)}</measTypes>\n"
            )
            texts = format_matrix(np.asarray(info.values, dtype=float))
            for obj, row, suspect in zip(info.objects, texts, info.suspect, strict=True):
                results = " ".join(row)
                flag = "<suspect>true</suspect>" if suspect else ""
                parts.append(
                    f'<measValue measObjLdn="{obj}"><measResults>{results}</measResults>'
                    f"{flag}</measValue>\n"
                )
            parts.append("</measInfo>\n")
        parts.append("</measData>\n")
    parts.append(f'<fileFooter>\n<measCollec endTime="{iso(end)}"/>\n</fileFooter>\n')
    parts.append("</measCollecFile>\n")
    return gzip.compress("".join(parts).encode("utf-8"), compresslevel=6, mtime=0)


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


@dataclass(frozen=True)
class Block:
    """One measInfo of one managed element, parsed.

    Attributes:
        element: managedElement localDn.
        sw_version: managedElement swVersion (None if absent).
        meas_info_id: measInfoId.
        counters: measTypes names.
        objects: measObjLdn per measValue.
        values: Values, shape (objects, counters); NaN for NIL.
        suspect: Suspect flag per measValue.
        period_end: granPeriod endTime (aware).
        duration_s: granPeriod duration in seconds.
    """

    element: str
    sw_version: str | None
    meas_info_id: str
    counters: list[str]
    objects: list[str]
    values: np.ndarray
    suspect: np.ndarray
    period_end: datetime
    duration_s: int


def duration_seconds(text: str | None) -> int:
    """Seconds of a PTnS duration (TS 32.435 truncated form).

    Args:
        text: The duration attribute.

    Returns:
        Seconds.

    Raises:
        ValueError: If it is not PTnS.
    """
    match = re.fullmatch(r"PT(\d+)S", text or "")
    if match is None:
        raise ValueError(f"duration {text!r} is not PTnS")
    return int(match[1])


def parse_blocks(content: bytes) -> dict[str, Any]:
    """Parse a measCollecFile (plain or gzip) block by block, vectorized.

    Accepts the list form and the positional form (measType p / r p). It
    checks what it reads (root, header, footer, result counts, object
    names); check_structure is the full clause 4.2.2 structure check.

    Args:
        content: File bytes.

    Returns:
        Header fields (file_format_version, vendor_name, dn_prefix, begin,
        end) and blocks.

    Raises:
        ValueError: If the structure check fails or a block is malformed.
    """
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False))
    if root.tag != q("measCollecFile"):
        raise ValueError(f"root is {root.tag}, not measCollecFile")
    header = child(root, "fileHeader")
    if header.get("fileFormatVersion") is None:
        raise ValueError("fileHeader needs fileFormatVersion")
    begin = datetime.fromisoformat(child(header, "measCollec").get("beginTime"))
    end = datetime.fromisoformat(child(child(root, "fileFooter"), "measCollec").get("endTime"))
    blocks: list[Block] = []
    for data in root.iterfind(q("measData")):
        element = child(data, "managedElement")
        ne = element.get("localDn")
        sw_version = element.get("swVersion")
        for info in data.iterfind(q("measInfo")):
            meas_values = info.findall(q("measValue"))
            period = child(info, "granPeriod")
            types = info.find(q("measTypes"))
            if types is not None:
                names = (types.text or "").split()
                texts = [mv.findtext(q("measResults")) or "" for mv in meas_values]
                flat = " ".join(texts).replace("NIL", "nan").split()
                if len(flat) != len(names) * len(meas_values):
                    raise ValueError(f"measInfo {info.get('measInfoId')}: result count mismatch")
                values = np.array(flat, dtype=float).reshape(len(meas_values), len(names))
            else:
                ordered = sorted(info.iterfind(q("measType")), key=lambda e: int(e.get("p", "0")))
                names = [e.text or "" for e in ordered]
                values = np.full((len(meas_values), len(names)), np.nan)
                for row, mv in enumerate(meas_values):
                    for r in mv.iterfind(q("r")):
                        text = r.text or "NIL"
                        values[row, int(r.get("p", "0")) - 1] = (
                            np.nan if text == "NIL" else float(text)
                        )
            objects = [mv.get("measObjLdn") for mv in meas_values]
            if any(o is None for o in objects):
                raise ValueError("measValue needs measObjLdn")
            suspect = np.array(
                [(mv.findtext(q("suspect")) or "").strip() == "true" for mv in meas_values],
                dtype=bool,
            )
            blocks.append(
                Block(
                    element=ne,
                    sw_version=sw_version,
                    meas_info_id=info.get("measInfoId"),
                    counters=names,
                    objects=objects,
                    values=values,
                    suspect=suspect,
                    period_end=datetime.fromisoformat(period.get("endTime")),
                    duration_s=duration_seconds(period.get("duration")),
                )
            )
    return {
        "file_format_version": header.get("fileFormatVersion"),
        "vendor_name": header.get("vendorName"),
        "dn_prefix": header.get("dnPrefix"),
        "begin": begin,
        "end": end,
        "blocks": blocks,
    }


def parse_file(content: bytes) -> dict[str, Any]:
    """Parse a measCollecFile (plain or gzip) into long-format columns.

    Accepts the list form and the positional form (measType p / r p).

    Args:
        content: File bytes.

    Returns:
        Header fields and columns: managed_element, sw_version,
        meas_info_id, meas_obj_ldn, counter, value (NaN for NIL), suspect,
        duration_s (the block's granPeriod).

    Raises:
        ValueError: If the structure check fails.
    """
    parsed = parse_blocks(content)
    columns: dict[str, list[Any]] = {
        k: []
        for k in (
            "managed_element",
            "sw_version",
            "meas_info_id",
            "meas_obj_ldn",
            "counter",
            "value",
            "suspect",
            "duration_s",
        )
    }
    for b in parsed["blocks"]:
        for row, obj in enumerate(b.objects):
            for col, name in enumerate(b.counters):
                columns["managed_element"].append(b.element)
                columns["sw_version"].append(b.sw_version)
                columns["meas_info_id"].append(b.meas_info_id)
                columns["meas_obj_ldn"].append(obj)
                columns["counter"].append(name)
                columns["value"].append(float(b.values[row, col]))
                columns["suspect"].append(bool(b.suspect[row]))
                columns["duration_s"].append(b.duration_s)
    blocks = parsed.pop("blocks")
    del blocks
    return parsed | {"columns": columns}
