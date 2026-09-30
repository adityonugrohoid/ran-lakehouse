"""The API contract (rule A1): one semver version for the OpenAPI document
and the plan constraint schema, committed under contract/.

The OpenAPI document is generated from the code and committed; a test
fails when the two drift. Run this module to rewrite it after a change:

    uv run python -m ran_lakehouse.api.contract
"""

import json
from pathlib import Path
from typing import Any

# Breaking changes bump the major version (rule A1).
CONTRACT_VERSION = "1.0.0"
VERSION_HEADER = "X-Contract-Version"
CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contract"
OPENAPI_PATH = CONTRACT_DIR / "openapi.json"
PLAN_SCHEMA_PATH = CONTRACT_DIR / "plan_constraints.schema.json"


def plan_schema() -> dict[str, Any]:
    """The plan constraint schema, checked to carry the contract version.

    Returns:
        The JSON Schema.

    Raises:
        RuntimeError: If its version is not CONTRACT_VERSION.
    """
    schema: dict[str, Any] = json.loads(PLAN_SCHEMA_PATH.read_text())
    version = schema["properties"]["schema_version"]["const"]
    if version != CONTRACT_VERSION or not schema["$id"].endswith(f"/{CONTRACT_VERSION}"):
        raise RuntimeError(
            f"{PLAN_SCHEMA_PATH.name} is version {version}, the contract is {CONTRACT_VERSION}"
        )
    return schema


def render(document: dict[str, Any]) -> str:
    """The committed form of the OpenAPI document.

    Args:
        document: The document.

    Returns:
        Stable JSON text.
    """
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> None:
    """Write contract/openapi.json from the code."""
    from ran_lakehouse.api.app import create_app

    OPENAPI_PATH.write_text(render(create_app().openapi()))


if __name__ == "__main__":
    main()
