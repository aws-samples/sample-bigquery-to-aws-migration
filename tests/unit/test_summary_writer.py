"""Tests for the cross-project SUMMARY.html writer (--gcp-project all roll-up)."""
from __future__ import annotations

from datetime import datetime, timezone

from bq_assess.models import (
    Assessment,
    AssessmentSummary,
    BQPricingModel,
    ComplexityCategory,
    ConfidenceLevel,
    CostComparison,
    EntityPopulation,
)
from bq_assess.report.summary_writer import write_summary


def _cost(bq_monthly: float, aws_low: float, aws_high: float) -> CostComparison:
    annual_low = (bq_monthly - aws_high) * 12
    annual_high = (bq_monthly - aws_low) * 12
    return CostComparison(
        bq_pricing_model=BQPricingModel.ON_DEMAND,
        bigquery_monthly=bq_monthly,
        bigquery_breakdown=[],
        aws_lines=[],
        aws_monthly_low=aws_low,
        aws_monthly_high=aws_high,
        monthly_delta_low=bq_monthly - aws_high,
        monthly_delta_high=bq_monthly - aws_low,
        annual_savings_low=annual_low,
        annual_savings_high=annual_high,
        migration_onetime=1000.0,
        breakeven_months_low=1.0,
        breakeven_months_high=2.0,
        compute_confidence=ConfidenceLevel.MEDIUM,
    )


def _assessment(project_id: str, bq_monthly: float, aws_mid: float) -> Assessment:
    return Assessment(
        assessment_id=f"assess-20260722-{project_id[:8]}",
        generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        project_id=project_id,
        summary=AssessmentSummary(
            total_entities=100,
            total_tables=40,
            total_size_gb=50.0,
            effort_counts={"AUTO": 30, "ASSISTED": 8, "MANUAL": 2},
            complexity_counts={"PORTABLE": 70, "ADAPT": 25, "REWRITE": 5},
            sql_surface_confidence=ConfidenceLevel.HIGH,
            total_logical_size_gb=60.0,
        ),
        cost=_cost(bq_monthly, aws_mid * 0.9, aws_mid * 1.1),
        entities=[],
        failures=[],
    )


class TestWriteSummary:
    def test_writes_summary_html(self, tmp_path) -> None:
        assessments = [
            _assessment("proj-alpha", 5000.0, 2000.0),
            _assessment("proj-beta", 1000.0, 1200.0),  # costs more on AWS
        ]
        path = write_summary(assessments, str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")

        assert path.endswith("SUMMARY.html")
        assert "proj-alpha" in html
        assert "proj-beta" in html
        assert "2 projects" in html
        # 80 tables total across both projects
        assert "80" in html

    def test_links_to_project_reports(self, tmp_path) -> None:
        write_summary([_assessment("proj-alpha", 5000.0, 2000.0)], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        assert 'href="proj-alpha_2026-07-22/report/proj-alpha-assessment.html"' in html

    def test_negative_saving_flagged(self, tmp_path) -> None:
        write_summary([_assessment("pricey", 1000.0, 1500.0)], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        assert "(higher)" in html

    def test_theme_matches_main_report(self, tmp_path) -> None:
        write_summary([_assessment("proj-alpha", 5000.0, 2000.0)], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        # Cloudscape design tokens from the main report
        assert "--color-bg-header: #0f1b2a" in html
        assert "Amazon Ember" in html
        assert "aws-cube" in html

    def test_auto_migrate_percentage(self, tmp_path) -> None:
        write_summary([_assessment("proj-alpha", 5000.0, 2000.0)], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        # 30 AUTO of 40 scored = 75.0%
        assert "75.0%" in html


def _sql_entity(name: str, category: ComplexityCategory) -> EntityReport:  # noqa: F821
    from bq_assess.models import (
        ComplexityResult,
        ConfidenceSource,
        EntityReport,
        EntityType,
    )
    return EntityReport(
        full_name=name, entity_type=EntityType.VIEW,
        population=EntityPopulation.REBUILT, rows=0, size_gb=0.0, depends_on=[],
        effort=None, conversion=None, load_sync_dml=None,
        complexity=ComplexityResult(
            category=category, score=1, constructs=[], flags=[], reasoning="t",
            confidence=ConfidenceLevel.MEDIUM,
            confidence_source=ConfidenceSource.VIEW_DEFINITION,
        ),
        rewrite_guidance=[], placement=None,
    )


class TestSummaryScopesAndRanges:
    def test_sql_complexity_scoped_to_rebuilt_entities(self, tmp_path) -> None:
        """The 'SQL portable' stat must count SQL-owning entities only.
        summary.complexity_counts spans all entities (plain tables score
        PORTABLE by definition) — at fleet scale that rendered '100.0% SQL
        portable, 0 need rewrite' over 12,599 entities (2026-07-31 sandbox validation)."""
        a = _assessment("proj-sql", 5000.0, 2000.0)
        # summary says everything is portable (the misleading all-entities view)
        a.summary.complexity_counts = {"PORTABLE": 100, "ADAPT": 0, "REWRITE": 0}
        # but the actual SQL entities include a rewrite
        a.entities = [
            _sql_entity("ds.v1", ComplexityCategory.PORTABLE),
            _sql_entity("ds.v2", ComplexityCategory.REWRITE),
        ]
        write_summary([a], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        assert "50.0%" in html                     # 1 of 2 SQL entities portable
        assert "1 of 2 SQL entities" in html
        assert "100.0%" not in html

    def test_straddling_annual_range_shows_both_bounds(self, tmp_path) -> None:
        """AWS range straddling BQ (IT worst case vs steady state) must show
        'X higher to Y saved', not a bare '(higher)' verdict."""
        a = _assessment("proj-straddle", 3570.0, 2700.0)
        a.cost.aws_monthly_low = 1488.0
        a.cost.aws_monthly_high = 3925.0
        a.cost.annual_savings_low = (3570.0 - 3925.0) * 12    # -4,260
        a.cost.annual_savings_high = (3570.0 - 1488.0) * 12   # +24,984
        write_summary([a], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        assert "higher" in html and "saved" in html
        assert "$25.0K saved" in html or "$24" in html
