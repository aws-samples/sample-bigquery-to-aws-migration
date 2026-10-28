"""The bq-assess skill must find reports where the CLI actually writes them.

Since commit 6986deb (2026-07-22) the CLI wrote reports one folder deeper than the
skill looked (``reports/<project>_<date>/report/`` vs ``reports/``), so a skill
following its own instructions treated every successful run as a fatal error.
Nothing caught it for three months — the existing CLI-flag guard (#62) checks
flags, not file locations. This test ties the skill's text to report/run_layout.py.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bq_assess.report.run_layout import (
    POINTER_FILE_KEYS,
    latest_pointer_name,
    legacy_run_folder_name,
    run_folder_name,
)

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "bq-assess"
_PHASES = _SKILL / "references" / "phases"
_SKILL_DOCS = sorted(_SKILL.rglob("*.md"))
_T = datetime(2026, 10, 27, 13, 43, 5, tzinfo=timezone.utc)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_skill_docs_exist():
    assert (_PHASES / "scan.md").is_file()
    assert _SKILL_DOCS, "no skill docs found"


@pytest.mark.parametrize("doc", _SKILL_DOCS, ids=lambda p: p.name)
def test_no_skill_doc_looks_for_reports_directly_under_reports(doc):
    """The original bug: ``reports/assessment-landing-*.json`` never matches."""
    assert "reports/assessment-" not in _read(doc)


def test_scan_phase_reads_the_cli_pointer():
    assert latest_pointer_name("{gcp_project}") in _read(_PHASES / "scan.md")


def test_fallback_pattern_finds_both_current_and_legacy_runs():
    """The skill's fallback for pre-pointer CLIs must match real folder names,
    old and new — and must never be the primary path (it can't tell runs apart
    by name alone)."""
    scan = _read(_PHASES / "scan.md")
    match = re.search(r"`(reports/\{gcp_project\}_\*/report/assessment-landing-\*\.json)`", scan)
    assert match, "scan.md lost its fallback search pattern"
    pattern = match.group(1).replace("{gcp_project}", "acme-prod")

    for folder in (run_folder_name("acme-prod", _T), legacy_run_folder_name("acme-prod", _T)):
        path = f"reports/{folder}/report/assessment-landing-assess-20261027-abcd1234.json"
        assert fnmatch.fnmatch(path, pattern), f"fallback misses {path}"
    assert "newest `generated_at`" in scan, "fallback must choose by generated_at, not by name"


def test_skill_only_references_pointer_keys_the_cli_writes():
    referenced = set()
    for doc in _SKILL_DOCS:
        referenced |= set(re.findall(r"files\.([a-z_]+)", _read(doc)))
    assert referenced, "no skill doc reads the pointer's files object"
    unknown = referenced - set(POINTER_FILE_KEYS)
    assert not unknown, f"skill references pointer keys the CLI never writes: {unknown}"
