"""Unit tests for the BQ→Redshift translation pipeline (rewrite.py, 2026-07-16 rework).

Covers the regression cases named in the design review
(2026-07-16 translation deep-audit):
temporal AST rewrites to a fixpoint, the string-literal offset guard,
AST-based residual detection (no string-literal false positives),
idempotency, multi-statement isolation, and the confidence taxonomy.
"""
from __future__ import annotations

import pytest

from bq_assess.engine.redshift.rewrite import RewriteGuide


@pytest.fixture()
def guide():
    return RewriteGuide()


# ---- Fix 1: DATETIME() rewrites --------------------------------------------


class TestDatetimeRewrite:
    def test_datetime_tz_becomes_convert_timezone(self, guide):
        r = guide.translate("SELECT DATETIME(ts, 'Australia/Melbourne') AS mel FROM t")
        assert "CONVERT_TIMEZONE('Australia/Melbourne', ts)" in r.redshift_sql
        assert "DATETIME(" not in r.redshift_sql
        assert r.confidence == "MEDIUM"
        assert any("auto-converted" in w for w in r.warnings)

    def test_bare_datetime_becomes_cast(self, guide):
        r = guide.translate("SELECT DATETIME(ts) FROM t")
        assert "CAST(ts AS TIMESTAMP)" in r.redshift_sql
        assert "DATETIME(" not in r.redshift_sql

    def test_nested_datetime_timestamp_fully_converted(self, guide):
        # The fixpoint test: transform() prunes replaced nodes' children, so a
        # single pass leaves the inner TIMESTAMP(ts,'UTC') unconverted.
        r = guide.translate(
            "SELECT DATETIME(TIMESTAMP(ts, 'UTC'), 'Australia/Melbourne') FROM t"
        )
        assert "DATETIME(" not in r.redshift_sql
        assert "TIMESTAMP(" not in r.redshift_sql.replace("CONVERT_TIMEZONE", "")
        assert r.redshift_sql.count("CONVERT_TIMEZONE") == 2

    def test_datetime_two_part_form_warned_low(self, guide):
        r = guide.translate("SELECT DATETIME(d, tm) FROM t")
        assert r.confidence == "LOW"
        assert any("DATETIME(date, time)" in w for w in r.warnings)

    def test_datetime_six_int_form_warned_low(self, guide):
        # Parses as TimestampFromParts → MAKE_TIMESTAMP, which Redshift lacks.
        r = guide.translate("SELECT DATETIME(2008, 12, 25, 5, 30, 0) FROM t")
        assert r.confidence == "LOW"
        assert any("MAKE_TIMESTAMP" in w for w in r.warnings)


# ---- Fix 2: TIMESTAMP() rewrites --------------------------------------------


class TestTimestampRewrite:
    def test_timestamp_tz_becomes_three_arg_convert_timezone(self, guide):
        r = guide.translate("SELECT TIMESTAMP(dt, 'Australia/Melbourne') FROM t")
        assert "CONVERT_TIMEZONE('Australia/Melbourne', 'UTC', dt)" in r.redshift_sql
        assert r.confidence == "MEDIUM"
        assert any("absolute instant" in w for w in r.warnings)

    def test_timestamp_string_with_offset_preserved_as_timestamptz(self, guide):
        # D2 guard: plain-TIMESTAMP cast would discard the +11 (Redshift ignores
        # in-string zones); TIMESTAMPTZ preserves the instant.
        r = guide.translate("SELECT TIMESTAMP('2024-01-01 10:00:00+11') FROM t")
        assert "WITH TIME ZONE" in r.redshift_sql
        assert any("embedded" in w for w in r.warnings)
        assert r.confidence == "MEDIUM"

    def test_timestamp_string_with_named_zone_preserved(self, guide):
        r = guide.translate("SELECT TIMESTAMP('2024-01-01 10:00:00 Australia/Melbourne') FROM t")
        assert "WITH TIME ZONE" in r.redshift_sql

    def test_timestamp_plain_string_becomes_cast(self, guide):
        r = guide.translate("SELECT TIMESTAMP('2024-01-01 10:00:00') FROM t")
        assert "CAST('2024-01-01 10:00:00' AS TIMESTAMP)" in r.redshift_sql
        assert r.confidence == "HIGH"

    def test_bare_timestamp_column_becomes_cast(self, guide):
        r = guide.translate("SELECT TIMESTAMP(dt_col) FROM t")
        assert "CAST(dt_col AS TIMESTAMP)" in r.redshift_sql
        assert "TIMESTAMP(dt_col)" not in r.redshift_sql


# ---- Fix 3+4: residual detection (AST, not regex) ---------------------------


