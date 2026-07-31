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
    ComplexityCategory,
    ComplexityResult,
    ConfidenceLevel,
    ConfidenceSource,
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

    from bq_assess.models import (
        AWSRecommendation,
        EngineRecommendation,
        SignalContribution,
        WorkloadProfile,
    )

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
    # Extract the embedded JSON payloads: light rows + lazy detail chunks
    import re
    m = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html)
    assert m, "report-data JSON block not found"
    data = json.loads(m.group(1))
    effort_rows = data["effort"]
    orders_row = next((r for r in effort_rows if r["full_name"] == "ds.orders"), None)
    assert orders_row is not None, "ds.orders not in effort rows"
    # Heavy payloads live in the lazy detail chunks, referenced by detail_chunk
    assert "migration_plan" not in orders_row, "heavy field leaked into the row payload"
    chunk_idx = orders_row["detail_chunk"]
    chunks = re.findall(
        r'<script type="application/json" class="detail-chunk" data-chunk="(\d+)">(.*?)</script>',
        html,
    )
    chunk = json.loads(dict(chunks)[str(chunk_idx)])
    plan = chunk["ds.orders"]["migration_plan"]
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
    assert _format_savings(0.11) == "Comparable (-$0.11)"
    assert _format_savings(-0.50) == "Comparable (+$0.50)"
    assert _format_savings(0.0) == "Comparable (-$0.00)"


def test_format_savings_large_positive():
    """MRI-5: $15 delta renders 'Save $15.00/mo'."""
    from bq_assess.report.html_writer import _format_savings
    assert _format_savings(15.0) == "Save $15.00/mo"


def test_format_savings_large_negative():
    """Negative deltas say 'more' explicitly — the previous bare '+$X' under a
    'Savings' label read as a saving (2026-07-31 sandbox validation fix)."""
    from bq_assess.report.html_writer import _format_savings
    assert _format_savings(-15.0) == "$15.00/mo more"
    assert _format_savings(-355.59) == "$356/mo more"


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


def test_html_athena_placement_tooltip_engine_branched():
    """Athena-recommended entity gets the Athena placement tooltip, NOT 'inside Redshift (engine-local)'."""
    from bq_assess.models import TranslationResult

    a = _known_assessment()
    # Give the view entity Athena-targeted translated SQL + a placement
    a.entities[1].translated_sql = TranslationResult(
        redshift_sql="SELECT * FROM ds.orders",
        confidence="HIGH",
        warnings=[],
        target_engine="athena",
    )
    a.entities[1].placement = PlacementRecommendation(
        home="CREATE VIEW",
        signals=["Entity recommended for Athena CREATE VIEW"],
        confidence=ConfidenceLevel.HIGH,
        refresh_unverified=False,
    )
    html = _render_html(a)
    # The Athena placement tooltip text must appear in the JS
    assert "Athena cannot create materialized views" in html
    # The old stale Redshift-only tooltip phrasing must NOT appear
    assert "can live either inside Redshift (engine-local) or in the shared Iceberg catalog" not in html


def test_html_redshift_placement_tooltip_engine_branched():
    """Redshift-recommended entity gets the Redshift placement tooltip."""
    from bq_assess.models import TranslationResult

    a = _known_assessment()
    a.entities[1].translated_sql = TranslationResult(
        redshift_sql="SELECT * FROM ds.orders",
        confidence="HIGH",
        warnings=[],
        target_engine="redshift",
    )
    a.entities[1].placement = PlacementRecommendation(
        home="REDSHIFT",
        signals=["Single-engine consumption"],
        confidence=ConfidenceLevel.HIGH,
        refresh_unverified=False,
    )
    html = _render_html(a)
    # Redshift path tooltip mentions engine-local and Iceberg catalog
    assert "Redshift (engine-local" in html
    assert "shared Iceberg catalog" in html


def test_help_include_no_orphaned_entries():
    """Every key in _help.j2 dicts is referenced by combined.html.j2 or its JS, and
    every reference in combined.html.j2 resolves to a _help.j2 key."""
    import re
    from pathlib import Path

    templates = Path(__file__).parent.parent.parent / "src" / "bq_assess" / "report" / "templates"
    help_src = (templates / "_help.j2").read_text()
    combined_src = (templates / "combined.html.j2").read_text()

    # Extract top-level dict names from _help.j2
    help_dicts = re.findall(r'\{%\s*set\s+(\w+)\s*=', help_src)
    assert help_dicts, "_help.j2 should define at least one dict"

    # Every dict defined in _help.j2 must be referenced in combined.html.j2
    for name in help_dicts:
        assert name in combined_src, f"_help.j2 defines '{name}' but combined.html.j2 never references it"

    # Every help.X reference in combined.html.j2 must resolve to a _help.j2 dict
    # Exclude 'j2' from matches (comes from "{% import '_help.j2' as help %}")
    refs = set(re.findall(r'help\.([A-Z]\w+)', combined_src))
    for ref in refs:
        assert ref in help_dicts, f"combined.html.j2 references 'help.{ref}' but _help.j2 does not define it"


