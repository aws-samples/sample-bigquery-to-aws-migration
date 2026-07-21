"""Tests for Athena placement advisor."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bq_assess.engine.athena.placement import AthenaPlacementAdvisor
from bq_assess.models import (
    EntityMetadata,
    EntityPopulation,
    EntityType,
    RoutineMetadata,
)


def _entity(entity_type: EntityType, population: EntityPopulation, **kwargs) -> EntityMetadata:
    defaults = dict(
        entity_id="test",
        dataset_id="ds",
        full_name="ds.test",
        entity_type=entity_type,
        population=population,
        num_rows=0,
        num_bytes=0,
        columns=[],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return EntityMetadata(**defaults)


@pytest.fixture
def advisor():
    return AthenaPlacementAdvisor()


def test_table_returns_none(advisor):
    entity = _entity(EntityType.TABLE, EntityPopulation.TABLE)
    assert advisor.recommend(entity) is None


def test_view_returns_placement(advisor):
    entity = _entity(EntityType.VIEW, EntityPopulation.REBUILT, view_query="SELECT 1")
    result = advisor.recommend(entity)
    assert result is not None
    assert result.engine_id == "athena"
    assert "VIEW" in result.home or "view" in result.home.lower()


def test_mv_flagged_as_unsupported(advisor):
    entity = _entity(EntityType.MATERIALIZED_VIEW, EntityPopulation.REBUILT, mview_query="SELECT 1")
    result = advisor.recommend(entity)
    assert result is not None
    assert any("cannot create" in g.lower() or "unsupported" in g.lower() for g in result.gaps)


def test_js_udf_flagged(advisor):
    routine = RoutineMetadata(
        name="my_udf", language="JAVASCRIPT", arguments=[], body="return 1;", routine_type="SCALAR_FUNCTION"
    )
    entity = _entity(EntityType.ROUTINE, EntityPopulation.REBUILT, routine=routine)
    result = advisor.recommend(entity)
    assert result is not None
    assert any("javascript" in g.lower() or "js" in g.lower() for g in result.gaps)


def test_sql_udf_requires_lambda(advisor):
    """SQL UDFs require Lambda in Athena (no CREATE FUNCTION)."""
    routine = RoutineMetadata(
        name="my_fn", language="SQL", arguments=["x INT64"], body="x + 1", routine_type="SCALAR_FUNCTION"
    )
    entity = _entity(EntityType.ROUTINE, EntityPopulation.REBUILT, routine=routine)
    result = advisor.recommend(entity)
    assert result is not None
    assert result.home == "LAMBDA_UDF_REQUIRED"
    assert result.confidence == "HIGH"
    assert any("lambda" in g.lower() or "create function" in g.lower() for g in result.gaps)


def test_stored_procedure_flagged(advisor):
    routine = RoutineMetadata(
        name="my_proc", language="SQL", arguments=[], body="BEGIN END", routine_type="PROCEDURE"
    )
    entity = _entity(EntityType.ROUTINE, EntityPopulation.REBUILT, routine=routine)
    result = advisor.recommend(entity)
    assert result is not None
    assert any("procedure" in g.lower() for g in result.gaps)
