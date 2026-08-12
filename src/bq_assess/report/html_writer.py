"""Single-file HTML report (Landing + Effort + Query tabs), offline-inlined — R20.

Renders all three views into one self-contained HTML file with JS tab navigation,
mobile-responsive layout. Uses the same serialization layer as JSONWriter.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Undefined

from bq_assess.core import pricing_constants as v4
from bq_assess.core import units
from bq_assess.core.disclaimer import (
    ADVISORY_GUIDANCE,
    AS_IS,
    BETA_STATUS,
    COST_NOT_QUOTE,
    DATA_HANDLING,
)
from bq_assess.core.query_attribution import TOP_QUERIES_PER_ENTITY
from bq_assess.engine.redshift import cost_constants as k
from bq_assess.models import Assessment
from bq_assess.report._serialize import (
    build_query_sample_chunks,
    build_report_rows,
    serialize_entities,
    serialize_landing,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _format_currency(value):
    """Jinja2 filter: format USD with commas, rounded to integer."""
    if value is None or isinstance(value, Undefined):
        return "N/A"
    return f"${value:,.0f}"


def _format_currency_precise(value):
    """Show up to 2 decimal places for all values."""
    if value is None or isinstance(value, Undefined):
        return "N/A"
    abs_val = abs(value)
    if abs_val < 0.005:
        return "$0.00"
    if abs_val < 100:
        return f"${value:,.2f}"
    return f"${value:,.0f}"


def _format_timestamp(value):
    """Render an ISO timestamp (str or datetime) in the local timezone, minute precision.

    e.g. 2026-06-29 21:42 PDT — aware values are converted to the machine's
    local timezone; naive values are displayed as-is.
    """
    if value is None or isinstance(value, Undefined):
        return "N/A"
    dt = value
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return value
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    tz = dt.strftime("%Z")
    return dt.strftime("%Y-%m-%d %H:%M") + (f" {tz}" if tz else "")


def _format_savings(value, suffix="/mo"):
    """Render savings uniformly (MRI-5): abs<$1 = 'Comparable (~$X less/more)', else Save/$X more.

    Single formatter for hero, tiles, and scenario rows — ensures consistent
    rendering across all savings-display paths. Sign semantics: positive delta =
    AWS cheaper. Negative deltas say '$X more' explicitly — the previous bare
    '+$X' under a "Savings" label read as a saving (2026-07-31 sandbox validation:
    a $356/mo cost increase rendered as '+$355.59/mo' in green).
    """
    if value is None or isinstance(value, Undefined):
        return "N/A"
    abs_val = abs(value)
    if abs_val < 1.0:
        sign = "-" if value >= 0 else "+"
        return f"Comparable ({sign}${abs_val:.2f})"
    if value < 0:
        amount = f"${abs_val:,.2f}" if abs_val < 100 else f"${abs_val:,.0f}"
        return f"{amount}{suffix} more"
    if abs_val < 100:
        return f"Save ${abs_val:,.2f}{suffix}"
    return f"Save ${abs_val:,.0f}{suffix}"


def _format_savings_annual(value):
    """Annual savings — same logic, /yr suffix.

    Rounds to the nearest dollar BEFORE the 12× so the annual figure is
    exactly 12× the displayed monthly (unrounded-delta artifact showed
    $24,987/yr beside $2,082/mo — 12×2,082=24,984; 2026-08-04 audit). The
    underlying annual is monthly×12, so value/12 recovers the monthly.
    """
    if value is None or isinstance(value, Undefined):
        return "N/A"
    if abs(value) >= 12.0:  # below this, whole-dollar rounding distorts more than it fixes
        value = round(value / 12.0) * 12
    return _format_savings(value, suffix="/yr")


def _format_size(gb):
    """Jinja-safe wrapper over the canonical size formatter (core/units.py)."""
    if gb is None or isinstance(gb, Undefined):
        return "N/A"
    return units.fmt_size(gb)


def _format_size_split(gb):
    """Return (value_str, unit_str) for templates that show the unit in a separate span."""
    if gb is None or isinstance(gb, Undefined):
        return ("N/A", "")
    return units.fmt_size_split(gb)


class HTMLWriter:
    """Write a single combined HTML report from an Assessment."""

    def __init__(self):
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=True,
        )
        self._env.filters["currency"] = _format_currency
        self._env.filters["currency_precise"] = _format_currency_precise
        self._env.filters["savings"] = _format_savings
        self._env.filters["savings_annual"] = _format_savings_annual
        self._env.filters["timestamp"] = _format_timestamp
        self._env.filters["fmt_size"] = _format_size
        self._env.filters["fmt_size_split"] = _format_size_split

    def write(
        self,
        assessment: Assessment,
        out_dir: str,
        storage_basis: str = "assumed",
        query_workloads: dict | None = None,
    ) -> list[str]:
        """Write a single combined HTML file; return list with its absolute path.

        Args:
            query_workloads: Optional dict of entity_name → {samples: [{query,
                translated, ...}]} for the embedded query-sample chunks. Only the
                TRANSLATED SAMPLES come from here — workload stats (count,
                slot-hours, shapes) live on EntityReport.query_workload and flow
                through the shared serializer to every writer.
        """
        landing_data = serialize_landing(assessment)
        effort_entities, query_entities = serialize_entities(assessment)

        query_sample_chunks: list[dict] = []
        entity_to_chunk: dict[str, int] = {}
        if query_workloads:
            query_sample_chunks, entity_to_chunk = build_query_sample_chunks(
                query_workloads
            )

        # query_workload is an EntityReport field, so the serialized dicts (and
        # the JSON sidecars) already carry the stats. Here the has_workload JS
        # flag and the entity's chunk index are added — on a shallow COPY:
        # serialize_entities memoizes its output on the assessment and shares it
        # with the JSON writer, so in-place edits would leak HTML-only keys into
        # the JSON sidecars. Gate on query_count, not samples: robust to a
        # workload carrying stats without embedded SQL (the row must still show
        # the workload column). query_chunk lets the JS parse only the one chunk
        # holding the expanded entity — walking/parsing all chunks on first
        # expand blocked the UI at petabyte scale.
        def _annotate(d: dict) -> dict:
            if not (d.get("query_workload") or {}).get("query_count"):
                return d
            out = {**d, "has_workload": True}
            chunk = entity_to_chunk.get(d["full_name"])
            if chunk is not None:
                out["query_chunk"] = chunk
            return out

        query_entities = [_annotate(d) for d in query_entities]

        report_rows, detail_chunks = build_report_rows(effort_entities, query_entities)

        rg_4xl = k.V7_RG_NODE_TYPES["rg.4xlarge"]
        rg_xl = k.V7_RG_NODE_TYPES["rg.xlarge"]
        ri_1yr_discount = round(
            (1 - rg_4xl["ri_1yr_usd_per_node_hour"] / rg_4xl["ondemand_usd_per_node_hour"]) * 100
        )
        ri_3yr_discount = round(
            (1 - rg_4xl["ri_3yr_usd_per_node_hour"] / rg_4xl["ondemand_usd_per_node_hour"]) * 100
        )

        # Compute reservation rates from the (possibly live-updated) base rate
        serverless_1yr_all_upfront_rate = round(
            k.V1_RPU_HOUR_USD * (1 - k.V1_SERVERLESS_RESERVATION_1YR_ALL_UPFRONT_DISCOUNT), 4
        )
        serverless_1yr_no_upfront_rate = round(
            k.V1_RPU_HOUR_USD * (1 - k.V1_SERVERLESS_RESERVATION_1YR_NO_UPFRONT_DISCOUNT), 4
        )
        serverless_3yr_rate = round(
            k.V1_RPU_HOUR_USD * (1 - k.V1_SERVERLESS_RESERVATION_3YR_DISCOUNT), 4
        )

        # Per-render CSP nonce: the one legitimate inline <script> carries it; any
        # script injected via a malicious BigQuery identifier (rendered into DDL/SQL
        # code blocks) will NOT, so a compliant browser refuses to execute it. Fresh
        # per file so the value is never guessable/reusable.
        script_nonce = secrets.token_urlsafe(16)

        # Expose the recommended engine as a top-level template global so client-side
        # JS can branch without depending on per-entity translated_sql (Fix 1).
        recommended_engine = "redshift"
        if landing_data.get("engine_recommendation"):
            recommended_engine = landing_data["engine_recommendation"].get(
                "primary_engine", "redshift"
            )

        # Server-side counts over entities in the default scope (REBUILT or has_workload)
        # so the Query Complexity stat cards are correct even without JavaScript.
        sql_counts = {"PORTABLE": 0, "ADAPT": 0, "REWRITE": 0, "total": 0}
        for row in report_rows["query"]:
            if row.get("population") != "REBUILT" and not row.get("has_workload"):
                continue
            sql_counts["total"] += 1
            cat = (row.get("complexity") or {}).get("category")
            if cat in sql_counts:
                sql_counts[cat] += 1

        ctx = {
            **landing_data,
            "report_rows": report_rows,
            "detail_chunks": detail_chunks,
            "query_sample_chunks": query_sample_chunks,
            "sql_counts": sql_counts,
            "recommended_engine": recommended_engine,
            "top_queries_per_entity": TOP_QUERIES_PER_ENTITY,
            "storage_basis": storage_basis,
            "csp_nonce": script_nonce,
            "disclaimer_paragraphs": [
                BETA_STATUS, COST_NOT_QUOTE, ADVISORY_GUIDANCE, AS_IS, DATA_HANDLING,
            ],
            "pricing": {
                "s3_tables_tier1_per_gb": k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER1,
                "int_ia_per_gb": k.V2_INT_IA_USD_PER_GB_MONTH,
                "int_aia_per_gb": k.V2_INT_AIA_USD_PER_GB_MONTH,
                "serverless_rpu_hr": k.V1_RPU_HOUR_USD,
                "serverless_1yr_all_upfront_rpu_hr": serverless_1yr_all_upfront_rate,
                "serverless_1yr_no_upfront_rpu_hr": serverless_1yr_no_upfront_rate,
                "serverless_1yr_all_upfront_discount_pct": round(k.V1_SERVERLESS_RESERVATION_1YR_ALL_UPFRONT_DISCOUNT * 100),
                "serverless_1yr_no_upfront_discount_pct": round(k.V1_SERVERLESS_RESERVATION_1YR_NO_UPFRONT_DISCOUNT * 100),
                "serverless_1yr_all_upfront_breakeven_pct": round(k.V1_SERVERLESS_1YR_ALL_UPFRONT_BREAKEVEN_UTIL * 100),
                "serverless_1yr_no_upfront_breakeven_pct": round(k.V1_SERVERLESS_1YR_NO_UPFRONT_BREAKEVEN_UTIL * 100),
                "serverless_3yr_rpu_hr": serverless_3yr_rate,
                "serverless_3yr_discount_pct": round(k.V1_SERVERLESS_RESERVATION_3YR_DISCOUNT * 100),
                "serverless_3yr_breakeven_pct": round(k.V1_SERVERLESS_3YR_BREAKEVEN_UTIL * 100),
                "slot_to_rpu_ratio": k.V3_SLOT_TO_RPU_RATIO,
                "rg_4xl_ondemand_hr": rg_4xl["ondemand_usd_per_node_hour"],
                "rg_xl_ondemand_hr": rg_xl["ondemand_usd_per_node_hour"],
                "ri_1yr_discount_pct": ri_1yr_discount,
                "ri_3yr_discount_pct": ri_3yr_discount,
                "hours_per_month": int(k.HOURS_PER_MONTH),
                "region": k.AWS_REGION_SCOPE,
                "aws_region": k.AWS_PRICING_REGION,
                "bq_region": v4.V4_PRICING_REGION,
                "bq_ondemand_per_tib": v4.V4_ONDEMAND_USD_PER_TIB,
                "bq_storage_active_per_gib": v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH,
                "physical_ratio": k.ASSUMED_PHYSICAL_RATIO,
            },
        }

        template = self._env.get_template("combined.html.j2")
        html = template.render(**ctx)

        filename = f"{assessment.project_id}-assessment.html"
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

        return [os.path.abspath(path)]
