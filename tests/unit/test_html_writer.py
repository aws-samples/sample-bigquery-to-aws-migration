"""Unit tests for report/html_writer.py — single combined HTML report (R20)."""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone

from bq_assess.models import (
    Assessment,
    AssessmentSummary,
    BQPricingModel,
    ConfidenceLevel,
    ConfidenceSource,
    ComplexityCategory,
    ComplexityResult,
    ConversionResult,
    CostComparison,
    CostLine,
    EffortCategory,
    EffortResult,
    EntityPopulation,
    EntityReport,
    EntityType,
    PlacementRecommendation,
)
from bq_assess.report.html_writer import HTMLWriter


def _known_assessment(
    compute_confidence=ConfidenceLevel.HIGH, sql_confidence=ConfidenceLevel.HIGH
):
    entities = [
        EntityReport(
            full_name="ds.orders",
            entity_type=EntityType.TABLE,
            population=EntityPopulation.TABLE,
            rows=1_000_000,
            size_gb=42.5,
            depends_on=[],
            effort=EffortResult(
                category=EffortCategory.ASSISTED,
                score=45,
                flags=["time_partitioning"],
                reasoning="partitioned",
                confidence=ConfidenceLevel.HIGH,
            ),
            conversion=ConversionResult(
                ddl="CREATE TABLE ds.orders (id long);",
                partition_mapping=None,
                lossy_casts=[],
                warnings=[],
                success=True,
            ),
            load_sync_dml="COPY INTO ds.orders FROM 's3://bucket'",
            complexity=ComplexityResult(
                category=ComplexityCategory.ADAPT,
                score=60,
                constructs=[],
                flags=["UNNEST"],
                reasoning="uses UNNEST",
                confidence=ConfidenceLevel.MEDIUM,
                confidence_source=ConfidenceSource.QUERY_LOGS,
            ),
            rewrite_guidance=["Replace UNNEST"],
            placement=None,
        ),
        EntityReport(
            full_name="ds.view1",
            entity_type=EntityType.VIEW,
            population=EntityPopulation.REBUILT,
            rows=0,
            size_gb=0.0,
            depends_on=["ds.orders"],
            effort=None,
            conversion=None,
            load_sync_dml=None,
            complexity=ComplexityResult(
                category=ComplexityCategory.REWRITE,
                score=80,
                constructs=[],
                flags=["JS_UDF"],
                reasoning="JS",
                confidence=ConfidenceLevel.LOW,
                confidence_source=ConfidenceSource.VIEW_DEFINITION,
            ),
            rewrite_guidance=["Rewrite JS UDF"],
            placement=None,
        ),
    ]
    return Assessment(
        assessment_id="assess-20260617-abc123",
        generated_at=datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc),
        project_id="my-project",
        summary=AssessmentSummary(
            total_entities=2,
            total_tables=1,
            total_size_gb=42.5,
            effort_counts={"AUTO": 0, "ASSISTED": 1, "MANUAL": 0},
            complexity_counts={"PORTABLE": 0, "ADAPT": 1, "REWRITE": 1},
            sql_surface_confidence=sql_confidence,
        ),
        cost=CostComparison(
            bq_pricing_model=BQPricingModel.CAPACITY,
            bigquery_monthly=105000.0,
            bigquery_breakdown=[
                CostLine(
                    label="BQ cap",
                    monthly=105000.0,
                    monthly_low=None,
                    monthly_high=None,
                    confidence=ConfidenceLevel.HIGH,
                    source_note="V4",
                )
            ],
            aws_lines=[
                CostLine(
                    label="S3",
                    monthly=50.0,
                    monthly_low=None,
                    monthly_high=None,
                    confidence=ConfidenceLevel.HIGH,
                    source_note="V2",
                )
            ],
            aws_monthly_low=26250.0,
            aws_monthly_high=26250.0,
            monthly_delta_low=78750.0,
            monthly_delta_high=78750.0,
            annual_savings_low=945000.0,
            annual_savings_high=945000.0,
            migration_onetime=15000.0,
            breakeven_months_low=0.19,
            breakeven_months_high=0.19,
            compute_confidence=compute_confidence,
        ),
        entities=entities,
        failures=[],
    )


