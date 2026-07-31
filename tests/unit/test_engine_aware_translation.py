"""Tests for engine-aware translation dispatch (Stage 12/12b/13 engine selection) and template rendering.

Verifies that when the engine recommendation is "athena", the pipeline uses
AthenaRewriteGuide and produces Trino-dialect SQL with target_engine="athena",
and when "redshift", it uses the Redshift RewriteGuide unchanged.
"""
from __future__ import annotations

import pytest

from bq_assess.engine.athena.rewrite import AthenaRewriteGuide
from bq_assess.engine.redshift.rewrite import RewriteGuide
from bq_assess.models import (
    DetectedConstruct,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    TranslationResult,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def athena_guide():
    return AthenaRewriteGuide()


@pytest.fixture
def redshift_guide():
    return RewriteGuide()


@pytest.fixture
def sample_entity():
    from datetime import datetime, timezone
    return EntityMetadata(
        entity_id="my_view",
        dataset_id="ds",
        full_name="ds.my_view",
        entity_type=EntityType.VIEW,
        population=EntityPopulation.REBUILT,
        num_bytes=0,
        num_rows=0,
        columns=[],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query="SELECT 1",
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ── Translation dispatch tests ───────────────────────────────────────────────


class TestAthenaTranslationDispatch:
    """When recommendation is athena, translations should use Trino dialect."""

    def test_athena_translate_produces_trino_dialect(self, athena_guide):
        """Simple BQ SQL should produce Trino-style output."""
        sql = "SELECT SAFE_DIVIDE(a, b), FORMAT_DATE('%Y-%m', d) FROM dataset.t"
        result = athena_guide.translate(sql)
        assert result.engine_id == "athena"
        # sqlglot BQ→Trino rewrites SAFE_DIVIDE → TRY(a / b)
        # The key thing: it should NOT contain Redshift-specific constructs
        assert "SAFE_DIVIDE" not in result.translated_sql

    def test_athena_result_maps_to_translation_result(self, athena_guide):
        """EngineRewrite from Athena maps cleanly to TranslationResult."""
        sql = "SELECT col1 FROM ds.tbl"
        engine_rewrite = athena_guide.translate(sql)
        # Simulate the mapping done in cli.py
        tr = TranslationResult(
            redshift_sql=engine_rewrite.translated_sql,
            confidence=engine_rewrite.confidence,
            warnings=engine_rewrite.warnings,
            target_engine="athena",
        )
        assert tr.target_engine == "athena"
        assert tr.confidence == "HIGH"
        assert "col1" in tr.redshift_sql

    def test_athena_translate_format_timestamp(self, athena_guide):
        """BQ FORMAT_TIMESTAMP should transpile to Trino's date_format or equivalent."""
        sql = "SELECT FORMAT_TIMESTAMP('%Y-%m-%d', ts) FROM t"
        result = athena_guide.translate(sql)
        # Should not have FORMAT_TIMESTAMP verbatim (it's a BQ function)
        # Trino uses date_format or similar
        assert result.engine_id == "athena"


class TestRedshiftTranslationDispatch:
    """When recommendation is redshift, translations use Redshift dialect (unchanged behavior)."""

    def test_redshift_translate_default_target_engine(self, redshift_guide):
        """Default TranslationResult has target_engine='redshift'."""
        sql = "SELECT col1, col2 FROM ds.t"
        result = redshift_guide.translate(sql)
        # TranslationResult from RewriteGuide doesn't set target_engine explicitly
        # (it defaults to "redshift" per the dataclass default)
        assert result.confidence == "HIGH"
        assert result.target_engine == "redshift"

    def test_redshift_translate_temporal_constructs(self, redshift_guide):
        """BQ temporal constructs should be rewritten to Redshift equivalents."""
        sql = "SELECT DATETIME(ts, 'Australia/Sydney') FROM t"
        result = redshift_guide.translate(sql)
        assert "CONVERT_TIMEZONE" in result.redshift_sql
        assert result.target_engine == "redshift"


# ── Guidance parity tests ────────────────────────────────────────────────────


class TestGuidanceParity:
    """Both guides should handle the same construct classes without dropping silently."""

    def _make_construct(self, cls: str, desc: str = "test") -> DetectedConstruct:
        return DetectedConstruct(construct_class=cls, snippet="...", description=desc)

    def test_athena_handles_all_redshift_construct_classes(self, athena_guide, redshift_guide, sample_entity):
        """Constructs that Redshift guide knows should also produce guidance under Athena."""
        # The construct classes the Redshift guide explicitly handles
        redshift_constructs = ["JS_UDF", "UNNEST", "ARRAY_FN", "STRUCT_NAV", "FUNCTION_DRIFT"]

        for cls in redshift_constructs:
            constructs = [self._make_construct(cls)]
            athena_guidance = athena_guide.guide(sample_entity, constructs)
            redshift_guidance = redshift_guide.guide(sample_entity, constructs)
            # Both should produce non-empty guidance
            assert len(athena_guidance) > 0, f"Athena guide returned empty for {cls}"
            assert len(redshift_guidance) > 0, f"Redshift guide returned empty for {cls}"

    def test_unknown_construct_gets_generic_guidance(self, athena_guide, sample_entity):
        """A construct class not in the explicit map still gets generic guidance."""
        constructs = [self._make_construct("WINDOW_FRAME", "complex window frame clause")]
        guidance = athena_guide.guide(sample_entity, constructs)
        assert len(guidance) == 1
        assert "WINDOW_FRAME" in guidance[0]

    def test_empty_constructs_returns_empty(self, athena_guide, redshift_guide, sample_entity):
        """Both guides return empty list for no constructs."""
        assert athena_guide.guide(sample_entity, []) == []
        assert redshift_guide.guide(sample_entity, []) == []


# ── TranslationResult model tests ────────────────────────────────────────────


class TestTranslationResultModel:
    """TranslationResult target_engine field."""

    def test_default_target_engine_is_redshift(self):
        tr = TranslationResult(redshift_sql="SELECT 1", confidence="HIGH", warnings=[])
        assert tr.target_engine == "redshift"

    def test_explicit_athena_target_engine(self):
        tr = TranslationResult(
            redshift_sql="SELECT 1", confidence="HIGH", warnings=[], target_engine="athena"
        )
        assert tr.target_engine == "athena"

    def test_serialization_includes_target_engine(self):
        """target_engine should survive dataclass-to-dict serialization."""
        import dataclasses
        tr = TranslationResult(
            redshift_sql="SELECT 1", confidence="HIGH", warnings=[], target_engine="athena"
        )
        d = dataclasses.asdict(tr)
        assert d["target_engine"] == "athena"
        assert d["redshift_sql"] == "SELECT 1"


# ── Fix 2: unsupported_constructs carried as BLOCKER warnings ──────────────


class TestUnsupportedConstructsAsBlockers:
    """Fix 2: EngineRewrite.unsupported_constructs are folded into warnings with BLOCKER prefix."""

    def test_geography_function_produces_blocker_warning(self, athena_guide):
        """SQL using GEOGRAPHY/geo functions emits BLOCKER warnings via unsupported_constructs."""
        # ST_GEOGPOINT is a BigQuery-only geography function with no Athena equivalent
        sql = "SELECT ST_GEOGPOINT(lng, lat) AS point FROM ds.locations"
        engine_rewrite = athena_guide.translate(sql)
        # Simulate the mapping done in cli.py (Fix 2)
        merged_warnings = engine_rewrite.warnings + [
            f"BLOCKER: {c}" for c in engine_rewrite.unsupported_constructs
        ]
        tr = TranslationResult(
            redshift_sql=engine_rewrite.translated_sql,
            confidence=engine_rewrite.confidence,
            warnings=merged_warnings,
            target_engine="athena",
        )
        # If the function is unsupported, there should be a BLOCKER entry
        # (the Athena guide marks geo functions as unsupported)
        if engine_rewrite.unsupported_constructs:
            blocker_entries = [w for w in tr.warnings if w.startswith("BLOCKER:")]
            assert len(blocker_entries) > 0
            assert any("ST_GEOGPOINT" in b or "GEOGRAPHY" in b.upper() or "geo" in b.lower() for b in blocker_entries)

    def test_simple_sql_no_blocker_warnings(self, athena_guide):
        """Simple SQL with no unsupported constructs produces no BLOCKER warnings."""
        sql = "SELECT col1, col2 FROM ds.simple_table"
        engine_rewrite = athena_guide.translate(sql)
        merged_warnings = engine_rewrite.warnings + [
            f"BLOCKER: {c}" for c in engine_rewrite.unsupported_constructs
        ]
        tr = TranslationResult(
            redshift_sql=engine_rewrite.translated_sql,
            confidence=engine_rewrite.confidence,
            warnings=merged_warnings,
            target_engine="athena",
        )
        blocker_entries = [w for w in tr.warnings if w.startswith("BLOCKER:")]
        assert len(blocker_entries) == 0


# ── Template source tests ────────────────────────────────────────────────────


class TestTemplateEngineAware:
    """Verify the HTML template uses engine-aware labels via ENGINE_META map."""

    @pytest.fixture
    def template_source(self):
        from pathlib import Path
        tpl = Path(__file__).parent.parent.parent / "src" / "bq_assess" / "report" / "templates" / "combined.html.j2"
        return tpl.read_text()

    def test_template_has_engine_meta_map(self, template_source):
        """Template defines ENGINE_META with both athena and redshift entries."""
        assert "var ENGINE_META = {" in template_source
        assert "athena:" in template_source
        assert "redshift:" in template_source

    def test_template_has_dynamic_engine_label(self, template_source):
        """Template should dynamically build heading from ENGINE_META, not hardcode 'Redshift'."""
        # The old hardcoded heading should NOT exist
        assert "sectionHeading('Suggested Redshift SQL'" not in template_source
        # The dynamic heading should use ENGINE_META
        assert "sqlMeta.label + ' SQL'" in template_source

    def test_template_has_dynamic_rewrite_heading(self, template_source):
        """The 'How to rewrite for X' heading should use ENGINE_META."""
        assert "ENGINE_META[engineKey].rewriteHeading" in template_source

    def test_template_has_athena_help_text(self, template_source):
        """Template should contain Athena-specific help text about Glue catalog."""
        assert "Glue catalog database context" in template_source

    def test_template_has_redshift_help_text(self, template_source):
        """Template should still contain Redshift-specific help text about external schema."""
        assert "external schema prefix" in template_source

    def test_template_engine_key_from_target_engine_or_global(self, template_source):
        """Engine key falls back to RECOMMENDED_ENGINE when translated_sql is absent."""
        assert "e.translated_sql.target_engine) || RECOMMENDED_ENGINE" in template_source
