"""Athena Placement Advisor — recommend home for Views/MVs/UDFs.

Athena can: CREATE VIEW.
Athena cannot: CREATE MATERIALIZED VIEW, CREATE FUNCTION (any UDF — SQL or JS),
stored procedures. UDFs require AWS Lambda invoked via USING EXTERNAL FUNCTION.
"""
from __future__ import annotations

from typing import Optional

from bq_assess.models import (
    EnginePlacement,
    EntityMetadata,
    EntityPopulation,
    EntityType,
)


class AthenaPlacementAdvisor:
    """Recommend placement for REBUILT entities in the Athena engine."""

    engine_id = "athena"

    def recommend(
        self, entity: EntityMetadata, has_logs: bool = False
    ) -> Optional[EnginePlacement]:
        if entity.population == EntityPopulation.TABLE:
            return None

        if entity.entity_type == EntityType.VIEW:
            return self._recommend_view(entity)
        if entity.entity_type == EntityType.MATERIALIZED_VIEW:
            return self._recommend_mv(entity)
        if entity.entity_type == EntityType.ROUTINE:
            return self._recommend_routine(entity)

        return None

    def _recommend_view(self, entity: EntityMetadata) -> EnginePlacement:
        return EnginePlacement(
            engine_id=self.engine_id,
            home="CREATE VIEW",
            signals=["Athena supports CREATE VIEW natively"],
            confidence="HIGH",
            gaps=[],
        )

    def _recommend_mv(self, entity: EntityMetadata) -> EnginePlacement:
        return EnginePlacement(
            engine_id=self.engine_id,
            home="UNSUPPORTED",
            signals=["Athena cannot CREATE MATERIALIZED VIEW (parse-level rejection)"],
            confidence="HIGH",
            gaps=[
                "Cannot create MVs in Athena — use Spark/Glue to create, Athena can read as Iceberg table"
            ],
        )

    def _recommend_routine(self, entity: EntityMetadata) -> EnginePlacement:
        if not entity.routine:
            return EnginePlacement(
                engine_id=self.engine_id,
                home="UNKNOWN",
                signals=[],
                confidence="LOW",
                gaps=["No routine metadata available"],
            )

        routine = entity.routine
        gaps = []

        if routine.language and routine.language.upper() == "JAVASCRIPT":
            gaps.append("JavaScript UDF has no Athena equivalent — no JS runtime available")
            return EnginePlacement(
                engine_id=self.engine_id,
                home="UNSUPPORTED",
                signals=["JS UDFs not available in Athena"],
                confidence="HIGH",
                gaps=gaps,
            )

        if routine.routine_type and routine.routine_type.upper() == "PROCEDURE":
            gaps.append("Stored procedures not supported in Athena")
            return EnginePlacement(
                engine_id=self.engine_id,
                home="UNSUPPORTED",
                signals=["Athena has no stored procedure support"],
                confidence="HIGH",
                gaps=gaps,
            )

        # SQL scalar UDF — Athena has no CREATE FUNCTION
        gaps.append(
            "Athena has no CREATE FUNCTION; scalar UDFs require an AWS Lambda function invoked via "
            "USING EXTERNAL FUNCTION per query — consider inlining simple SQL UDF bodies into consuming queries instead"
        )
        return EnginePlacement(
            engine_id=self.engine_id,
            home="LAMBDA_UDF_REQUIRED",
            signals=["SQL UDFs must be implemented as Lambda functions in Athena"],
            confidence="HIGH",
            gaps=gaps,
        )