def test_html_renders_single_file():
    a = _known_assessment()
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    assert len(paths) == 1
    assert paths[0].endswith(".html")
    assert os.path.exists(paths[0])
    assert os.path.basename(paths[0]) == "my-project-assessment.html"


def test_html_contains_all_tabs():
    a = _known_assessment()
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert 'id="tab-landing"' in html
    assert 'id="tab-effort"' in html
    assert 'id="tab-query"' in html


def test_html_offline_no_external_urls():
    a = _known_assessment()
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "http://" not in html
    assert "https://" not in html


def test_html_low_confidence_banner_compute():
    a = _known_assessment(compute_confidence=ConfidenceLevel.LOW)
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "Low Confidence Cost Estimate" in html


def test_html_medium_confidence_cue_adjacent_to_savings():
    """A non-HIGH estimate must carry a confidence cue in the cost section itself,
    anchor-linked to the methodology section — not only in Assumptions & Methodology
    (2026-07-16 audit HRI-1: MEDIUM headline had zero adjacent uncertainty signal)."""
    a = _known_assessment()
    a.cost.estimate_basis_level = ConfidenceLevel.MEDIUM
    a.cost.estimate_basis = "Priced from 27 days of measured workload."
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert 'href="#assumptions"' in html
    assert 'id="assumptions"' in html
    assert "confidence estimate" in html


def test_html_high_confidence_suppresses_cost_cue():
    """HIGH confidence renders no cue under the savings figure."""
    a = _known_assessment()
    a.cost.estimate_basis_level = ConfidenceLevel.HIGH
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "confidence estimate" not in html


def test_html_renders_narrative_cards_when_populated():
    """The three narrative cards render when the model fields are populated — pins
    the caveat-presence behavior no prior test asserted (2026-07-16 audit)."""
    a = _known_assessment()
    a.cost.estimate_basis = "Priced from 27 days of measured workload."
    a.cost.pricing_notes = ["BigQuery priced for us; AWS priced for us-east-1."]
    a.cost.key_uncertainties = ["Slot to RPU conversion is an assumption."]
    a.cost.scope_notes = ["BigQuery side: analysis and storage only.", "AWS side: Spectrum not modeled."]
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "How This Estimate Was Priced" in html
    assert "Key Uncertainties" in html
    assert "Not Modeled (Both Sides of the Comparison)" in html


def test_html_low_confidence_banner_sql_surface():
    a = _known_assessment(sql_confidence=ConfidenceLevel.LOW)
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "Low Confidence SQL Analysis" in html


def test_html_no_csv_emitted():
    a = _known_assessment()
    out = tempfile.mkdtemp()
    HTMLWriter().write(a, out)
    files = os.listdir(out)
    assert not any(f.endswith(".csv") for f in files)


def test_html_placement_home_label_map():
    """HRI-1: placement home renders via a lookup map, not a binary ternary.

    LAMBDA_UDF_REQUIRED must render as "Requires AWS Lambda UDF (USING EXTERNAL FUNCTION)",
    not "Iceberg catalog (open, multi-engine)".
    """
    a = _known_assessment()
    # Add a UDF entity with LAMBDA_UDF_REQUIRED placement
    a.entities.append(
        EntityReport(
            full_name="ds.my_udf",
            entity_type=EntityType.ROUTINE,
            population=EntityPopulation.REBUILT,
            rows=0,
            size_gb=0.0,
            depends_on=[],
            effort=None,
            conversion=None,
            load_sync_dml=None,
            complexity=ComplexityResult(
                category=ComplexityCategory.REWRITE,
                score=70,
                constructs=[],
                flags=["SQL_UDF"],
                reasoning="SQL UDF",
                confidence=ConfidenceLevel.HIGH,
                confidence_source=ConfidenceSource.SCHEMA_ONLY,
            ),
            rewrite_guidance=["Implement as Lambda function"],
            placement=PlacementRecommendation(
                home="LAMBDA_UDF_REQUIRED",
                signals=["SQL UDFs must be implemented as Lambda functions in Athena"],
                confidence=ConfidenceLevel.HIGH,
                refresh_unverified=False,
            ),
        )
    )
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()

    # Must render the correct label for LAMBDA_UDF_REQUIRED
    assert "Requires AWS Lambda UDF (USING EXTERNAL FUNCTION)" in html
    # Must NOT mislabel it as Iceberg catalog
    # (can't assert absence of "Iceberg catalog" string since other entities may use it,
    # but the homeLabels map in JS ensures the correct label is used)
    assert "homeLabels" in html
    assert "'LAMBDA_UDF_REQUIRED':" in html


