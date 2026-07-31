# Feature: bq-assess-lakehouse, issue 5.x review fix: shared JOBS reader
"""Unit tests for the shared INFORMATION_SCHEMA.JOBS reader (core/jobs_query.py).

One place builds the project-wide job-history query that PricingDetector (5.1) and
WorkloadAnalyzer (5.2) both read — using JOBS_BY_PROJECT (not the per-user JOBS alias),
project- and region-qualified, with the completed-query lookback filter — and runs it under
the scanner's retry, degrading to [] on any error (never raises).
"""

from __future__ import annotations

from google.api_core.exceptions import Forbidden

from bq_assess.core.jobs_query import read_jobs


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return iter(self._rows)


class _FakeClient:
    def __init__(self, *, rows=None, error=None):
        self._rows = rows if rows is not None else []
        self._error = error
        self.queries: list[str] = []

    def query(self, sql, *a, **k):
        self.queries.append(sql)
        if self._error is not None:
            raise self._error
        return _FakeQueryJob(self._rows)


def test_query_uses_jobs_by_project_not_per_user_alias() -> None:
    """The whole-project view, not the bare per-user JOBS alias (review #1)."""
    c = _FakeClient(rows=[{"x": 1}])
    read_jobs(c, "proj", "SELECT reservation_id", days=30, location="US")
    sql = c.queries[0]
    assert "INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
    # Project- and region-qualified.
    assert "`proj`" in sql
    assert "`region-us`" in sql


def test_query_filters_completed_queries_over_lookback() -> None:
    """WHERE bounds to completed QUERY jobs over the lookback, excluding SCRIPT (review #2)."""
    c = _FakeClient(rows=[])
    read_jobs(c, "proj", "SELECT reservation_id", days=14, location="US")
    sql = c.queries[0]
    assert "job_type = 'QUERY'" in sql
    assert "state = 'DONE'" in sql
    assert "INTERVAL 14 DAY" in sql
    assert "statement_type != 'SCRIPT'" in sql


def test_returns_rows_on_success() -> None:
    rows = [{"a": 1}, {"a": 2}]
    assert read_jobs(_FakeClient(rows=rows), "proj", "SELECT a") == rows


def test_degrades_to_empty_on_error_never_raises() -> None:
    """Missing perms / any error → [] (callers treat no-signal as no-data), never raises."""
    c = _FakeClient(error=Forbidden("403 bigquery.jobs.listAll"))
    assert read_jobs(c, "proj", "SELECT a") == []


def test_select_clause_is_caller_supplied() -> None:
    """The caller supplies only the SELECT list; the FROM/WHERE skeleton is shared."""
    c = _FakeClient(rows=[])
    read_jobs(c, "proj", "SELECT total_slot_ms, creation_time")
    assert "SELECT total_slot_ms, creation_time" in c.queries[0]


def test_retryable_error_exhausts_retries_then_degrades_to_empty() -> None:
    """A retryable error (429/500/503) that persists through all retry attempts still
    degrades to [] — the retry logic re-raises on the final attempt, caught by the outer
    except in read_jobs (R16.3/R17.3 graceful degradation)."""
    from unittest.mock import patch

    from google.api_core.exceptions import ServiceUnavailable

    call_count = 0

    class _RetryClient:
        def query(self, sql, *a, **k):
            nonlocal call_count
            call_count += 1
            raise ServiceUnavailable("503 Backend unavailable")

    # _retry_query delegates to the scanner's shared ladder (2026-07-28), so the
    # sleep to patch lives in core.scanner now.
    with patch("bq_assess.core.scanner.time.sleep"):
        result = read_jobs(_RetryClient(), "proj", "SELECT a")

    assert result == []
    assert call_count == 4  # initial + 3 retries (server-class budget)


# --- Query-shape dedup (2026-07-28 scale review finding #6) ----------------------------


