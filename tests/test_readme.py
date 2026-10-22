"""The examples in README.md must stay valid, strictly typed Python."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mypy import api

README = Path(__file__).parent.parent / "README.md"
PREAMBLE = "from gcpapi_sync import GarminClient\n\ngarmin = GarminClient()\n"


def python_blocks() -> list[str]:
    chunks = README.read_text(encoding="utf-8").split("```python\n")[1:]
    return [chunk.split("```", 1)[0] for chunk in chunks]


def as_module(block: str) -> str:
    """Give snippets that use a ``garmin`` client without creating one a definition."""
    defines_client = re.search(r"\bgarmin\b\s*(=|:)|as garmin\b", block)
    return block if defines_client or "garmin" not in block else PREAMBLE + block


@pytest.mark.parametrize("block", python_blocks())
def test_examples_compile(block: str) -> None:
    compile(as_module(block), "README.md", "exec")


def test_examples_pass_strict_type_checking(tmp_path: Path) -> None:
    for index, block in enumerate(python_blocks()):
        (tmp_path / f"example_{index}.py").write_text(as_module(block), encoding="utf-8")

    stdout, stderr, status = api.run(
        ["--strict", "--no-error-summary", "--cache-dir", str(tmp_path / ".cache"), str(tmp_path)]
    )

    assert status == 0, stdout + stderr
