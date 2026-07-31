"""Tests for Athena rewrite guide (BQ→Trino translation)."""
from __future__ import annotations

import pytest

from bq_assess.engine.athena.rewrite import AthenaRewriteGuide


@pytest.fixture
def guide():
    return AthenaRewriteGuide()


def test_simple_select_translates(guide):
    result = guide.translate("SELECT col1, col2 FROM dataset.my_table")
    assert result.engine_id == "athena"
    assert "col1" in result.translated_sql
    assert result.confidence == "HIGH"


def test_safe_divide_rewritten(guide):
    result = guide.translate("SELECT SAFE_DIVIDE(a, b) FROM t")
    assert "SAFE_DIVIDE" not in result.translated_sql
    assert result.confidence in ("HIGH", "MEDIUM")


def test_merge_produces_warning_not_blocker(guide):
    """MERGE is supported on Athena engine v3 (merge-on-read) — warning only."""
    sql = "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.x = s.x"
    result = guide.translate(sql)
    # Should have a warning about merge-on-read/compaction
    assert any("merge" in w.lower() and ("engine v3" in w or "compaction" in w.lower()) for w in result.warnings)
    # Should NOT be in unsupported_constructs
    assert not any("MERGE" in u for u in result.unsupported_constructs)
    # Confidence should NOT be LOW solely due to MERGE
    # (it may be LOW if parse failed, but not from MERGE detection alone)
    if result.confidence == "LOW":
        # If LOW, it must be from parse failure, not MERGE
        assert any("parse" in w.lower() or "translation failed" in w.lower() for w in result.warnings)


def test_struct_constructor_translated(guide):
    result = guide.translate("SELECT STRUCT(1 AS a, 'b' AS b)")
    assert "ROW" in result.translated_sql
    assert result.confidence in ("HIGH", "MEDIUM")


def test_geography_flagged(guide):
    result = guide.translate("SELECT ST_GEOGPOINT(lng, lat) FROM t")
    assert any("geog" in w.lower() or "geography" in w.lower() or "ST_GEOGPOINT" in w
               for w in result.warnings + result.unsupported_constructs)
    assert result.confidence == "LOW"


def test_empty_sql_returns_low(guide):
    result = guide.translate("")
    assert result.confidence == "LOW"


def test_qualify_rewritten(guide):
    sql = "SELECT * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1"
    result = guide.translate(sql)
    assert "QUALIFY" not in result.translated_sql


def test_procedural_script_is_blocker_not_high(guide):
    """BQ scripting (BEGIN/DECLARE) can never run on Athena — must be LOW with a
    BLOCKER, never HIGH (2026-07-30 live-verification finding #3)."""
    sql = "BEGIN DECLARE d DATE DEFAULT CURRENT_DATE(); SET d = CURRENT_DATE(); END"
    result = guide.translate(sql)
    assert result.confidence == "LOW"
    assert any("procedural script" in u for u in result.unsupported_constructs)


def test_reserved_word_user_gets_quoted(guide):
    """`user` is reserved in Trino/Athena — struct-path references must be quoted
    (finding #5)."""
    result = guide.translate("SELECT e.user.user_id FROM ds.events AS e")
    assert 'e."user".user_id' in result.translated_sql
    assert any("reserved" in w for w in result.warnings)


def test_ignore_nulls_distinct_filter_is_valid(guide):
    """ARRAY_AGG(DISTINCT x IGNORE NULLS): the FILTER predicate must not carry the
    DISTINCT keyword (finding #6 — `WHERE DISTINCT x` is a syntax error)."""
    result = guide.translate(
        "SELECT ARRAY_AGG(DISTINCT e.event_type IGNORE NULLS) FROM ds.events AS e"
    )
    assert "WHERE DISTINCT" not in result.translated_sql
    assert "FILTER (WHERE e.event_type IS NOT NULL)" in result.translated_sql
    # the aggregate itself keeps DISTINCT
    assert "ARRAY_AGG(DISTINCT e.event_type)" in result.translated_sql