class TestResidualDetection:
    def test_struct_constructor_warned_low_with_super_guidance(self, guide):
        r = guide.translate("SELECT ARRAY_AGG(STRUCT(a, b)) FROM t GROUP BY c")
        assert r.confidence == "LOW"
        assert any("SUPER" in w for w in r.warnings)

    def test_struct_inside_string_literal_no_false_positive(self, guide):
        # The AST-walk rationale: 'STRUCT(' inside a string literal must not warn.
        r = guide.translate("SELECT 'call STRUCT(a, b) manually' AS note FROM t")
        assert r.confidence == "HIGH"
        assert r.warnings == []

    def test_datetime_inside_string_literal_no_false_positive(self, guide):
        r = guide.translate("SELECT 'DATETIME(x, tz) is a BQ function' AS doc FROM t")
        assert r.confidence == "HIGH"

    def test_safe_family_function_warned(self, guide):
        r = guide.translate("SELECT SAFE_MULTIPLY(a, b) FROM t")
        assert r.confidence == "LOW"
        assert any("SAFE_MULTIPLY" in w for w in r.warnings)

    def test_farm_fingerprint_raw_name_warned(self, guide):
        # sqlglot converts FARM_FINGERPRINT → FARMFINGERPRINT64 (valid Redshift);
        # this test pins the residual scan to the RAW name only, in case a future
        # sqlglot regression passes it through.
        r = guide.translate("SELECT FARM_FINGERPRINT(x) FROM t")
        if "FARM_FINGERPRINT(" in r.redshift_sql:
            assert r.confidence == "LOW"
        else:
            assert "FARMFINGERPRINT64" in r.redshift_sql
            assert r.confidence == "HIGH"


# ---- Pipeline behaviors ------------------------------------------------------


class TestPipeline:
    def test_idempotent_on_translated_output(self, guide):
        first = guide.translate("SELECT DATETIME(ts, 'Australia/Melbourne') FROM t")
        second = guide.translate(first.redshift_sql)
        assert second.redshift_sql == first.redshift_sql
        assert second.confidence == "HIGH"   # nothing left to convert or warn about

    def test_multi_statement_all_translated(self, guide):
        r = guide.translate(
            "SELECT DATETIME(a, 'UTC') FROM t; SELECT DATETIME(b, 'UTC') FROM u"
        )
        assert r.redshift_sql.count("CONVERT_TIMEZONE") == 2
        assert "; " in r.redshift_sql

    def test_parse_failure_preserved_behavior(self, guide):
        r = guide.translate("return fuzzball.distance(a,b);")
        assert r.redshift_sql.startswith("-- [TRANSLATION FAILED")
        assert r.confidence == "LOW"

    def test_empty_sql(self, guide):
        r = guide.translate("")
        assert r.confidence == "LOW"

    def test_js_udf_still_low(self, guide):
        r = guide.translate(
            "CREATE FUNCTION f(x STRING) RETURNS STRING LANGUAGE js AS 'return x;'"
        )
        assert r.confidence == "LOW"
        assert any("Lambda" in w for w in r.warnings)

    def test_clean_sql_stays_high_and_unchanged_semantics(self, guide):
        r = guide.translate("SELECT a, b FROM t WHERE c = 1")
        assert r.confidence == "HIGH"
        assert r.warnings == []

    def test_warnings_deduped(self, guide):
        r = guide.translate(
            "SELECT DATETIME(a, 'UTC'), DATETIME(b, 'UTC'), DATETIME(c, 'UTC') FROM t"
        )
        datetime_warnings = [w for w in r.warnings if "CONVERT_TIMEZONE(tz, ts)" in w]
        assert len(datetime_warnings) == 1


# ---- Confidence taxonomy -----------------------------------------------------


class TestConfidenceTaxonomy:
    def test_auto_converted_only_is_medium(self, guide):
        r = guide.translate("SELECT DATETIME(ts, 'UTC') FROM t")
        assert r.confidence == "MEDIUM"

    def test_mixed_auto_and_manual_is_low(self, guide):
        r = guide.translate(
            "SELECT DATETIME(ts, 'UTC'), STRUCT(a, b) FROM t"
        )
        assert r.confidence == "LOW"

    def test_safe_divide_converted_cleanly(self, guide):
        # sqlglot 30.x converts SAFE_DIVIDE to CASE WHEN y <> 0 ... ELSE NULL END —
        # correct NULL semantics, no residual, so HIGH.
        r = guide.translate("SELECT SAFE_DIVIDE(a, b) FROM t")
        assert "SAFE_DIVIDE" not in r.redshift_sql
        assert "ELSE NULL" in r.redshift_sql or "NULLIF" in r.redshift_sql
        assert r.confidence == "HIGH"