def test_html_mobile_viewport():
    a = _known_assessment()
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert 'name="viewport"' in html
    assert "@media (max-width: 768px)" in html


def test_html_storage_basis_measured():
    """When storage_basis='measured', template receives correct basis."""
    a = _known_assessment()
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out, storage_basis="measured")
    with open(paths[0]) as f:
        html = f.read()
    # The template's conditional text for measured storage
    assert "measured physical bytes" in html.lower()


def test_html_storage_basis_assumed():
    """When storage_basis='assumed' (default), template interpolates physical_ratio."""
    from bq_assess.engine.redshift import cost_constants as k
    a = _known_assessment()
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out, storage_basis="assumed")
    with open(paths[0]) as f:
        html = f.read()
    # The template's conditional text for assumed storage — should have interpolated ratio
    assert str(k.ASSUMED_PHYSICAL_RATIO) in html


def _render_html(assessment) -> str:
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(assessment, out)
    with open(paths[0]) as f:
        return f.read()


def test_html_has_csp_with_script_nonce():
    """The report ships a CSP that only allows the nonce'd inline script (no unsafe-inline)."""
    html = _render_html(_known_assessment())
    m = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", html)
    assert m, "CSP header missing a script nonce"
    nonce = m.group(1)
    # The one legitimate inline <script> must carry the matching nonce...
    assert f'<script nonce="{nonce}">' in html
    # ...and there must be NO bare <script> that a compliant browser would run.
    assert "<script>" not in html
    # unsafe-inline for scripts would defeat the whole point.
    assert "'unsafe-inline'" not in re.search(r"script-src[^;]*", html).group(0)


def test_html_csp_nonce_is_per_render():
    """Each rendered file gets a fresh, unguessable nonce (never a fixed constant)."""
    h1 = _render_html(_known_assessment())
    h2 = _render_html(_known_assessment())
    n1 = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", h1).group(1)
    n2 = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", h2).group(1)
    assert n1 != n2
    assert len(n1) >= 16


def test_html_malicious_identifier_is_neutralized():
    """A BigQuery identifier attempting <code> breakout + <script> injection is escaped.

    Regression for the internal deep-audit XSS finding: DDL/DML rendered from
    attacker-controlled entity names must never produce an executable <script>.
    """
    a = _known_assessment()
    payload = "ds.evil</code><script>alert(document.domain)</script><code>t"
    a.entities[0].full_name = payload
    a.entities[0].conversion.ddl = f"CREATE TABLE {payload} (id long);"
    a.entities[0].load_sync_dml = f"INSERT INTO {payload} SELECT * FROM src;"
    html = _render_html(a)
    # The raw injected script must not survive as executable markup anywhere —
    # entity data now ships inside a JSON data block, where Jinja's |tojson
    # escapes `<` as \\u003c so the payload can never close the block or form a tag.
    assert "<script>alert(document.domain)</script>" not in html
    assert "</code><script>" not in html
    assert "\\u003cscript\\u003ealert(document.domain)\\u003c/script\\u003e" in html
    # And the only executable <script> tag is still the nonce'd one.
    assert "<script>" not in html
    # The client-side renderer must insert entity data as text, never as markup.
    assert "innerHTML" not in html


