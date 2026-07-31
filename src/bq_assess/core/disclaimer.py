"""Single source of truth for beta/legal disclaimers (both distributions)."""

DISCLAIMER_VERSION = 1

BETA_STATUS = (
    "This tool is in beta. Features, scoring models, and cost calculations are under "
    "active development and may change without notice."
)

COST_NOT_QUOTE = (
    "All cost figures are directional estimates based on published list pricing, "
    "detected or assumed usage patterns, and stated assumptions at the time of "
    "generation. They are not a quote, offer, or commitment from Amazon Web Services "
    "or Google. Actual costs depend on final architecture, negotiated pricing, usage, "
    "and region. Refer to official AWS and Google Cloud pricing for authoritative rates."
)

ADVISORY_GUIDANCE = (
    "Migration effort scores, complexity classifications, generated DDL/DML, SQL "
    "translations, and placement recommendations are automated, best-effort guidance. "
    "They do not replace engineering review. Independently validate all generated code, "
    "recommendations, and cost figures before using them in any environment or for "
    "planning and budgeting decisions."
)

AS_IS = (
    'This software is provided "AS IS", without warranties or conditions of any kind, '
    "express or implied, per the Apache License 2.0. Use at your own risk."
)

DATA_HANDLING = (
    "This tool performs read-only operations against BigQuery metadata and "
    "INFORMATION_SCHEMA. It does not read table data contents. The output bundle "
    "contains schema metadata, aggregated workload statistics, and anonymized query "
    "text (literals stripped; use --exclude-query-text to omit query text entirely). "
    "You are responsible for reviewing bundle contents before transmitting them "
    "outside your environment."
)

FULL_DISCLAIMER = f"{BETA_STATUS}\n\n{COST_NOT_QUOTE}\n\n{ADVISORY_GUIDANCE}\n\n{AS_IS}\n\n{DATA_HANDLING}"

CLI_ONE_LINER = (
    "⚠ BETA — estimates are directional, not a pricing quote. "
    "See report footer for full disclaimer."
)
