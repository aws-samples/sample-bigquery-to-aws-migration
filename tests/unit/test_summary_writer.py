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


def test_only_one_most_manual_takeaway():
    """2026-08-04 audit: two projects were both claimed 'the most manual' —
    the bullet must name only the minimum."""
    from bq_assess.report.summary_writer import _key_takeaways

    rows = [
        {"project_id": "a", "annual_saving": 100, "annual_saving_high": 100,
         "scored_count": 10, "auto_pct": 64.0, "recommendation": "X"},
        {"project_id": "b", "annual_saving": 50, "annual_saving_high": 50,
         "scored_count": 10, "auto_pct": 67.0, "recommendation": "Y"},
    ]
    takeaways = _key_takeaways(rows)
    manual = [t for t in takeaways if "most manual" in t]
    assert len(manual) == 1
    assert manual[0].startswith("a ")  # the lower auto_pct wins


class TestUnavailableBQCost:
    """Projects with unavailable BQ cost must render dashes in the fleet summary
    and be excluded from savings aggregates (2026-08-10 capacity-pricing-honesty)."""

    def test_project_row_returns_none_for_unavailable_cost_fields(self) -> None:
        """_project_row must return None for cost fields when bq_cost_available=False."""
        import dataclasses

        from bq_assess.report.summary_writer import _project_row

        a = _assessment("proj-unavail", 5000.0, 2000.0)
        a.cost = dataclasses.replace(
            a.cost,
            bq_cost_available=False,
            bq_cost_unavailable_reason="Enterprise Edition capacity without reservation data",
            bigquery_monthly=0.0,
            annual_savings_low=0.0,
            annual_savings_high=0.0,
        )
        row = _project_row(a)
        assert row["bq_cost_available"] is False
        assert row["bq_monthly"] is None
        assert row["aws_monthly"] is None
        assert row["annual_saving"] is None
        assert row["annual_saving_high"] is None

    def test_html_renders_dash_for_unavailable_cost(self, tmp_path) -> None:
        """Fleet table must show '—' in cost/savings columns when BQ cost is unavailable."""
        import dataclasses

        a = _assessment("proj-unavail", 5000.0, 2000.0)
        a.cost = dataclasses.replace(
            a.cost,
            bq_cost_available=False,
            bq_cost_unavailable_reason="Enterprise Edition capacity without reservation data",
            bigquery_monthly=0.0,
            annual_savings_low=0.0,
            annual_savings_high=0.0,
        )
        write_summary([a], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")
        # The row for proj-unavail must show dashes in all cost columns
        assert "proj-unavail" in html
        # Find the table row for this project and verify dashes appear
        lines = html.split('\n')
        row_start = None
        for i, line in enumerate(lines):
            if 'proj-unavail' in line and '<td>' in line:
                row_start = i
                break
        assert row_start is not None
        # The next few lines should contain the table cells with dashes
        row_section = '\n'.join(lines[row_start:row_start+10])
        # Should have 3 dashes in cost columns (BQ Cost, AWS Cost, Annual Saving)
        assert row_section.count('<span class="neutral">—</span>') == 3

    def test_key_takeaways_excludes_unavailable_projects_from_savings(self) -> None:
        """Projects with unavailable BQ cost must not be counted in savings aggregates."""
        from bq_assess.report.summary_writer import _key_takeaways

        rows = [
            {"project_id": "a", "annual_saving": 10000, "annual_saving_high": 10000,
             "scored_count": 10, "auto_pct": 75.0, "recommendation": "X"},
            {"project_id": "b-unavail", "annual_saving": None, "annual_saving_high": None,
             "bq_monthly": None, "aws_monthly": None, "scored_count": 10, "auto_pct": 75.0,
             "recommendation": "Y"},
            {"project_id": "c", "annual_saving": 5000, "annual_saving_high": 5000,
             "scored_count": 10, "auto_pct": 80.0, "recommendation": "Z"},
        ]
        takeaways = _key_takeaways(rows)
        # Should say "2 of 3 projects save money" (excluding the unavailable one)
        saving_line = [t for t in takeaways if "projects save money" in t]
        assert len(saving_line) == 1
        assert "2 of 3 projects" in saving_line[0]

    def test_fleet_aggregates_exclude_unavailable_costs(self, tmp_path) -> None:
        """Fleet-wide cost aggregates must exclude projects with unavailable BQ cost."""
        import dataclasses

        a1 = _assessment("proj-good", 5000.0, 2000.0)
        a2 = _assessment("proj-unavail", 3000.0, 1000.0)
        a2.cost = dataclasses.replace(
            a2.cost,
            bq_cost_available=False,
            bigquery_monthly=0.0,
            annual_savings_low=0.0,
            annual_savings_high=0.0,
        )

        write_summary([a1, a2], str(tmp_path))
        html = (tmp_path / "SUMMARY.html").read_text(encoding="utf-8")

        # Total annual saving should only count proj-good
        # proj-good saves: (5000 - 2200)*12 = 33,600/yr ~= $33.6K
        assert "$33" in html or "$34" in html
        # Should NOT include the zeroed unavailable project in the total