def test_html_engine_recommendation_section_present():
    """R19 unified surface: removed standalone recommendation banner and 'Why this Query Engine' collapsible."""
    from decimal import Decimal
    from bq_assess.models import EngineRecommendation, SignalContribution, AWSRecommendation, WorkloadProfile

    a = _known_assessment()
    # Add a cost recommendation
    a.cost.recommendation = AWSRecommendation(
        recommended_scenario="Redshift Serverless",
        reasoning="Your workload scans 15.5 TB/month. Redshift recommended.",
        workload_profile=WorkloadProfile(),
        alternatives_considered=["Athena"],
    )
    a.engine_recommendation = EngineRecommendation(
        primary_engine="redshift",
        confidence=0.72,
        reasoning=[
            SignalContribution(signal="daily_scan_volume_tb", value=25.5, direction="redshift", weight=0.4),
            SignalContribution(signal="concurrency", value=45, direction="redshift", weight=0.3),
        ],
        crossover_point_tb_day=Decimal("25.00"),
        override_reason=None,
    )
    html = _render_html(a)
    # R19 unified surface: removed sections
    assert "Why this Query Engine" not in html
    assert "Signals Breakdown" not in html
    assert '<h3 style="margin-top:0;font-size:.9375rem;color:var(--color-severity-success)">Recommendation:' not in html


def test_html_engine_recommendation_section_absent_when_none():
    """When engine_recommendation is None, no separate signal analysis block exists (R19 unified)."""
    a = _known_assessment()
    a.engine_recommendation = None
    html = _render_html(a)
    # Check that the removed blocks are never present
    assert "Why this Query Engine" not in html
    assert "Signals Breakdown" not in html


def test_html_standalone_migration_plans_section_removed():
    """The standalone 'Migration Plan (Athena)' section is no longer rendered
    even when migration_plans is populated — plans now live in per-entity rows."""
    from bq_assess.models import MigrationDML, MigrationShortcoming, PostMigrationStep

    a = _known_assessment()
    a.migration_plans = {
        "ds.orders": MigrationDML(
            table="ds.orders",
            statements=["INSERT INTO iceberg_db.ds_orders SELECT * FROM source_db.ds_orders;"],
            shortcomings=[
                MigrationShortcoming(
                    category="compaction",
                    severity="advisory",
                    bq_source="table size: 2.5 GB",
                    description="Table exceeds 1.0 GB threshold; post-load compaction recommended",
                    remediation="OPTIMIZE iceberg_db.ds_orders REWRITE DATA USING BIN_PACK",
                    remediation_engine="athena",
                )
            ],
            post_optimization=[
                PostMigrationStep(
                    table="ds.orders",
                    step_type="compact",
                    command="OPTIMIZE iceberg_db.ds_orders REWRITE DATA USING BIN_PACK",
                    engine="athena",
                    reason="Post-load compaction reduces small-file overhead",
                    priority="recommended",
                )
            ],
            estimated_scan_bytes=2684354560,
        )
    }
    html = _render_html(a)
    # Standalone section heading must NOT appear
    assert "<h2>Migration Plan (Athena)</h2>" not in html
    assert "Migration Plan (Athena)" not in html
    # But the per-entity JS renderer includes 'Load DML (Athena)' string
    assert "Load DML (Athena)" in html


def test_html_migration_plan_absent_when_none():
    """When migration_plans is None, no migration plan heading appears anywhere."""
    a = _known_assessment()
    a.migration_plans = None
    html = _render_html(a)
    assert "<h2>Migration Plan (Athena)</h2>" not in html


