"""One provenance block, stamped on every artifact the tool emits (#61).

Before this, only the landing JSON said what produced it. The effort and query
JSON carried `assessment_id` alone, `plan.json` carried nothing, and the HTML,
MIGRATION_GUIDE and run_migration.py carried no version at all. A file handed
around on its own could not be attributed to a project, a run, or a tool
version — and the advisory that the scores need engineering review reached the
HTML reader but never a programmatic consumer of the JSON.

## Why schema_version is NOT the package version

It used to be `__version__`, which cannot do the job it was added for. The
original note on `serialize_landing` says it exists so consumers can "gate on
field semantics" — but tied to the package version it moves on every release
that changes nothing structural, and stays put when semantics genuinely shift.
Both failure directions were observed in one day:

  * `migration_plans.chunked` went 0 -> 5 on identical source data (#53)
  * effort counts went AUTO 8/ASSISTED 2/MANUAL 0 -> 7/1/2, same data (#74)

both at `schema_version: "0.8.0"`. A consumer had no way to notice either.

So REPORT_SCHEMA_VERSION is now an integer bumped BY HAND when the contract
changes, and `tool_version` carries the package version separately. Changing
this is itself a contract change, which is exactly why it is happening before
anything consumes the JSON rather than after.

## When to bump REPORT_SCHEMA_VERSION

Bump it when a consumer that worked before could now be wrong: a field removed
or renamed, a field's meaning changed under the same key, or a value's units or
derivation changed. Do NOT bump for additive fields — those are safe by
construction — or for prose/wording edits.
"""
from __future__ import annotations

from bq_assess.core.disclaimer import ADVISORY_GUIDANCE

# History:
#   1  implicit — everything up to 2026-10-04, when schema_version was the
#      package version string and could not signal a contract change
#   2  schema_version became this integer; tool_version split out; every
#      artifact gained the provenance block; migration_plans gained
#      load_blocking + action_required_review_only (#68)
REPORT_SCHEMA_VERSION = 2


def provenance(assessment) -> dict:
    """Identity + advisory fields common to every emitted artifact.

    ``advisory`` is included deliberately: effort categories and complexity
    classifications are best-effort heuristics, and a consumer reading only the
    JSON would otherwise receive them with none of the qualification the HTML
    reader gets.
    """
    from bq_assess import __version__

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": __version__,
        "assessment_id": assessment.assessment_id,
        "project_id": assessment.project_id,
        "generated_at": assessment.generated_at.isoformat(),
        "advisory": ADVISORY_GUIDANCE,
    }
