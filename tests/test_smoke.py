"""Smoke test: the package imports and the CLI runs."""

import shutil
import subprocess

import pytest

import ran_lakehouse
from ran_lakehouse.cli import main


def test_package_has_version() -> None:
    assert ran_lakehouse.__version__


def test_version_command_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == ran_lakehouse.__version__


def test_no_command_is_an_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_console_script_help() -> None:
    exe = shutil.which("ranlake")
    assert exe is not None, "ranlake console script is not installed"
    result = subprocess.run([exe, "--help"], capture_output=True, text=True, check=True)
    assert "usage: ranlake" in result.stdout