class TestQueryShapeKey:
    """The server-side GROUP BY key must group literal variants of one template
    into one shape — exact-text grouping let 1M literal variants of a few
    hundred templates crowd every real shape out of the 50k cap."""

    @staticmethod
    def _sql_shape(q: str) -> str:
        """Python simulation of QUERY_SHAPE_SQL's three re2 REGEXP_REPLACEs.

        The numeric replace runs twice — re2 consumes the trailing delimiter,
        so one pass skips every second literal in a comma-separated run."""
        import re
        num = r"([\s=><!(,+*/-])(\d+\.?\d*)([\s,);]|$)"
        q = re.sub(r"'[^']*'", "'?'", q)
        q = re.sub(num, r"\1?\3", q)
        q = re.sub(num, r"\1?\3", q)
        return q

    def test_literal_variants_collapse_to_one_shape(self) -> None:
        a = self._sql_shape("SELECT a FROM ds.t WHERE x = 'foo' AND y = 42")
        b = self._sql_shape("SELECT a FROM ds.t WHERE x = 'bar' AND y = 99999")
        assert a == b

    def test_identifiers_with_digits_survive(self) -> None:
        s = self._sql_shape("SELECT col1, col2 FROM table3 WHERE id2 > 5")
        assert "col1" in s and "table3" in s and "id2" in s
        assert s.endswith("> ?")

    def test_distinct_shapes_stay_distinct(self) -> None:
        s1 = self._sql_shape("SELECT a FROM t WHERE x = 'v'")
        s2 = self._sql_shape("SELECT b FROM t WHERE x = 'v'")
        assert s1 != s2

    def test_in_list_literals_fully_normalize(self) -> None:
        """The double-pass numeric replace catches odd positions in literal
        runs — one pass left IN (1,2,3) as (?,2,?), grouping per residual
        value (MR E review follow-up)."""
        a = self._sql_shape("SELECT x FROM t WHERE k IN (1,2,3,4,5,6)")
        b = self._sql_shape("SELECT x FROM t WHERE k IN (11,22,33,44,55,66)")
        assert a == b
        assert "IN (?,?,?,?,?,?)" in a

    def test_same_shape_group_anonymizes_identically(self) -> None:
        """THE invariant the cross-region dedup relies on: any two texts in one
        SQL shape group must Python-anonymize to the same exported string —
        ANY_VALUE may pick different representatives per region, and
        _queries_from_api_multi dedups on the anonymized text."""
        from bq_assess.core.analyzer import QueryAnalyzer

        py = QueryAnalyzer().anonymize_query
        variant_pairs = [
            ("SELECT a FROM t WHERE x = 'foo' AND y = 42",
             "SELECT a FROM t WHERE x = 'barbaz' AND y = 99999"),
            ("UPDATE t SET v = 5 WHERE k IN (10, 20, 30)",
             "UPDATE t SET v = 7 WHERE k IN (1, 2, 3)"),
            ("SELECT * FROM t WHERE d BETWEEN '2026-01-01' AND '2026-02-01' LIMIT 100",
             "SELECT * FROM t WHERE d BETWEEN '1999-12-31' AND '2000-01-31' LIMIT 5"),
        ]
        for a, b in variant_pairs:
            assert self._sql_shape(a) == self._sql_shape(b), "not one shape group"
            assert py(a) == py(b), (
                f"same shape group but different exported text:\n{py(a)}\n{py(b)}"
            )

    def test_group_by_uses_shape_key_and_carries_representative(self) -> None:
        """The emitted SQL groups by the normalized shape and exports
        ANY_VALUE(query) so the client-side anonymizer still gets real text."""
        from bq_assess.core.jobs_query import read_jobs_queries

        c = _FakeClient(rows=[])
        read_jobs_queries(c, "proj", location="EU")
        sql = c.queries[0]
        assert "REGEXP_REPLACE" in sql
        assert "AS query_shape" in sql
        assert "ANY_VALUE(query) AS query" in sql
        assert "GROUP BY query_shape" in sql
        assert "ORDER BY total_slot_ms DESC" in sql