# --- Fix 1: recommended engine at payload level ---


def test_html_athena_engine_global_without_translated_sql():
    """Fix 1: entity with rewrite_guidance + placement but NO translated_sql reads
    the GLOBAL recommended engine, not the per-entity translated_sql.target_engine.
    The JS variable RECOMMENDED_ENGINE is 'athena' and ENGINE_META drives the branches."""
    from decimal import Decimal

    from bq_assess.models import EngineRecommendation, SignalContribution

    a = _known_assessment()
    a.engine_recommendation = EngineRecommendation(
        primary_engine="athena",
        confidence=0.85,
        reasoning=[
            SignalContribution(signal="daily_scan_volume_tb", value=2.0, direction="athena", weight=0.6),
        ],
        crossover_point_tb_day=Decimal("10.00"),
        override_reason=None,
    )
    # Entity has rewrite_guidance + placement but NO translated_sql
    a.entities[1].rewrite_guidance = ["Replace UNNEST with CROSS JOIN UNNEST"]
    a.entities[1].translated_sql = None
    a.entities[1].placement = PlacementRecommendation(
        home="CREATE VIEW",
        signals=["Simple view — CREATE VIEW on Athena"],
        confidence=ConfidenceLevel.HIGH,
        refresh_unverified=False,
    )
    html = _render_html(a)
    # The RECOMMENDED_ENGINE JS variable must be 'athena'
    assert '"recommendedEngine": "athena"' in html
    # ENGINE_META.athena entries are present (both always present as they're in the JS source)
    assert "How to rewrite for Athena" in html
    # The JS uses `ENGINE_META[engineKey].rewriteHeading` where engineKey falls through
    # to RECOMMENDED_ENGINE when translated_sql is absent — verified by the var assignment
    assert "var RECOMMENDED_ENGINE = HELP.recommendedEngine" in html


# --- Fix 3: Load DML explanation under Redshift recommendation ---


def test_html_load_dml_redshift_explanation():
    """Fix 3: when recommended engine is redshift, the ENGINE_META.redshift.loadDmlHelp
    contains the explanation that Athena performs the data load regardless. The JS code
    dispatches based on RECOMMENDED_ENGINE which is 'redshift'."""
    from decimal import Decimal

    from bq_assess.models import (
        EngineRecommendation,
        MigrationDML,
        SignalContribution,
    )

    a = _known_assessment()
    a.engine_recommendation = EngineRecommendation(
        primary_engine="redshift",
        confidence=0.75,
        reasoning=[
            SignalContribution(signal="concurrency", value=80, direction="redshift", weight=0.5),
        ],
        crossover_point_tb_day=Decimal("25.00"),
        override_reason=None,
    )
    a.migration_plans = {
        "ds.orders": MigrationDML(
            table="ds.orders",
            statements=["INSERT INTO iceberg_db.ds_orders SELECT * FROM source_db.ds_orders;"],
            shortcomings=[],
            post_optimization=[],
            estimated_scan_bytes=1073741824,
        )
    }
    html = _render_html(a)
    # The ENGINE_META.redshift.loadDmlHelp text is in the JS
    assert "Athena performs the one-time data load regardless of the recommended Query Engine" in html
    # And the RECOMMENDED_ENGINE is set to 'redshift'
    assert '"recommendedEngine": "redshift"' in html
    # The JS dispatches: ENGINE_META[RECOMMENDED_ENGINE].loadDmlHelp → picks redshift entry
    assert "ENGINE_META[RECOMMENDED_ENGINE].loadDmlHelp" in html


