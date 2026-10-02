"""Real-data cross-check (rule E2): the fetch stays plain and verified, the
report comes from its record."""

import json
from pathlib import Path

import numpy as np
import pytest

from ran_lakehouse.crosscheck import fetch, report


def test_report_is_rendered_from_its_record() -> None:
    record = json.loads(report.RECORD_JSON.read_text())
    assert report.RECORD_MD.read_text() == report.render_markdown(record)


def test_the_download_request_carries_no_identifier() -> None:
    for name in fetch.FILES:
        r = fetch.request(name)
        assert r.headers == {}
        assert r.unredirected_hdrs == {}
        assert "?" not in r.full_url
        assert r.full_url == f"https://zenodo.org/api/records/{fetch.RECORD}/files/{name}/content"


def test_a_changed_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_bytes(b"not the published file")
    with pytest.raises(RuntimeError, match="MD5"):
        fetch.verified(path, fetch.FILES["README.md"])


def test_the_real_data_stays_out_of_git() -> None:
    import subprocess

    probe = fetch.DEST / "Dataset_03.zip"
    result = subprocess.run(["git", "check-ignore", "-q", str(probe)], check=False)
    assert result.returncode == 0


def test_best_shift_finds_a_known_shift() -> None:
    hours = np.arange(24)
    synthetic = 1.0 + np.sin(2 * np.pi * (hours - 14) / 24)
    real = np.roll(synthetic, -5)
    shift, r = report.best_shift(real, synthetic)
    assert shift == 5 and r == pytest.approx(1.0)


def test_spearman_handles_ties() -> None:
    x = np.array([1.0, 2.0, 2.0, 3.0])
    assert report.spearman(x, x) == pytest.approx(1.0)
    assert report.spearman(x, -x) == pytest.approx(-1.0)