def test_html_per_entity_migration_plan_in_payload():
    """Per-entity migration_plan field is serialized into the effort row payload."""
    import json
    from bq_assess.models import MigrationDML, MigrationShortcoming, PostMigrationStep

    a = _known_assessment()
    a.migration_plans = {
        "ds.orders": MigrationDML(
            table="ds.orders",
            statements=[
                "DELETE FROM iceberg_db.ds_orders WHERE dt >= '2024-01-01';",
                "INSERT INTO iceberg_db.ds_orders SELECT * FROM source_db.ds_orders WHERE dt >= '2024-01-01';",
            ],
            shortcomings=[
                MigrationShortcoming(
                    category="compaction",
                    severity="advisory",
                    bq_source="table size: 2.5 GB",
                    description="Post-load compaction recommended",
                    remediation="OPTIMIZE iceberg_db.ds_orders REWRITE DATA USING BIN_PACK",
                    remediation_engine="athena",
                )
            ],
            post_optimization=[
                PostMigrationStep(
                    table="ds.orders",
                    step_type="compact",
                    command="OPTIMIZE iceberg_db.ds_orders REWRITE DATA USING BIN_PACK",
                    engine="athena",
                    reason="Reduces small-file overhead",
                    priority="recommended",
                )
            ],
            estimated_scan_bytes=2684354560,
        )
    }
    html = _render_html(a)
    # Extract the embedded JSON payload
    import re
    m = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html)
    assert m, "report-data JSON block not found"
    data = json.loads(m.group(1))
    # The effort row for ds.orders must carry the structured migration_plan
    effort_rows = data["effort"]
    orders_row = next((r for r in effort_rows if r["full_name"] == "ds.orders"), None)
    assert orders_row is not None, "ds.orders not in effort rows"
    assert "migration_plan" in orders_row, "migration_plan field missing from entity row"
    plan = orders_row["migration_plan"]
    assert len(plan["statements"]) == 2
    assert "DELETE FROM" in plan["statements"][0]
    assert len(plan["shortcomings"]) == 1
    assert plan["shortcomings"][0]["category"] == "compaction"
    assert len(plan["post_optimization"]) == 1
    assert plan["post_optimization"][0]["engine"] == "athena"


# --- MRI-5: unified savings formatter ---


def test_format_savings_comparable_shows_delta():
    """MRI-5: abs < $1 renders 'Comparable (+-$X.XX)'."""
    from bq_assess.report.html_writer import _format_savings
    assert _format_savings(0.11) == "Comparable (±$0.11)"
    assert _format_savings(-0.50) == "Comparable (±$0.50)"
    assert _format_savings(0.0) == "Comparable (±$0.00)"


def test_format_savings_large_positive():
    """MRI-5: $15 delta renders 'Save $15.00/mo'."""
    from bq_assess.report.html_writer import _format_savings
    assert _format_savings(15.0) == "Save $15.00/mo"


def test_format_savings_large_negative():
    """MRI-5: -$15 renders '+$15.00/mo'."""
    from bq_assess.report.html_writer import _format_savings
    assert _format_savings(-15.0) == "+$15.00/mo"


def test_format_savings_none():
    from bq_assess.report.html_writer import _format_savings
    assert _format_savings(None) == "N/A"


# --- MRI-2a: UNKNOWN pricing sentinel guard ---


def test_html_unknown_pricing_shows_info_card():
    """MRI-2a: when bq_pricing_model == UNKNOWN, cost-hero is replaced by info card."""
    a = _known_assessment()
    a.cost.bq_pricing_model = BQPricingModel.UNKNOWN
    html = _render_html(a)
    assert "Pricing data unavailable in this bundle" in html
    # The cost-hero block content should NOT render (the class exists in CSS but not as an element)
    assert 'class="cost-hero__arrow"' not in html


# --- MRI-4b: homeLabels fallback ---


def test_html_home_label_fallback_unknown_enum():
    """MRI-4b: unmapped placement.home values render 'Review required (...)' not raw enum."""
    a = _known_assessment()
    a.entities[0].placement = PlacementRecommendation(
        home="SOME_NEW_VALUE",
        signals=["test signal"],
        confidence=ConfidenceLevel.MEDIUM,
        refresh_unverified=False,
    )
    html = _render_html(a)
    # JS fallback should produce the safe generic label
    assert "Review required (" in html