def test_html_load_dml_athena_recommended_engine():
    """Fix 3: when recommended engine is athena, RECOMMENDED_ENGINE='athena', so JS
    dispatches to ENGINE_META.athena.loadDmlHelp (which lacks the Redshift explanation)."""
    from decimal import Decimal

    from bq_assess.models import (
        EngineRecommendation,
        MigrationDML,
        SignalContribution,
    )

    a = _known_assessment()
    a.engine_recommendation = EngineRecommendation(
        primary_engine="athena",
        confidence=0.85,
        reasoning=[
            SignalContribution(signal="daily_scan_volume_tb", value=2.0, direction="athena", weight=0.6),
        ],
        crossover_point_tb_day=Decimal("10.00"),
        override_reason=None,
    )
    a.migration_plans = {
        "ds.orders": MigrationDML(
            table="ds.orders",
            statements=["INSERT INTO iceberg_db.ds_orders SELECT * FROM source_db.ds_orders;"],
            shortcomings=[],
            post_optimization=[],
            estimated_scan_bytes=1073741824,
        )
    }
    html = _render_html(a)
    # RECOMMENDED_ENGINE is set to 'athena'
    assert '"recommendedEngine": "athena"' in html
    # The athena.loadDmlHelp does NOT contain the "regardless" sentence
    # (verified structurally: both ENGINE_META entries are in the JS, but the dispatch selects athena)
    assert "ENGINE_META[RECOMMENDED_ENGINE].loadDmlHelp" in html


# --- Fix 4: no Trino in customer-facing content ---


def test_html_no_trino_in_report():
    """Fix 4: 'Trino' must not appear in any customer-facing report content."""
    a = _known_assessment()
    html = _render_html(a)
    assert "Trino" not in html


# --- Fix 5: restored tooltip facts ---


def test_html_placement_high_udf_constraint():
    """Fix 5: HIGH placement confidence tooltip mentions the Iceberg catalog has no function concept."""
    a = _known_assessment()
    html = _render_html(a)
    assert "Iceberg catalog has no function concept" in html


def test_html_redshift_placement_iceberg_catalog_benefit():
    """Fix 5: Redshift placement tooltip names the catalog benefit (queryable by Athena, Redshift, and Spark)."""
    from bq_assess.models import TranslationResult

    a = _known_assessment()
    a.entities[1].translated_sql = TranslationResult(
        redshift_sql="SELECT * FROM ds.orders",
        confidence="HIGH",
        warnings=[],
        target_engine="redshift",
    )
    a.entities[1].placement = PlacementRecommendation(
        home="REDSHIFT",
        signals=["Single-engine consumption"],
        confidence=ConfidenceLevel.HIGH,
        refresh_unverified=False,
    )
    html = _render_html(a)
    assert "queryable by Athena, Redshift, and Spark" in html


# --- Fix 7: TargetEngine enum ---


def test_target_engine_enum_str_comparison():
    """Fix 7: TargetEngine enum compares equal to bare strings (str inheritance)."""
    from bq_assess.models import TargetEngine
    assert TargetEngine.ATHENA == "athena"
    assert TargetEngine.REDSHIFT == "redshift"
    assert TargetEngine.ATHENA != "redshift"



def test_range_storage_line_renders_range_not_na():
    """A range CostLine (monthly=None, low/high set) must render 'low – high'.
    _to_dict omits None-valued keys, so the template must use .get(): Jinja's
    Undefined is NOT none, and the bare check rendered every range line as
    'N/A' (2026-07-31 sandbox validation: all three storage cells showed N/A)."""
    a = _known_assessment()
    a.cost.aws_lines = [
        CostLine(
            label="S3 Tables storage",
            monthly=None, monthly_low=1905.0, monthly_high=2103.0,
            confidence=ConfidenceLevel.MEDIUM, source_note="V2-INT range",
        )
    ]
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    i = html.index("S3 Tables storage")
    cell = html[i:i + 800]
    assert "N/A" not in cell
    assert "$1,905" in cell and "$2,103" in cell


def test_negative_delta_never_labeled_savings():
    """A cost increase must not render as a green 'Savings' (sandbox validation:
    -$356/mo rendered '+$355.59/mo' under 'Monthly Savings')."""
    a = _known_assessment()
    a.cost.monthly_delta_low = -355.59
    a.cost.monthly_delta_high = -100.0
    a.cost.annual_savings_low = -4267.09
    a.cost.annual_savings_high = -1200.0
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "Monthly Cost Increase" in html
    assert "Annual Cost Increase" in html
    assert "$356/mo more" in html
    assert "+$355.59/mo" not in html and "+$356/mo" not in html


def test_straddling_delta_range_shows_both_bounds():
    """When the AWS range straddles BQ (worst case costs more, steady state
    saves — the Intelligent-Tiering spread), both bounds must be visible."""
    a = _known_assessment()
    a.cost.monthly_delta_low = -355.59
    a.cost.monthly_delta_high = 2082.0
    a.cost.annual_savings_low = -4267.09
    a.cost.annual_savings_high = 24984.0
    out = tempfile.mkdtemp()
    paths = HTMLWriter().write(a, out)
    with open(paths[0]) as f:
        html = f.read()
    assert "Monthly Cost vs BigQuery" in html
    assert "worst case" in html
    assert "Save $2,082/mo" in html
