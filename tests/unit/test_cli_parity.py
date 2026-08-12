"""CLI parity guard (2026-08-03 rule): the collection surface of bq-collect and
bq-assess must be identical — the only intended difference between the two
tools is that bq-assess also generates the report.

This test fails when a flag is added to one CLI and not the other. If you hit
it: implement the flag in BOTH `collect_cli.py` and `cli.py`'s assess options
(matching name, default, and help text), with the behavior default living in
shared code (collector.py / core/ / bundle/). If the flag is genuinely
report-side, add it to REPORT_ONLY_FLAGS below with a one-line reason.
"""
from __future__ import annotations

import click

from bq_assess.cli import assess_cmd
from bq_assess.collect_cli import main as collect_cmd

# Flags that exist only in bq-assess because they configure REPORT generation —
# the one legitimate difference between the tools. Everything else must match.
REPORT_ONLY_FLAGS = {
    "--bigquery-monthly-cost",   # cost-comparison override (report figure)
    "--config",                  # YAML config incl. engine/report sections
    "--engine",                  # which engine assessment to render
    "--export-bundle",           # bundle is bq-collect's PRODUCT, not optional there
    "--no-export-bundle",
    "--format",                  # html/json report formats
    "--interactive",             # assess prompt flow
    "--post-optimization",       # migration-plan report content
    "--no-post-optimization",
    "--query-logs",              # offline logs FILE feeds report analysis paths
    "--query-sla-ms",            # engine recommendation input
    "--skip-translation",        # report-side SQL translation stage
    "--target-region",           # engine/terraform region for the deliverables
}

# Collector-only flags: none are allowed. The collector is the constrained
# surface; anything it needs, assess's collection path needs too.
COLLECT_ONLY_FLAGS: set[str] = set()


def _option_flags(cmd: click.Command) -> set[str]:
    flags: set[str] = set()
    for param in cmd.params:
        if isinstance(param, click.Option):
            flags.update(o for o in param.opts if o.startswith("--"))
            flags.update(o for o in param.secondary_opts if o.startswith("--"))
    return flags


def test_collection_flag_surface_is_identical():
    collect_flags = _option_flags(collect_cmd) - {"--version", "--help"}
    assess_flags = _option_flags(assess_cmd) - {"--version", "--help"}

    missing_in_assess = collect_flags - assess_flags - COLLECT_ONLY_FLAGS
    assert not missing_in_assess, (
        f"bq-collect flags missing from bq-assess assess: {sorted(missing_in_assess)} — "
        "implement in cli.py's _assess_options (parity rule 2026-08-03)"
    )

    extra_in_assess = assess_flags - collect_flags - REPORT_ONLY_FLAGS
    assert not extra_in_assess, (
        f"bq-assess collection-side flags missing from bq-collect: {sorted(extra_in_assess)} — "
        "implement in collect_cli.py, or add to REPORT_ONLY_FLAGS with a reason "
        "(parity rule 2026-08-03)"
    )


def test_shared_flags_have_matching_defaults():
    """A flag existing in both CLIs with different defaults is the worst drift
    (same command, different behavior) — the --include-query-logs class."""
    collect_opts = {
        o: p for p in collect_cmd.params if isinstance(p, click.Option)
        for o in p.opts if o.startswith("--")
    }
    assess_opts = {
        o: p for p in assess_cmd.params if isinstance(p, click.Option)
        for o in p.opts if o.startswith("--")
    }
    mismatches = []
    for flag in set(collect_opts) & set(assess_opts):
        c, a = collect_opts[flag], assess_opts[flag]
        if c.default != a.default:
            # --output legitimately differs: the collector writes a bundle dir,
            # assess writes the full deliverable tree.
            if flag == "--output":
                continue
            # assess uses default=None as a NOT-SPECIFIED sentinel so YAML
            # config can layer under the CLI; the effective default then comes
            # from shared code (e.g. collector.py's `query_log_days or 30`).
            # Same for --gcp-project: required in collect, sentinel-None in
            # assess because --config/--interactive may supply it. Only a
            # CONCRETE divergent default is drift.
            if a.default is None:
                continue
            mismatches.append(f"{flag}: collect={c.default!r} assess={a.default!r}")
    assert not mismatches, (
        "Shared flags with divergent defaults (move the default into shared "
        f"collector.py code): {mismatches}"
    )
