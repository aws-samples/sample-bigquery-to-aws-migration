"""Unit tests for report/templates/combined.html.j2 — template source validation."""
from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).parent.parent.parent / "src" / "bq_assess" / "report" / "templates"


def test_storage_assumptions_conditional_exists():
    """Storage Assumptions section has storage_basis == 'measured' conditional."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert 'storage_basis == "measured"' in raw
    assert "Storage Assumptions" in raw


def test_storage_assumptions_measured_content():
    """When storage_basis='measured', assumptions mention TABLE_STORAGE."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "TABLE_STORAGE" in raw
    assert "measured physical bytes" in raw


def test_storage_assumptions_fallback_content():
    """When storage_basis != 'measured', assumptions interpolate pricing.physical_ratio."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "pricing.physical_ratio" in raw
    assert "Parquet compression" in raw


def test_total_size_stat_shows_both_bases():
    """Total Size card shows BigQuery logical headline plus projected S3 size with a basis tooltip."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "Total Size" in raw
    assert "summary.total_logical_size_gb" in raw   # headline = customer's console number
    assert "summary.total_size_gb" in raw           # secondary = projected S3 footprint
    assert "on S3 Tables (Iceberg)" in raw
    assert "matches your console" in raw            # tooltip explains both numbers


def test_storage_assumptions_single_iceberg_path():
    """Both engine paths bill S3 Tables — the misleading RMS-per-GB display line is gone.

    Gap analysis 2026-07-22 item 2.1: the template said 'Provisioned path: Redshift
    Managed Storage @ $X/GB' while the cost calc billed S3 Tables for both postures.
    """
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "Both engine paths:" in raw
    assert "pricing.rms_per_gb" not in raw
    assert "included with RG Graviton4 nodes" not in raw


def test_idle_ratio_narrative_serverless_framing():
    """Workload profile explains idle correctly: both serverless engines cost
    nothing while idle (each engine's billing unit is named, not a vague
    'bill zero' claim); the Athena tilt comes from the SLS 60s resume minimum
    (gap item 1.1, reworded in the 2026-07-30 text audit)."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "cost nothing while idle" in raw
    assert "per-TB scanned" in raw
    assert "60-second minimum per resume" in raw
    assert "keep-alive" in raw


def test_iceberg_features_callout_present_and_engine_gated():
    """'Performance Features on Iceberg' callout exists and only renders for the
    Redshift recommendation (gap item 2.3)."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "Redshift Performance Features on Iceberg Storage" in raw
    assert "result caching" in raw
    assert "AutoMV" in raw
    assert "zone maps / sort keys" in raw
    assert "AUTO REFRESH" in raw
    # gated on the recommended engine, not shown for Athena-primary reports
    gate_idx = raw.index('recommended_engine == "redshift"')
    callout_idx = raw.index("Redshift Performance Features on Iceberg Storage")
    assert gate_idx < callout_idx


def test_entity_size_has_reconciliation_tooltip():
    """Per-entity size column has reconciliation tooltip showing logical vs physical.

    Entity rows render client-side, so the tooltip is built by the report's JS
    renderer rather than a Jinja data-tip attribute.
    """
    raw = (TEMPLATES / "combined.html.j2").read_text()
    # The physical_size_gb rendering should have a tooltip showing logical_size_gb
    assert "e.physical_size_gb" in raw
    assert "e.logical_size_gb" in raw
    assert "'BigQuery logical: '" in raw


def test_axis_banners_present_on_both_tabs():
    """Each scoring tab carries its own axis banner explaining question + inputs
    + scale (colleague feedback 2026-07-30; sibling cross-link removed same day;
    effort banner reworded table-scoped and scope notes merged into the banners
    in the 2026-07-30 text audit)."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "How hard is it to move each table's data?" in raw
    assert "How hard is it to rewrite the SQL that runs on it?" in raw
    assert "the other axis" not in raw
    # nav tabs carry plain-language sub-captions
    assert "moving your data" in raw
    assert "rewriting your SQL" in raw
    # the old standalone scope notes were merged into the axis banners
    assert "scope-note" not in raw


def test_query_tab_defaults_to_sql_entities():
    """Query Complexity table defaults to SQL-owning entities (REBUILT population
    or has_workload) with a show-all toggle; stat cards resync with the active scope."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "function ownsSql(e) { return e.population === 'REBUILT' || e.has_workload; }" in raw
    assert "baseFilter: ownsSql" in raw
    assert 'id="query-show-all"' in raw
    assert "syncQueryCards" in raw
    assert "Show all entities" in raw