# ---- Deep-audit fixes (2026-07-16 build deep-audit) ---------------------


class TestZSuffixFix:
    def test_timestamp_z_suffix_becomes_timestamptz(self, guide):
        r = guide.translate("SELECT TIMESTAMP('2024-01-01T10:00:00Z') FROM t")
        assert "WITH TIME ZONE" in r.redshift_sql
        assert any("embedded" in w for w in r.warnings)
        assert r.confidence == "MEDIUM"

    def test_timestamp_z_suffix_no_space(self, guide):
        r = guide.translate("SELECT TIMESTAMP('2024-01-01 10:00:00Z') FROM t")
        assert "WITH TIME ZONE" in r.redshift_sql

    def test_timestamp_without_z_still_plain_cast(self, guide):
        r = guide.translate("SELECT TIMESTAMP('2024-01-01 10:00:00') FROM t")
        assert "WITHOUT TIME ZONE" not in r.redshift_sql or "WITH TIME ZONE" not in r.redshift_sql
        assert "CAST('2024-01-01 10:00:00' AS TIMESTAMP)" in r.redshift_sql
        assert r.confidence == "HIGH"


class TestExpandedInvalidFuncs:
    def test_datetime_trunc_warned_low(self, guide):
        r = guide.translate("SELECT DATETIME_TRUNC(dt, MONTH) FROM t")
        assert r.confidence == "LOW"
        assert any("DATETIME_TRUNC" in w for w in r.warnings)

    def test_datetime_add_warned_low(self, guide):
        r = guide.translate("SELECT DATETIME_ADD(dt, INTERVAL 1 DAY) FROM t")
        assert r.confidence == "LOW"
        assert any("DATETIME_ADD" in w or "DATEADD" in w for w in r.warnings)

    def test_regexp_extract_all_warned_low(self, guide):
        r = guide.translate("SELECT REGEXP_EXTRACT_ALL(col, '[0-9]+') FROM t")
        assert r.confidence == "LOW"
        assert any("REGEXP_EXTRACT_ALL" in w for w in r.warnings)

    def test_format_warned_low(self, guide):
        r = guide.translate("SELECT FORMAT('%d items', cnt) FROM t")
        assert r.confidence == "LOW"
        assert any("FORMAT" in w for w in r.warnings)


class TestStringLiteralNoCorruption:
    def test_safe_divide_in_string_not_rewritten(self, guide):
        r = guide.translate("SELECT 'Use SAFE_DIVIDE(a, b) for division' AS help FROM t")
        assert "SAFE_DIVIDE(a, b)" in r.redshift_sql
        assert r.confidence == "HIGH"
        assert r.warnings == []

    def test_timestampdiff_in_string_not_rewritten(self, guide):
        r = guide.translate("SELECT 'call TIMESTAMPDIFF(a, b, DAY)' AS help FROM t")
        assert "TIMESTAMPDIFF(a, b, DAY)" in r.redshift_sql
        assert r.confidence == "HIGH"


class TestProjectQualifierStripping:
    """2026-08-04 audit: every shipped rebuilt view kept its 3-part BigQuery
    project qualifier ('pdp22--playa--data--prd'.standardized.t) — a catalog
    that does not exist on the target. The translator must strip it."""

    def test_backticked_three_part_ref_stripped(self):
        guide = RewriteGuide()
        r = guide.translate(
            "SELECT * FROM `my-proj.standardized.inf_game_rounds` WHERE x > 1"
        )
        assert "my-proj" not in r.redshift_sql
        assert '"standardized"."inf_game_rounds"' in r.redshift_sql
        # mechanical normalization — no warning, confidence unaffected
        assert r.confidence == "HIGH"

    def test_two_part_ref_untouched(self):
        guide = RewriteGuide()
        r = guide.translate("SELECT * FROM ds.t")
        assert "ds.t" in r.redshift_sql
        assert r.confidence == "HIGH"


class TestTimestampAddSub:
    """2026-08-04 audit: sqlglot passes TIMESTAMP_SUB through verbatim
    ('TIMESTAMP_SUB(GETDATE(), '1', HOUR)' — invalid Redshift, unflagged)."""

    def test_timestamp_sub_becomes_negative_dateadd(self):
        r = RewriteGuide().translate(
            "SELECT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)"
        )
        assert "DATEADD(HOUR, -1, GETDATE())" in r.redshift_sql
        assert "TIMESTAMP_SUB" not in r.redshift_sql

    def test_timestamp_add_becomes_dateadd(self):
        r = RewriteGuide().translate("SELECT TIMESTAMP_ADD(ts, INTERVAL 2 DAY)")
        assert "DATEADD(DAY, 2, ts)" in r.redshift_sql
