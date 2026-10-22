"""Package-level guarantees: public API, sync/async parity, spec coverage."""

from __future__ import annotations

import inspect
import re
import tomllib
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

import gcpapi_sync
from gcpapi_sync import AsyncGarminClient, GarminClient
from tests.cases import CASES, NOT_ENDPOINTS, Case

ROOT = Path(__file__).parent.parent
SPEC = ROOT / "spec" / "endpoints.json"


class SpecParameter(BaseModel):
    name: str
    location: str = Field(alias="in")
    required: bool
    type: str


class SpecEndpoint(BaseModel):
    method: str
    path: str
    parameters: list[SpecParameter]
    response: str | None


class SpecSurface(BaseModel):
    source: str
    endpoints: list[SpecEndpoint]


def public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_public_names_are_importable() -> None:
    for name in gcpapi_sync.__all__:
        assert getattr(gcpapi_sync, name) is not None, name


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["version"])


def test_version_is_0_minor_garmin_date() -> None:
    """Versions are 0.MINOR.YYYYMMDD (see scripts/bump_version.py)."""
    version = project_version()
    match = re.fullmatch(r"0\.(?P<minor>\d+)\.(?P<day>\d{8})", version)
    assert match is not None, f"{version} is not 0.MINOR.YYYYMMDD"
    day = datetime.strptime(match["day"], "%Y%m%d").replace(tzinfo=UTC).date()
    assert day <= datetime.now(UTC).date() + timedelta(days=1), "the date lies in the future"
    assert int(match["minor"]) >= 1
    assert gcpapi_sync.__version__ == version


def test_changelog_describes_this_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"^## \[(?P<version>[^\]]+)\] - \d{4}-\d{2}-\d{2}$", changelog, re.MULTILINE)

    assert top is not None, "CHANGELOG.md has no version section"
    assert top["version"] == project_version(), "run scripts/bump_version.py to update it"


def test_package_is_typed() -> None:
    assert resources.files("gcpapi_sync").joinpath("py.typed").is_file()


def test_sync_and_async_clients_have_the_same_interface() -> None:
    sync_methods = public_methods(GarminClient)
    async_methods = public_methods(AsyncGarminClient)
    renamed = {"close": "aclose"}

    assert {renamed.get(name, name) for name in sync_methods} == set(async_methods)
    for name, signature in sync_methods.items():
        async_name = renamed.get(name, name)
        assert signature.parameters == async_methods[async_name].parameters, name
        member = getattr(AsyncGarminClient, async_name)
        if name == "iter_activities":
            assert inspect.isasyncgenfunction(member)
        else:
            assert inspect.iscoroutinefunction(member), name


def test_every_endpoint_method_has_a_test_case() -> None:
    tested = {case.method for case in CASES}
    endpoint_methods = set(public_methods(GarminClient)) - NOT_ENDPOINTS

    assert tested == endpoint_methods


def spec_surface() -> SpecSurface:
    return SpecSurface.model_validate_json(SPEC.read_bytes())


def path_pattern(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$")


@pytest.mark.parametrize("endpoint", spec_surface().endpoints, ids=lambda endpoint: endpoint.path)
def test_every_documented_endpoint_is_wrapped(endpoint: SpecEndpoint) -> None:
    """Fails when Garmin adds an endpoint (spec/endpoints.json is refreshed weekly)."""
    pattern = path_pattern(endpoint.path)

    assert endpoint.method == "GET"
    assert any(pattern.match(case.path) for case in CASES), (
        f"no client method calls {endpoint.path}"
    )


@pytest.mark.parametrize(
    "case", [case for case in CASES if case.method != "get_json"], ids=lambda case: case.method
)
def test_only_documented_query_parameters_are_sent(case: Case) -> None:
    (endpoint,) = [
        endpoint
        for endpoint in spec_surface().endpoints
        if path_pattern(endpoint.path).match(case.path)
    ]
    documented = {
        parameter.name for parameter in endpoint.parameters if parameter.location == "query"
    }

    assert {name for name, _ in case.params} <= documented