def test_workload_constructs_callout():
    """Constructs detected in collected query logs (the __ad_hoc__ surface bucket)
    surface as a workload-level callout on the Query Complexity tab."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "summary.workload_constructs" in raw
    assert "query workload" in raw


def test_intelligent_tiering_storage_table():
    """The AWS breakdown renders the per-tier derivation table when the cost
    model produced one (storage_tier_breakdown non-empty), and the storage
    assumptions carry the tiering bullet with the access-proxy caveat
    (2026-07-31)."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    assert "storage_tier_breakdown" in raw
    assert "Intelligent-Tiering" in raw
    assert "Archive Instant" in raw
    assert "millisecond" in raw                       # the latency claim
    assert "access proxy" in raw or "access history" in raw  # honesty caveat
    assert "re-heat" in raw or "back to Frequent" in raw


def test_landing_donut_scoped_to_sql_entities():
    """The landing Query Complexity donut must use the same scope as the tab
    it summarizes (SQL-owning entities), not all-entity complexity_counts —
    pdp22 showed 68 Adapt of 208 on the donut vs 7 of 7 on the tab
    (2026-08-03 sandbox review)."""
    raw = (TEMPLATES / "combined.html.j2").read_text()
    donut_block = raw[raw.index('charts.donut("Query Complexity'):]
    donut_block = donut_block[:donut_block.index("]) }}")]
    assert "sql_counts" in donut_block
    assert "summary.complexity_counts" not in donut_block


def _render_donut(segments):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    tmpl = env.from_string(
        '{% import "_donut.j2" as charts %}{{ charts.donut("T", segments) }}'
    )
    return tmpl.render(segments=segments)


def _donut_center(html: str) -> str:
    """Extract the center value+label ('100% Adapt') from rendered donut HTML."""
    import re
    m = re.search(
        r'donut__center-value">([^<]+)</span>\s*'
        r'<span class="donut__center-label">([^<]+)</span>',
        html,
    )
    assert m, "donut center markup not found"
    return f"{m.group(1)} {m.group(2)}"


def test_donut_center_shows_dominant_segment():
    """Center headline must be the biggest slice, not blindly segments[0] —
    an all-Adapt estate rendered '0% Portable' inside an all-amber ring
    (2026-08-03 sandbox review)."""
    html = _render_donut([
        {"label": "Portable", "count": 0, "color": "green"},
        {"label": "Adapt", "count": 7, "color": "amber"},
        {"label": "Rewrite", "count": 0, "color": "red"},
    ])
    assert _donut_center(html) == "100% Adapt"


def test_donut_center_tie_prefers_earlier_segment():
    """50/50 split: the earlier (better-news) segment wins the headline."""
    html = _render_donut([
        {"label": "Portable", "count": 1, "color": "green"},
        {"label": "Adapt", "count": 1, "color": "amber"},
        {"label": "Rewrite", "count": 0, "color": "red"},
    ])
    assert _donut_center(html) == "50% Portable"


def test_donut_center_majority_portable_unchanged():
    html = _render_donut([
        {"label": "Portable", "count": 140, "color": "green"},
        {"label": "Adapt", "count": 68, "color": "amber"},
        {"label": "Rewrite", "count": 0, "color": "red"},
    ])
    assert _donut_center(html) == "67% Portable"


def test_workload_summary_single_formatter():
    """2026-08-04 review: five hand-built workload phrasings had drifted into
    three terms for num_shapes. ONE formatter (wlSummary) must be the only
    producer, and the terminology is 'distinct statements' everywhere."""
    from pathlib import Path
    src = Path("src/bq_assess/report/templates/combined.html.j2").read_text()
    assert "function wlSummary(wl)" in src
    # every consumer goes through the formatter
    assert src.count("wlSummary(e.query_workload)") >= 2
    # no stray hand-built phrasings left
    assert "' unique)" not in src
    assert "query shapes" not in src
    # nested access pin: a rename of these keys must fail the suite
    assert "wl.query_count" in src and "wl.num_shapes" in src and "wl.slot_hours" in src
    # counts are locale-formatted like sibling columns
    assert "function fmtCount(n) { return Number(n).toLocaleString('en-US'); }" in src


def test_workload_summary_conditional_parenthetical():
    """'1 queries (1 unique)' regression: the parenthetical renders only when
    dedup collapsed something, and 'query' pluralizes."""
    from pathlib import Path
    src = Path("src/bq_assess/report/templates/combined.html.j2").read_text()
    assert "ns !== q" in src          # parenthetical gated on shapes != executions
    assert "q === 1 ? ' query' : ' queries'" in src
