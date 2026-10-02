"""The compose app's bootstrap (rule S3): settings are required, never guessed."""

import pytest

from ran_lakehouse import bootstrap


def test_a_missing_setting_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RANLAKE_WAREHOUSE", raising=False)
    with pytest.raises(RuntimeError, match="RANLAKE_WAREHOUSE is not set"):
        bootstrap.setting("RANLAKE_WAREHOUSE")
    monkeypatch.setenv("RANLAKE_WAREHOUSE", "")
    with pytest.raises(RuntimeError, match="not set"):
        bootstrap.setting("RANLAKE_WAREHOUSE")
    monkeypatch.setenv("RANLAKE_WAREHOUSE", "compose-demo")
    assert bootstrap.setting("RANLAKE_WAREHOUSE") == "compose-demo"


def test_compose_sets_every_setting() -> None:
    from pathlib import Path

    compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
    app = compose[compose.index("\n  app:\n") :]
    for name in (
        "RANLAKE_WAREHOUSE",
        "RANLAKE_PROFILE",
        "RANLAKE_HISTORY_WEEKS",
        "RANLAKE_RUN_WEEKS",
        "RANLAKE_PORT",
    ):
        assert f"      {name}: " in app, name
