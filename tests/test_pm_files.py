"""3GPP PM XML files (rules P1-P3, P5) on the tiny profile."""

import gzip
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pytest
from lxml import etree

from ran_lakehouse.files import omes
from ran_lakehouse.files import report as pm_report
from ran_lakehouse.files.dialects import HUAWEI_R1, NOKIA_R1
from ran_lakehouse.files.ems import WIB, Ems, day_files, nokia_dn, nokia_relation_dn, relative_ldn
from ran_lakehouse.files.pm_xml import (
    ElementData,
    MeasInfo,
    check_structure,
    file_name,
    parse_file,
    parse_file_name,
    write_file,
)
from ran_lakehouse.model import Day, NetworkModel, default_model, simulate_days
from ran_lakehouse.world import build_world

EMS = Ems("EMS-HW-01", HUAWEI_R1, WIB, "Huawei-style synthetic EMS", "3gpp-xml")
SCHEMA_URL = (
    "https://www.3gpp.org/ftp/Specs/archive/32_series/32.435/schema/32435-800-XMLSchema.zip"
)
SCHEMA_CACHE = Path(__file__).resolve().parents[1] / ".cache" / "3gpp" / "measCollec.xsd"


@pytest.fixture(scope="module")
def tiny() -> tuple[NetworkModel, Day, list[tuple[str, bytes]]]:
    model = default_model(build_world("tiny"))
    day = next(simulate_days(model, 0, 1))
    return model, day, list(day_files(model, day, EMS))


def test_one_type_b_file_per_period(
    tiny: tuple[NetworkModel, Day, list[tuple[str, bytes]]],
) -> None:
    _, _, files = tiny
    assert len(files) == 96
    first = parse_file_name(files[0][0])
    assert first["type"] == "B" and first["unique_id"] == "EMS-HW-01" and first["gzip"]
    assert first["begin"].utcoffset() == timedelta(hours=7)
    assert files[0][0] == "B20260105.0000+0700-0015+0700_EMS-HW-01.xml.gz"


def test_values_round_trip(tiny: tuple[NetworkModel, Day, list[tuple[str, bytes]]]) -> None:
    model, day, files = tiny
    parsed = parse_file(files[40][1])
    assert parsed["file_format_version"] == "32.435 V19.0"
    cols = parsed["columns"]
    lte = np.flatnonzero(model.state.technology == "LTE")
    cell = int(lte[0])
    element = model.world.cells[cell].managed_element
    ldn = relative_ldn(model.world.cells[cell].dn, element)
    column = int(np.flatnonzero(day.lte.cells == cell)[0])
    got = {
        c: v
        for e, o, c, v in zip(
            cols["managed_element"],
            cols["meas_obj_ldn"],
            cols["counter"],
            cols["value"],
            strict=True,
        )
        if e == f"ManagedElement={element}" and o == ldn
    }
    assert got["L.RRC.ConnReq.Att"] == day.lte.values["RRC.ConnEstabAtt.sum"][40, column]
    assert got["L.Thrp.bits.DL"] == pytest.approx(
        day.lte.values["DRB.IPVolDl.sum"][40, column] * 1000.0
    )


def test_type_a_file_parses() -> None:
    begin = datetime(2026, 1, 5, 15, 0, tzinfo=timezone(timedelta(hours=7)))
    end = begin + timedelta(minutes=15)
    info = MeasInfo(
        meas_info_id="LTE.Cell",
        counters=["L.RRC.ConnReq.Att"],
        objects=["ENBFunction=1,EUtranCellFDD=X_1"],
        values=np.array([[12.0]]),
        suspect=np.array([True]),
    )
    element = ElementData("ManagedElement=ENB0001", "R1", [info])
    content = write_file("SubNetwork=RanLake", "ENB0001", "test", begin, end, [element])
    name = file_name("A", begin, end, "ENB0001")
    assert name == "A20260105.1500+0700-1515+0700_ENB0001.xml.gz"
    assert parse_file_name(name)["type"] == "A"
    parsed = parse_file(content)
    assert parsed["columns"]["value"] == [12.0] and parsed["columns"]["suspect"] == [True]


def test_positional_form_parses() -> None:
    ns = "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec"
    xml = f"""<measCollecFile xmlns="{ns}"><fileHeader fileFormatVersion="32.435 V19.0">
<fileSender/><measCollec beginTime="2026-01-05T15:00:00+07:00"/></fileHeader>
<measData><managedElement localDn="ManagedElement=E1"/><measInfo>
<granPeriod duration="PT900S" endTime="2026-01-05T15:15:00+07:00"/>
<measType p="2">B</measType><measType p="1">A</measType>
<measValue measObjLdn="X=1"><r p="1">5</r><r p="2">NIL</r></measValue>
</measInfo></measData><fileFooter><measCollec endTime="2026-01-05T15:15:00+07:00"/></fileFooter>
</measCollecFile>"""
    cols = parse_file(xml.encode())["columns"]
    assert cols["counter"] == ["A", "B"]
    assert cols["value"][0] == 5.0 and np.isnan(cols["value"][1])


