"""Unit tests for report/_serialize.py — shared serialization helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

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
)
from bq_assess.report._serialize import (
    _EFFORT_ROW_KEYS,
    _QUERY_ROW_KEYS,
    _to_dict,
    build_report_rows,
    serialize_entities,
)


class _Color(Enum):
    RED = "RED"
    BLUE = "BLUE"


def test_enum_serializes_to_value():
    assert _to_dict(_Color.RED) == "RED"


def test_datetime_serializes_to_isoformat():
    dt = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert _to_dict(dt) == "2026-06-17T12:00:00+00:00"


def test_none_fields_omitted_from_dict():
    from dataclasses import dataclass

    @dataclass
    class _Sample:
        a: str
        b: str | None = None

    result = _to_dict(_Sample(a="hello", b=None))
    assert result == {"a": "hello"}


def test_nested_dataclass_recursion():
    from dataclasses import dataclass

    @dataclass
    class _Inner:
        x: int

    @dataclass
    class _Outer:
        inner: _Inner
        name: str

    result = _to_dict(_Outer(inner=_Inner(x=42), name="test"))
    assert result == {"inner": {"x": 42}, "name": "test"}


def test_list_of_dataclasses():
    from dataclasses import dataclass

    @dataclass
    class _Item:
        val: int

    result = _to_dict([_Item(val=1), _Item(val=2)])
    assert result == [{"val": 1}, {"val": 2}]


def _make_entity(full_name, population, entity_type):
    """Minimal EntityReport for filter tests."""
    effort = None
    conversion = None
    if population is EntityPopulation.TABLE:
        effort = EffortResult(
            category=EffortCategory.AUTO, score=0, flags=[], reasoning="", confidence=ConfidenceLevel.HIGH
        )
        conversion = ConversionResult(ddl="CREATE TABLE t (id long);", partition_mapping=None, lossy_casts=[], warnings=[], success=True)
    return EntityReport(
        full_name=full_name, entity_type=entity_type, population=population,
        rows=100, size_gb=1.0, depends_on=[],
        effort=effort, conversion=conversion, load_sync_dml=None,
        complexity=ComplexityResult(
            category=ComplexityCategory.PORTABLE, score=0, constructs=[], flags=[],
            reasoning="", confidence=ConfidenceLevel.HIGH, confidence_source=ConfidenceSource.SCHEMA_ONLY,
        ),
        rewrite_guidance=[], placement=None,
    )


def _minimal_assessment(entities):
    return Assessment(
        assessment_id="assess-20260617-abc123",
        generated_at=datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc),
        project_id="test-proj",
        summary=AssessmentSummary(
            total_entities=len(entities), total_tables=sum(1 for e in entities if e.population is EntityPopulation.TABLE),
            total_size_gb=0.0, effort_counts={"AUTO": 0, "ASSISTED": 0, "MANUAL": 0},
            complexity_counts={"PORTABLE": 0, "ADAPT": 0, "REWRITE": 0},
            sql_surface_confidence=ConfidenceLevel.HIGH,
        ),
        cost=CostComparison(
            bq_pricing_model=BQPricingModel.ON_DEMAND, bigquery_monthly=1000.0,
            bigquery_breakdown=[], aws_lines=[CostLine(label="s3", monthly=100.0, monthly_low=None, monthly_high=None, confidence=ConfidenceLevel.HIGH, source_note="test")],
            aws_monthly_low=100.0, aws_monthly_high=100.0,
            monthly_delta_low=900.0, monthly_delta_high=900.0,
            annual_savings_low=10800.0, annual_savings_high=10800.0,
            migration_onetime=500.0, breakeven_months_low=0.56, breakeven_months_high=0.56,
            compute_confidence=ConfidenceLevel.HIGH,
        ),
        entities=entities, failures=[],
    )


def test_effort_filters_tables_only():
    entities = [
        _make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE),
        _make_entity("ds.view1", EntityPopulation.REBUILT, EntityType.VIEW),
    ]
    effort, _ = serialize_entities(_minimal_assessment(entities))
    assert len(effort) == 1
    assert effort[0]["full_name"] == "ds.tbl"


def test_query_includes_all_entities():
    entities = [
        _make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE),
        _make_entity("ds.view1", EntityPopulation.REBUILT, EntityType.VIEW),
    ]
    _, query = serialize_entities(_minimal_assessment(entities))
    assert len(query) == 2
    names = {e["full_name"] for e in query}
    assert names == {"ds.tbl", "ds.view1"}


def test_serialize_entities_memoized():
    """JSONWriter and HTMLWriter both serialize per run — the walk must happen once."""
    entities = [_make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE)]
    a = _minimal_assessment(entities)
    first = serialize_entities(a)
    assert serialize_entities(a) is first


def test_report_rows_prune_nested_dead_fields():
    """The embedded report JSON must not carry fields the renderer never reads —
    especially complexity.constructs, whose anonymized SQL snippets dominate the
    payload at query-log scale."""
    from bq_assess.models import DetectedConstruct, PlacementRecommendation

    entities = [_make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE)]
    entities[0].complexity.constructs = [
        DetectedConstruct(construct_class="UNNEST", snippet="SELECT * FROM UNNEST(?)", description="x")
    ]
    entities[0].placement = PlacementRecommendation(
        home="REDSHIFT", signals=["s"], confidence=ConfidenceLevel.HIGH, refresh_unverified=False
    )
    effort, query = serialize_entities(_minimal_assessment(entities))
    rows, chunks = build_report_rows(effort, query)

    q = rows["query"][0]
    assert "constructs" not in q["complexity"]
    assert "refresh_unverified" not in q["placement"]
    # Heavy fields move to the detail chunks — pruned there, absent from rows.
    e = rows["effort"][0]
    assert "conversion" not in e
    assert e["detail_chunk"] == 0
    detail = chunks[0]["ds.tbl"]
    assert "success" not in detail["conversion"]
    # Pruning must not mutate the shared dicts the JSON sidecars serialize.
    assert query[0]["complexity"]["constructs"], "sidecar dict was mutated"
    assert query[0]["conversion"]["success"] is not None, "sidecar dict was mutated"


def test_report_rows_drop_unscored_entities():
    entities = [
        _make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE),
        _make_entity("ds.view1", EntityPopulation.REBUILT, EntityType.VIEW),
    ]
    entities[1].complexity = None
    effort, query = serialize_entities(_minimal_assessment(entities))
    rows, _ = build_report_rows(effort, query)
    assert [r["full_name"] for r in rows["query"]] == ["ds.tbl"]


def test_detail_chunking_splits_and_indexes():
    """Heavy fields land in DETAIL_CHUNK_SIZE-entity chunks; each row's detail_chunk
    points at the chunk holding its payload; entities without heavy fields get none."""
    from bq_assess.report import _serialize as ser

    entities = [
        _make_entity(f"ds.tbl{i:03d}", EntityPopulation.TABLE, EntityType.TABLE)
        for i in range(5)
    ]
    entities[3].conversion = None  # no heavy payload → no chunk assignment
    effort, query = serialize_entities(_minimal_assessment(entities))

    original = ser.DETAIL_CHUNK_SIZE
    ser.DETAIL_CHUNK_SIZE = 2
    try:
        rows, chunks = build_report_rows(effort, query)
    finally:
        ser.DETAIL_CHUNK_SIZE = original

    # 4 entities with conversions → chunks of 2
    assert [len(c) for c in chunks] == [2, 2]
    with_chunk = [r for r in rows["effort"] if "detail_chunk" in r]
    assert len(with_chunk) == 4
    assert "detail_chunk" not in rows["effort"][3]
    for r in with_chunk:
        assert r["full_name"] in chunks[r["detail_chunk"]]
        assert "conversion" in chunks[r["detail_chunk"]][r["full_name"]]


def test_report_rows_cover_template_accesses():
    """Pin the two-sided contract: every top-level `e.<field>` the report's JS renderer
    reads must be in the corresponding allowlist, so a renderer change that forgets to
    extend the allowlist fails here instead of rendering undefined in the browser."""
    import re
    from pathlib import Path

    template = (
        Path(__file__).parent.parent.parent
        / "src" / "bq_assess" / "report" / "templates" / "combined.html.j2"
    ).read_text()
    script = template[template.index("</noscript>"):]

    # Row-level metadata added by the renderer itself, not the serializer; plus
    # DOM event API (e.target, e.preventDefault) that regex can't distinguish from entity fields.
    renderer_owned = {"_nameLower", "target"}
    accessed = set(re.findall(r"\be\.([a-z_]+)\b", script)) - renderer_owned
    # Detail builders receive detailFor(e) — row fields merged with the entity's
    # lazy detail chunk — so heavy _DETAIL_KEYS reads are part of the contract too.
    from bq_assess.report._serialize import _DETAIL_KEYS

    allowed = set(_EFFORT_ROW_KEYS) | set(_QUERY_ROW_KEYS) | set(_DETAIL_KEYS)
    assert accessed <= allowed, f"JS reads fields missing from allowlists: {accessed - allowed}"


def test_serialize_landing_includes_engine_fields():
    """Regression: serialize_landing must include engine_recommendation and migration_plans."""
    import json
    from decimal import Decimal

    from bq_assess.models import (
        EngineRecommendation,
        MigrationDML,
        SignalContribution,
    )
    from bq_assess.report._serialize import serialize_landing

    # Build minimal assessment with engine fields
    entities = [_make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE)]
    assessment = _minimal_assessment(entities)

    # Add engine_recommendation with Decimal field (tests Decimal → float conversion)
    assessment.engine_recommendation = EngineRecommendation(
        primary_engine="athena",
        confidence=0.85,
        reasoning=[
            SignalContribution(signal="cost_savings", value=0.6, direction="athena", weight=0.8)
        ],
        crossover_point_tb_day=Decimal("15.5"),  # Decimal type
        override_reason=None,
    )

    # Add migration_plans
    assessment.migration_plans = {
        "ds.tbl": MigrationDML(
            table="ds.tbl",
            statements=["INSERT INTO iceberg.ds.tbl SELECT * FROM bq.ds.tbl;"],
            shortcomings=[],
            post_optimization=[],
            estimated_scan_bytes=1000000,
        )
    }

    # Serialize and verify JSON encoding works (tests Decimal handling)
    result = serialize_landing(assessment)
    json_str = json.dumps(result)  # Would raise TypeError if Decimal not converted
    parsed = json.loads(json_str)

    # Verify both fields present
    assert "engine_recommendation" in parsed
    assert parsed["engine_recommendation"]["primary_engine"] == "athena"
    assert parsed["engine_recommendation"]["crossover_point_tb_day"] == 15.5  # float now

    # migration_plans is now a slim summary (Fix 7: kill double serialization)
    assert "migration_plans" in parsed
    assert parsed["migration_plans"]["tables"] == 1
    assert parsed["migration_plans"]["chunked"] == 0
    assert parsed["migration_plans"]["action_required"] == 0


def test_athena_one_time_optimize_serializes():
    """Verify athena_one_time_optimize flows through serialization (Decimal → float)."""
    import json
    from decimal import Decimal

    from bq_assess.report._serialize import serialize_landing

    entities = [_make_entity("ds.tbl", EntityPopulation.TABLE, EntityType.TABLE)]
    assessment = _minimal_assessment(entities)

    # Set athena_one_time_optimize on the cost comparison
    import dataclasses
    assessment.cost = dataclasses.replace(
        assessment.cost,
        athena_one_time_optimize=Decimal("123.45")
    )

    result = serialize_landing(assessment)
    json_str = json.dumps(result)  # Would raise TypeError if Decimal not converted
    parsed = json.loads(json_str)

    # Verify field is present and converted to float
    assert "cost" in parsed
    assert "athena_one_time_optimize" in parsed["cost"]
    assert parsed["cost"]["athena_one_time_optimize"] == 123.45
    assert isinstance(parsed["cost"]["athena_one_time_optimize"], float)
