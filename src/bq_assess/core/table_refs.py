"""Shared table-reference extraction — the ONE grammar for "which tables does
this SQL touch?".

Three subsystems previously carried their own divergent regexes (2026-08-04
consolidation): the query-log analyzer (hub tables / query counts), the scanner
(view/MV dependency extraction), and query attribution (workload mapping).
Bugs fixed in one grammar — unbackticked 3-part refs, EXTRACT(x FROM y)
phantoms, dashed-project handling — were absent from the others. This module is
now the single source; call sites keep their own POLICY (cross-project
filtering, dedup, counting) but share the PARSE.

Covers, in table positions (FROM / JOIN / INTO / UPDATE / MERGE [INTO]):
- `project.dataset.table` (backticked, dashes allowed in project)
- project.dataset.table (unbackticked — legal BQ for dash-free projects)
- dataset.table, backticked or not
- bare single-part names (returned with dataset=None; most callers skip them)

Not a SQL parser: CTE names shadowing real tables, quoted exotic identifiers,
and comments containing SQL are out of scope for this best-effort grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Backtick-qualified refs anywhere in the statement (BQ requires the backtick
# form for dashed project IDs, and table positions are the only place these
# full paths legally appear).
_BACKTICK_3PART_RE = re.compile(
    r"`([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)`"
)
_BACKTICK_2PART_RE = re.compile(
    r"`([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)`"
)

# Unbackticked refs after a table-position keyword: 1, 2, or 3 dot-parts.
# The first part may carry dashes (a project ID); dataset/table parts cannot.
_KEYWORD_REF_RE = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE|MERGE\s+INTO?)\s+"
    r"(?!`)([a-zA-Z0-9_-]+)(?:\.([a-zA-Z0-9_]+))?(?:\.([a-zA-Z0-9_]+))?(?!`)",
    re.IGNORECASE,
)

# EXTRACT(part FROM expr) / TRIM([LEADING|...] x FROM y): that FROM is an
# expression position, not a table position. Detected by the function opener
# immediately preceding the match; a bare datepart word is NOT enough —
# 'SELECT day FROM ds.t' is a real table ref.
_NON_TABLE_FROM_RE = re.compile(
    r"(?:\bEXTRACT\s*\(\s*[A-Za-z]+\s+|\bTRIM\s*\((?:[^()]*\s)?)$",
    re.IGNORECASE,
)

# SQL keywords that can follow FROM/JOIN in non-table grammar (UNNEST(...),
# sub-selects). Single-part matches equal to these are noise, never tables.
_KEYWORD_NOISE = frozenset({
    "select", "unnest", "lateral", "values", "each", "the",
})


@dataclass(frozen=True)
class TableRef:
    """One table reference. dataset/project are None when the SQL didn't
    qualify them (bare name / dataset-local ref)."""
    project: str | None
    dataset: str | None
    table: str

    @property
    def dataset_table(self) -> str | None:
        """'dataset.table', or None for bare single-part refs."""
        if self.dataset is None:
            return None
        return f"{self.dataset}.{self.table}"


def extract_table_refs(sql: str, project_id: str | None = None) -> list[TableRef]:
    """All table refs in ``sql``, in match order, duplicates preserved.

    When ``project_id`` is given, refs explicitly qualified with a DIFFERENT
    project are dropped (cross-project workload/deps don't belong to this
    Source's entities); refs qualified with ``project_id`` itself are kept and
    normalized. Unqualified refs are always kept — they implicitly target the
    querying project.
    """
    if not sql:
        return []
    refs: list[TableRef] = []

    for m in _BACKTICK_3PART_RE.finditer(sql):
        proj, dataset, table = m.group(1), m.group(2), m.group(3)
        if project_id is not None and proj != project_id:
            continue
        refs.append(TableRef(project=proj, dataset=dataset, table=table))

    for m in _BACKTICK_2PART_RE.finditer(sql):
        refs.append(TableRef(project=None, dataset=m.group(1), table=m.group(2)))

    for m in _KEYWORD_REF_RE.finditer(sql):
        if _NON_TABLE_FROM_RE.search(sql, 0, m.start(0)):
            continue
        first, second, third = m.group(1), m.group(2), m.group(3)
        if third:
            # project.dataset.table
            if project_id is not None and first != project_id:
                continue
            refs.append(TableRef(project=first, dataset=second, table=third))
        elif second:
            # dataset.table — unless the first part carries a dash, which makes
            # it a project ref with the table part missing (datasets can't
            # contain dashes): skip those.
            if "-" not in first:
                refs.append(TableRef(project=None, dataset=first, table=second))
        else:
            # bare single-part name (alias/CTE/table) — callers decide
            if first.lower() not in _KEYWORD_NOISE and "-" not in first:
                refs.append(TableRef(project=None, dataset=None, table=first))

    return refs


def extract_dataset_tables(sql: str, project_id: str | None = None) -> set[str]:
    """Convenience: the distinct 'dataset.table' names referenced in ``sql``.

    Bare single-part refs are excluded (no dataset to qualify them with).
    """
    return {
        ref.dataset_table
        for ref in extract_table_refs(sql, project_id)
        if ref.dataset is not None
    }