def test_structure_check_rejects_broken_files(
    tiny: tuple[NetworkModel, Day, list[tuple[str, bytes]]],
) -> None:
    text = gzip.decompress(tiny[2][0][1]).decode()
    broken = text.replace("<fileFooter>", "<fileFooterX>").replace(
        "</fileFooter>", "</fileFooterX>"
    )
    with pytest.raises(ValueError, match="fileFooter"):
        check_structure(etree.fromstring(broken.encode()))
    missing = text.replace("</measResults>", " 1</measResults>", 1)
    with pytest.raises(ValueError, match="results for"):
        check_structure(etree.fromstring(missing.encode()))


def test_bad_file_names_fail() -> None:
    with pytest.raises(ValueError, match=r"not a TS 32\.432"):
        parse_file_name("C20260105.1500+0700-20260106.1500+0700_X.xml")


def test_report_markdown_matches_record() -> None:
    record = json.loads(pm_report.RECORD_JSON.read_text())
    assert pm_report.RECORD_MD.read_text() == pm_report.render_markdown(record)


@pytest.mark.network
def test_files_validate_against_the_3gpp_schema(
    tiny: tuple[NetworkModel, Day, list[tuple[str, bytes]]],
) -> None:
    """Full schema validation, with the schema fetched from the 3GPP archive.

    Run with `pytest -m network`; the schema is cached in .cache/3gpp and
    never committed.
    """
    if not SCHEMA_CACHE.exists():
        request = Request(SCHEMA_URL, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
        with urlopen(request, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        SCHEMA_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_CACHE.write_bytes(archive.read("measCollec.xsd"))
    schema = etree.XMLSchema(etree.parse(str(SCHEMA_CACHE)))
    for _, content in tiny[2][::24]:
        document = etree.fromstring(gzip.decompress(content))
        assert schema.validate(document), schema.error_log.last_error


NOKIA = Ems("EMS-NK-01", NOKIA_R1, UTC, "Nokia-style synthetic EMS", "omes")


@pytest.fixture(scope="module")
def tiny_nokia() -> tuple[NetworkModel, Day, list[tuple[str, bytes]]]:
    model = default_model(build_world("tiny"))
    day = next(simulate_days(model, 0, 1))
    return model, day, list(day_files(model, day, NOKIA))


def test_nokia_files_are_utc(tiny_nokia: tuple[NetworkModel, Day, list[tuple[str, bytes]]]) -> None:
    _, day, files = tiny_nokia
    name, content = files[40]
    assert name == "OMeS_EMS-NK-01_20260105T0300Z.xml.gz"
    parsed = omes.parse_file(content)
    assert parsed["begin"].utcoffset() == timedelta(0)
    assert parsed["begin"] == day.starts[40].replace(tzinfo=WIB)
    assert parsed["interval_min"] == 15


def test_nokia_values_round_trip(
    tiny_nokia: tuple[NetworkModel, Day, list[tuple[str, bytes]]],
) -> None:
    model, day, files = tiny_nokia
    cols = omes.parse_file(files[40][1])["columns"]
    cell = int(
        np.flatnonzero((model.state.technology == "LTE") & (model.state.vendor == "nokia"))[0]
    )
    dn = nokia_dn(model, cell)
    column = int(np.flatnonzero(day.lte.cells == cell)[0])
    got = {
        c: v for d, c, v in zip(cols["dn"], cols["counter"], cols["value"], strict=True) if d == dn
    }
    assert got["M8013C17"] == day.lte.values["RRC.ConnEstabAtt.sum"][40, column]
    assert got["M8012C20"] == pytest.approx(day.lte.values["DRB.IPVolDl.sum"][40, column] / 8.0)
    assert got["M8011C37"] == day.lte.values["RRU.PrbTotDl"][40, column]
    assert got["M8020C3"] == 90.0


def test_nokia_relation_names_are_stable(
    tiny_nokia: tuple[NetworkModel, Day, list[tuple[str, bytes]]],
) -> None:
    model, day, _ = tiny_nokia
    source, target = next((s, t) for s, t in day.lte.relations if model.state.vendor[s] == "nokia")
    name = nokia_relation_dn(model, source, target)
    assert name.startswith(nokia_dn(model, source) + "/LNREL-")
    assert name == nokia_relation_dn(model, source, target)


def test_not_omes_fails() -> None:
    with pytest.raises(ValueError, match="not OMeS"):
        omes.parse_file(b"<measCollecFile/>")
    with pytest.raises(ValueError, match="Nokia-style"):
        omes.parse_file_name("B20260105.1500+0700-1515+0700_EMS.xml.gz")
