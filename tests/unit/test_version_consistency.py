"""The package version is declared in three places — assert they agree.

Nothing enforced this before 0.9.0, and the version is no longer cosmetic:
`tool_version` is now stamped into every artifact the tool emits (#61), and a
bundle records `collector_version`. If the three declarations drift, two reports
can claim the same version while being built from different code — which is the
exact failure #61 exists to prevent, one field over.

The three sources:
  pyproject.toml                      the installed `bq-assess` distribution
  src/bq_assess/__init__.py           what __version__ (and so every artifact) reports
  packaging/collector/pyproject.toml  the separately-shipped `bq-collect` wheel,
                                      built from this same source tree
"""
from __future__ import annotations

import re
from pathlib import Path

from bq_assess import __version__

_REPO = Path(__file__).resolve().parents[2]
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def _declared_version(pyproject: Path) -> str:
    text = pyproject.read_text(encoding="utf-8")
    # Only the [project] table's version — not a dependency pin further down.
    project_block = text.split("[project]", 1)[1].split("\n[", 1)[0]
    match = _VERSION_RE.search(project_block)
    assert match, f"no [project] version found in {pyproject}"
    return match.group(1)


def test_main_pyproject_matches_dunder_version():
    assert _declared_version(_REPO / "pyproject.toml") == __version__


def test_collector_pyproject_matches_dunder_version():
    """The collector wheel is built from this tree, so a mismatch would make a
    bundle's collector_version point at code that never built it."""
    assert _declared_version(
        _REPO / "packaging" / "collector" / "pyproject.toml"
    ) == __version__


def test_version_is_pep440_three_part():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_report_schema_version_is_independent_of_the_package_version():
    """REPORT_SCHEMA_VERSION must NOT track the release number.

    Decoupling them was the point of #61: tied to the package version it moved
    on releases that changed nothing structural, and stayed put when field
    semantics genuinely shifted. Bumping both together out of habit would
    quietly re-link them, so assert the types can never be confused.
    """
    from bq_assess.report.provenance import REPORT_SCHEMA_VERSION

    assert isinstance(REPORT_SCHEMA_VERSION, int)
    assert str(REPORT_SCHEMA_VERSION) != __version__
