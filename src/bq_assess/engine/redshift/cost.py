"""Cost comparison — BigQuery vs the AWS lakehouse (R18).

``CostEstimator.estimate()`` produces the honest run-rate comparison that is the business-case
headline of the tool. The AWS side evaluates **multiple deployment scenarios** — Serverless,
Provisioned On-Demand, Provisioned 1yr RI, Provisioned 3yr RI — selects the best-fit option
based on the customer's actual workload profile, and provides a justified recommendation.

Storage is always **decoupled** — S3 Tables (V2) for serverless, Managed Storage (V6) for
provisioned — independent of compute choice.

The recommendation engine uses customer-specific workload metrics (queries/day, bytes scanned,
concurrent query load, active hours) to size provisioned clusters and compare against serverless.
Justification text references the customer's actual numbers, never generic assumptions.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from datetime import datetime, timedelta, timezone

from bq_assess.core import pricing_constants as v4
from bq_assess.core.units import fmt_size_exact
from bq_assess.engine.redshift import cost_constants as k
from bq_assess.models import (
    AWSRecommendation,
    AWSScenario,
    BQPricingModel,
    ConfidenceLevel,
    CostComparison,
    CostLine,
    PricingDetection,
    SlotUtilization,
    WorkloadProfile,
)

_log = logging.getLogger(__name__)

_MS_PER_HOUR = 3_600_000
# Workload windows at least this long count as "measured" in the estimate-basis sentence;
# shorter windows are called out as thin samples. (Was the HIGH/MEDIUM cut for the
# serverless compute badge before the 2026-07-15 confidence consolidation.)
_DAYS_MEASURED = 7

_CONFIDENCE_ORDER = {
    ConfidenceLevel.LOW: 0,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.HIGH: 2,
}


class CostEstimator:
    """Compute the BigQuery-vs-AWS cost comparison (R18)."""

    def __init__(self, *, skip_live_pricing: bool = False):
        # Refresh once per (bq_location, aws_region) pair — a reused estimator pricing
        # a second Source in a different geography needs its own live lookup (a plain
        # once-per-instance flag silently skipped it and left the snapshot rates).
        self._refreshed_pairs: set[tuple[str, str]] = set()
        self._skip_live_pricing = skip_live_pricing
        self._longterm_bytes: int = 0  # set per estimate() call from last_modified

    def _refresh_pricing(self, bq_location: str, aws_region: str) -> None:
        """Attempt live pricing lookup; updates module constants and confirmed dates."""
        if self._skip_live_pricing or (bq_location, aws_region) in self._refreshed_pairs:
            return
        self._refreshed_pairs.add((bq_location, aws_region))
        try:
            from bq_assess.core.price_lookup import PriceLookup, apply_live_rates
            rates = PriceLookup(aws_region=aws_region, bq_location=bq_location).fetch()
            apply_live_rates(rates)
        except Exception as exc:
            _log.warning("Live pricing refresh failed, using hardcoded rates: %s", exc)

    def estimate(
        self,
        entities,
        pricing: PricingDetection,
        slots: SlotUtilization | None,
        bq_monthly_override: float | None,
        effort_total,
        *,
        location: str | None = None,
        storage_basis: str = "assumed",
        as_of: datetime | None = None,
        egress_gib: float | None = None,
    ) -> CostComparison:
        # ---- Region cascade: price BOTH clouds in the Source's geography (2026-07-02) ----
        # BigQuery rates re-resolve to the detected dataset location; AWS rates re-resolve
        # to the nearest AWS region so the comparison is like-for-like. A live pricing
        # refresh then overrides with current catalog rates for the same regions.
        # Ordering rules (review fixes 2026-07-03/04):
        # - The region tags (V4_PRICING_REGION / AWS_PRICING_REGION) are stamped by BOTH
        #   writers — apply_*_region AND apply_live_rates — so "tag == requested region"
        #   reliably means the constants already reflect that geography (hardcoded or
        #   live). When it holds, do NOT re-apply: re-applying would clobber live rates
        #   back to the hardcoded snapshot. This also protects locations OUTSIDE the
        #   hardcoded table whose rates came solely from the live Billing Catalog lookup
        #   (the CLI applies live rates in Stage 9b before calling estimate()).
        # - If the location is unknown to the table AND no live rates were applied for
        #   it, RESET to US multi-region rather than silently keeping whatever region a
        #   previous estimate left in the module constants.
        # - location=None preserves the module constants as-is (R18.7 override contract);
        #   bq_pricing_region on the result reports whatever region they reflect.
        region_known = True
        if location is not None:
            bq_location = v4.normalize_bq_location(location) or "us"
            if v4.V4_PRICING_REGION != bq_location:
                region_known = v4.apply_bq_region(bq_location)
                if not region_known:
                    v4.apply_bq_region("us")   # reset — never inherit a previous region
                    _log.warning(
                        "No verified rate table for BigQuery location %r — pricing at US "
                        "multi-region rates unless a live catalog lookup resolves it",
                        bq_location,
                    )
            aws_region, _is_fallback = k.bq_location_to_aws_region(bq_location)
            if k.AWS_PRICING_REGION != aws_region:
                k.apply_aws_region(aws_region)
        else:
            bq_location = v4.V4_PRICING_REGION

        self._refresh_pricing(bq_location, k.AWS_PRICING_REGION)
        # Post-refresh reconcile: a live Billing Catalog lookup covers ~49 regions vs the
        # hardcoded table's subset — if it just resolved this location (stamping the tag),
        # the "priced at US rates" caveat would be false. Trust the tag.
        if not region_known and v4.V4_PRICING_REGION == bq_location:
            region_known = True
        total_bytes = sum(_entity_bytes(e) for e in entities)
        total_physical_bytes = sum(_entity_physical_bytes(e) for e in entities)
        # Long-term split (V4): a table untouched for 90+ days bills at ~50% of the
        # active-logical rate. last_modified is collected per entity, so the BQ side
        # must honor it — pricing everything active overstates the customer's current
        # bill (67% of one audited estate was long-term-eligible).
        self._longterm_bytes = _longterm_bytes(entities, as_of)
        # S3 Tables Intelligent-Tiering split (V2-INT): the AWS-side mirror of the
        # long-term split above — symmetric treatment of both clouds' aging
        # discounts. Consumed by _aws_s3_storage_line as the steady-state low bound
        # and rendered as the report's per-tier derivation table. All-Frequent
        # (no cold bytes) keeps the storage line a point.
        self._int_tiers = _int_tier_breakdown(entities, as_of)

        # ---- BigQuery side: per detected model, override wins (R18.2) ----
        bigquery_monthly, bq_breakdown, bq_avail = self._bigquery_runrate(
            total_bytes, pricing, slots, bq_monthly_override
        )

        # ---- Egress: add BQ egress line if Cloud Monitoring data was collected ----
        if egress_gib is not None and egress_gib > 0:
            bq_egress_usd = egress_gib * v4.V4_EGRESS_USD_PER_GIB
            bq_breakdown.append(CostLine(
                label="BigQuery Storage Read API egress",
                monthly=round(bq_egress_usd, 4),
                monthly_low=None, monthly_high=None,
                confidence=ConfidenceLevel.MEDIUM,
                source_note=(
                    f"{egress_gib:,.1f} GiB/mo × ${v4.V4_EGRESS_USD_PER_GIB}/GiB "
                    f"({v4.V4_PRICING_REGION}) — inferred from Cloud Monitoring "
                    f"CreateReadSession count × avg table size"
                ),
            ))
            if bq_avail["available"]:
                bigquery_monthly = round(bigquery_monthly + bq_egress_usd, 4)
        self._egress_gib = egress_gib

        # ---- AWS side: evaluate all scenarios ----
        profile = self._build_workload_profile(slots, total_bytes)
        scenarios = self._evaluate_all_scenarios(
            total_bytes, total_physical_bytes, storage_basis, slots, profile
        )

        # ---- Select best-fit and generate recommendation ----
        recommendation = self._generate_recommendation(
            scenarios, profile, bigquery_monthly
        )

        # Mark the recommended scenario. A scenario can't be both recommended and demoted:
        # if the picker chose one carrying a not_recommended_reason (e.g. a below-break-even
        # reservation that still prices cheapest), the fit concern is resolved by being
        # chosen — clear the demotion rather than render a contradictory report.
        for s in scenarios:
            s.is_recommended = (s.label == recommendation.recommended_scenario)
            if s.is_recommended:
                s.not_recommended_reason = ""

        # Use the recommended scenario for headline numbers
        best = next((s for s in scenarios if s.is_recommended), scenarios[0])
        aws_monthly_low = sum(_line_low(ln) for ln in best.lines)
        aws_monthly_high = sum(_line_high(ln) for ln in best.lines)

        # ---- deltas / annual / break-even ----
        # Range-basis BQ cost (2026-08-11): when the BQ side is a modelled range
        # (STANDARD capacity), the committable savings floor must be computed
        # against the BQ measured minimum, not the upper estimate — anchoring
        # savings to the upper figure alone flatters AWS one-sidedly.
        bigquery_monthly_low = _bq_breakdown_low(bq_breakdown, bigquery_monthly)
        if bq_avail["available"]:
            bq_low_basis = (
                bigquery_monthly_low if bigquery_monthly_low is not None
                else bigquery_monthly
            )
            monthly_delta_low = bq_low_basis - aws_monthly_high
            monthly_delta_high = bigquery_monthly - aws_monthly_low
        else:
            monthly_delta_low = 0.0
            monthly_delta_high = 0.0
        annual_savings_low = monthly_delta_low * 12
        annual_savings_high = monthly_delta_high * 12
        migration_onetime = _safe_num(effort_total) * k.MIGRATION_USD_PER_EFFORT_POINT
        breakeven_low = _breakeven(migration_onetime, monthly_delta_low)
        breakeven_high = _breakeven(migration_onetime, monthly_delta_high)

        basis_level, basis_text = self._estimate_basis(
            bq_breakdown, best, slots, bq_monthly_override
        )

        return CostComparison(
            bq_pricing_model=pricing.model,
            bigquery_monthly=bigquery_monthly,
            bigquery_monthly_low=bigquery_monthly_low,
            bigquery_breakdown=bq_breakdown,
            aws_lines=best.lines,
            aws_monthly_low=aws_monthly_low,
            aws_monthly_high=aws_monthly_high,
            monthly_delta_low=monthly_delta_low,
            monthly_delta_high=monthly_delta_high,
            annual_savings_low=annual_savings_low,
            annual_savings_high=annual_savings_high,
            migration_onetime=migration_onetime,
            breakeven_months_low=breakeven_low,
            breakeven_months_high=breakeven_high,
            compute_confidence=best.confidence,
            aws_scenarios=scenarios,
            recommendation=recommendation,
            bq_pricing_region=v4.V4_PRICING_REGION,
            aws_pricing_region=k.AWS_PRICING_REGION,
            bq_cost_available=bq_avail["available"],
            bq_cost_basis=bq_avail["basis"],
            bq_cost_unavailable_reason=bq_avail["reason"],
            scope_notes=self._scope_notes(),
            pricing_notes=self._pricing_notes(bq_location, region_known, bq_monthly_override),
            key_uncertainties=self._key_uncertainties(slots),
            estimate_basis_level=basis_level,
            estimate_basis=basis_text,
            storage_tier_breakdown=self._storage_tier_rows(total_physical_bytes),
        )

    def _estimate_basis(
        self, bq_breakdown, best, slots: SlotUtilization | None, override,
    ) -> tuple[ConfidenceLevel, str]:
        """The one always-visible confidence statement for the cost section (2026-07-15).

        Replaces the per-scenario and per-line badge scatter (11 badges on a typical
        summary page) with a single plain-language sentence naming what the estimate was
        priced from. Level = minimum across the BigQuery breakdown and the recommended
        option's lines — the chain is as strong as its weakest input, so it caps at
        MEDIUM while the slot→RPU ratio is an assumption. The uncertainties themselves
        live in ``key_uncertainties`` (2026-07-16 restructure: one home per statement).
        """
        lines = list(bq_breakdown) + list(best.lines)
        level = min((ln.confidence for ln in lines), key=lambda c: _CONFIDENCE_ORDER.get(c, 0), default=ConfidenceLevel.LOW)

        if override is not None:
            text = "BigQuery cost is operator-supplied (--bigquery-monthly-cost)."
        elif slots is not None and slots.days_sampled > 0:
            window = f"{_window_days(slots)} days of measured workload"
            volume = f"{slots.total_queries:,} queries"
            scan_basis = (
                "billed bytes" if getattr(slots, "has_billed_bytes", False)
                else "processed bytes — an overestimate of what BigQuery actually charges"
            )
            text = f"Priced from {window} ({volume}, {scan_basis}) and current published rates."
        else:
            text = (
                "No workload data — compute is a rough range and scan volume a stored-bytes "
                "proxy. Provide query logs for a measured estimate."
            )
        return level, text

    def _key_uncertainties(self, slots: SlotUtilization | None) -> list[str]:
        """The assumptions most likely to move the AWS figure — one bullet each, naming
        the uncertainty AND how to validate it. Single home for the slot→RPU caveat
        (previously worded twice: estimate-basis callout + Key Caveats card).

        The card must always name the DOMINANT uncertainty for its path: slot→RPU when
        slot data exists, the range estimate itself when it doesn't (2026-07-16 audit
        MRI-2 — a "Key Uncertainties" card that omits the biggest one understates by
        construction).
        """
        notes: list[str] = []
        if slots is not None and slots.total_slot_ms > 0:
            notes.append(
                f"BigQuery-slot to Redshift-RPU conversion ({k.V3_SLOT_TO_RPU_RATIO}): a "
                "research-based assumption at the midpoint of the evidence range "
                "(0.06–0.25); always-on workloads are additionally floored at Redshift "
                "Serverless's 4-RPU minimum base capacity. "
                "This is the largest uncertainty in the AWS estimate — validate with "
                "SYS_SERVERLESS_USAGE from a pilot workload before committing to a migration budget "
                "(the view retains 7 days; capture within that window or use Cost "
                "Explorer)."
            )
        else:
            notes.append(
                "No workload compute signal — the AWS compute figure is a rough range, "
                "the largest uncertainty in this estimate. Provide query logs (or run a "
                "pilot workload and measure with SYS_SERVERLESS_USAGE) before committing "
                "to a migration budget."
            )
        if slots is not None and 0 < slots.days_sampled < _DAYS_MEASURED:
            notes.append(
                f"Only {slots.days_sampled} active day(s) of workload sampled — a thin "
                "window; treat the monthly projection as indicative."
            )
        notes.append(
            "Provisioned cluster sizing uses concurrency estimated from query "
            "timestamps, not measured slot contention — confirm with the same pilot."
        )
        # Athena-specific uncertainties
        notes.append(
            "Athena estimate scales linearly with scanned bytes: projected from your BigQuery "
            "scan history; post-migration scan volumes can differ (Iceberg partition pruning may "
            "reduce them; loss of BigQuery clustering may increase them until sort-order "
            "optimization is applied)."
        )
        notes.append(
            f"The Athena-vs-Redshift crossover (~2.4–2.7 TB/day depending on region; priced "
            f"for {k.AWS_PRICING_REGION}) assumes Redshift Serverless at its 4-RPU/8-hour "
            f"minimum posture; always-on workloads break even nearer 8 TB/day."
        )
        return notes

    def _pricing_notes(self, bq_location: str, region_known: bool, override=None) -> list[str]:
        """Pricing mechanics a reader needs to reconcile the figure against a real bill:
        which geography each side was priced in, and the 30-day-month normalization.

        When the BigQuery cost is operator-supplied, the BQ rate-table bullet is
        suppressed — asserting a rate for a hand-entered number reads as contradictory
        (2026-07-16 audit polish item).
        """
        bq_side = (
            "BigQuery cost is operator-supplied (not priced from a rate table)"
            if override is not None else
            f"BigQuery priced for {v4.V4_PRICING_REGION} "
            f"(${v4.V4_ONDEMAND_USD_PER_TIB}/TiB scan, "
            f"${v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH}/GiB-mo active storage)"
        )
        notes = [
            f"{bq_side}; AWS priced for {k.AWS_REGION_SCOPE}.",
            ("Athena priced at $5.00/TB scanned (on-demand, region-invariant; verified 2026-07-21); "
            "DDL and failed queries are not billed; a 10 MB per-query minimum applies."),
            ("Athena one-time OPTIMIZE compaction estimated at $5/TB of stored data — an upper bound "
            "based on uncompressed BigQuery logical size (Parquet on S3 is typically several times smaller)."),
            ("Monthly figures normalize to a 30-day month; calendar months of 31 days "
            "bill ~3% higher."),
            (f"S3 Tables ongoing compaction (${k.V2_COMPACTION_USD_PER_GB_PROCESSED}/GB "
            "processed) bills against newly written data — not estimable without an "
            "ingestion rate; expect it to track your write volume."),
        ]
        tiers = getattr(self, "_int_tiers", None) or {}
        cold_bytes = sum(
            tiers.get(key, {}).get("bytes", 0) for key in ("infrequent", "archive")
        )
        if cold_bytes > 0:
            notes.append(
                "S3 Tables Intelligent-Tiering: storage is priced on the observed "
                "access pattern — cold data bills at the discounted tiers (Infrequent "
                f"Access after {k.V2_INT_INFREQUENT_THRESHOLD_DAYS} days without access, "
                f"Archive Instant Access after {k.V2_INT_ARCHIVE_THRESHOLD_DAYS}). All "
                "tiers serve reads at millisecond latency with no retrieval fee; an "
                "accessed file returns to Frequent automatically. Caveats: month 1 bills "
                "all-Frequent (the storage line's upper bound) and declines as files "
                "tier; the split is estimated from last-modified time, not true access "
                "history — tables that are read regularly stay Frequent; periodic "
                "full-table scans re-heat cold files."
            )
        if not region_known and bq_location != "us":
            notes.insert(0, (
                f"⚠️ No verified rate table for BigQuery location '{bq_location}' — priced "
                f"at US multi-region rates, which likely UNDERstates the true cost."
            ))
        return notes

    def _scope_notes(self) -> list[str]:
        """What the estimate omits, on BOTH sides of the comparison — rendered verbatim.

        A customer reconciling the estimate against the GCP billing console must know that
        only analysis (scan) + storage SKUs are modeled; ingestion/egress SKUs
        (streaming inserts, Storage Read/Write API, BI Engine, Data Transfer Service)
        appear on the same BigQuery bill but are NOT in this figure. The AWS-side
        exclusions sit next to them so out-of-scope is one list, not two.
        """
        egress_gib = getattr(self, "_egress_gib", None)
        if egress_gib is not None and egress_gib > 0:
            bq_scope = (
                "BigQuery side: covers on-demand analysis (bytes billed), active logical "
                "storage, and Storage Read API egress (from Cloud Monitoring). Not modeled: "
                "streaming inserts, Storage Write API, BI Engine, Data Transfer Service — "
                "these appear on the BigQuery bill and can be significant for ingestion-heavy "
                "projects. The on-demand free tier (1 TiB scan + 10 GiB storage/month) and "
                "negotiated discounts are also excluded."
            )
        else:
            bq_scope = (
                "BigQuery side: covers on-demand analysis (bytes billed) and active logical "
                "storage only. Not modeled: streaming inserts, Storage Read/Write API egress "
                "(roles/monitoring.viewer unavailable or no sessions detected), "
                "BI Engine, Data Transfer Service — these appear on the BigQuery bill and can "
                "be significant for ingestion- or extract-heavy projects. The on-demand free "
                "tier (1 TiB scan + 10 GiB storage/month) and negotiated discounts are also "
                "excluded."
            )
        return [
            bq_scope,
            ("AWS side: one-time migration data transfer is not included; Redshift "
            "Spectrum, ML features, and cross-region replication are not modeled. "
            "Glue Data Catalog request charges and S3 request charges are not modeled for either Query Engine."),
        ]

    # ================================================================== AWS Scenarios

    def _evaluate_all_scenarios(
        self, total_bytes: int, total_physical_bytes: int, storage_basis: str,
        slots: SlotUtilization | None, profile: WorkloadProfile
    ) -> list[AWSScenario]:
        """Evaluate Serverless (OD + reservations) + Provisioned RG options."""
        scenarios = []

        # Scenario 1: Serverless On-Demand (always evaluated)
        scenarios.append(self._serverless_scenario(
            total_bytes, total_physical_bytes, storage_basis, slots
        ))

        # Scenarios 2-4: Serverless Reservations (1yr All Upfront, 1yr No Upfront, 3yr)
        if slots is not None and slots.total_slot_ms > 0:
            assert scenarios[0].category == "SERVERLESS"
            serverless_od_total = scenarios[0].monthly_total
            for term in ("1yr_all_upfront", "1yr_no_upfront", "3yr"):
                reservation = self._serverless_reservation_scenario(
                    total_bytes, total_physical_bytes, storage_basis, slots, profile, term
                )
                self._apply_dominance_demotion(reservation, serverless_od_total)
                scenarios.append(reservation)

        # Scenarios 4-6: Provisioned RG (only when we have workload data to size a cluster)
        if slots is not None and slots.total_slot_ms > 0:
            node_type, node_count = self._size_cluster(profile)
            for category, rate_key, label_suffix in [
                ("PROVISIONED_ONDEMAND", "ondemand_usd_per_node_hour", "On-Demand"),
                ("PROVISIONED_1YR", "ri_1yr_usd_per_node_hour", "1yr Reserved"),
                ("PROVISIONED_3YR", "ri_3yr_usd_per_node_hour", "3yr Reserved"),
            ]:
                prov = self._provisioned_scenario(
                    total_bytes, total_physical_bytes, storage_basis,
                    profile, node_type, node_count, category, rate_key, label_suffix,
                )
                self._apply_dominance_demotion(prov, serverless_od_total)
                scenarios.append(prov)

        # Append AWS Data Transfer Out line to all scenarios if egress was estimated
        egress_gib = getattr(self, "_egress_gib", None)
        if egress_gib is not None and egress_gib > 0:
            egress_gb = egress_gib * (1024 ** 3) / (1000 ** 3)  # GiB → GB (AWS bills decimal)
            dt_out_usd = round(egress_gb * k.V8_DATA_TRANSFER_OUT_USD_PER_GB, 4)
            dt_source_note = (
                f"{egress_gb:,.1f} GB/mo × ${k.V8_DATA_TRANSFER_OUT_USD_PER_GB}/GB "
                f"({k.AWS_PRICING_REGION}) — mirrors BQ Storage Read API egress "
                f"(assumes consumers access data over internet; within-VPC = $0)"
            )
            for scenario in scenarios:
                dt_line = CostLine(
                    label="Data Transfer Out (internet)",
                    monthly=dt_out_usd,
                    monthly_low=None, monthly_high=None,
                    confidence=ConfidenceLevel.MEDIUM,
                    source_note=dt_source_note,
                )
                scenario.lines.append(dt_line)
                scenario.monthly_total = round(
                    scenario.monthly_total + dt_out_usd, 4
                )

        return scenarios

    def _serverless_scenario(
        self, total_bytes: int, total_physical_bytes: int, storage_basis: str,
        slots: SlotUtilization | None,
        is_recommended: bool = False,
        engine_recommendation = None,
    ) -> AWSScenario:
        """Redshift Serverless scenario."""
        storage_line = self._aws_s3_storage_line(total_physical_bytes, storage_basis)
        compute_line, compute_conf = self._serverless_compute_line(slots)
        lines = [storage_line, compute_line]
        total = round(_line_value(storage_line) + _line_value(compute_line), 4)

        if slots is not None and slots.total_slot_ms > 0:
            qpd = slots.total_queries / _window_days(slots)
            if is_recommended and engine_recommendation:
                justification = self._redshift_elaborated_justification(
                    slots, engine_recommendation, "serverless"
                )
            else:
                justification = (
                    f"Serverless at ${k.V1_RPU_HOUR_USD}/RPU-hr ({k.AWS_PRICING_REGION}) with "
                    f"{k.V3_SLOT_TO_RPU_RATIO} slot-to-RPU ratio. "
                    f"Your workload runs {qpd:,.0f} queries/day — pay-per-second only when queries "
                    f"are active, scaling to zero during idle. Consider Serverless Reservations "
                    f"(20–45% discount, 1yr or 3yr terms) if utilization is sustained."
                )
        else:
            justification = (
                "Serverless estimated with a conservative range (no workload data). "
                "Suitable for unpredictable or low-volume workloads."
            )

        return AWSScenario(
            label="Redshift Serverless",
            category="SERVERLESS",
            lines=lines,
            monthly_total=total,
            confidence=compute_conf,
            justification=justification,
            workload_fit_notes=_serverless_fit_notes(slots),
        )

    def _serverless_reservation_scenario(
        self, total_bytes: int, total_physical_bytes: int, storage_basis: str,
        slots: SlotUtilization, profile: WorkloadProfile, term: str
    ) -> AWSScenario:
        """Serverless Reservation (1yr or 3yr)."""
        if term == "1yr_all_upfront":
            rpu_rate = k.V1_SERVERLESS_1YR_ALL_UPFRONT_RPU_HOUR_USD
            discount_pct = k.V1_SERVERLESS_RESERVATION_1YR_ALL_UPFRONT_DISCOUNT
            category = "SERVERLESS_1YR"
            label = "Serverless Reserved (1yr, All Upfront)"
            confirmed = k.AWS_SERVERLESS_RESERVATIONS_CONFIRMED_DATE
        elif term == "1yr_no_upfront":
            rpu_rate = k.V1_SERVERLESS_1YR_NO_UPFRONT_RPU_HOUR_USD
            discount_pct = k.V1_SERVERLESS_RESERVATION_1YR_NO_UPFRONT_DISCOUNT
            category = "SERVERLESS_1YR_NO_UPFRONT"
            label = "Serverless Reserved (1yr, No Upfront)"
            confirmed = k.AWS_SERVERLESS_RESERVATIONS_CONFIRMED_DATE
        else:
            rpu_rate = k.V1_SERVERLESS_3YR_RPU_HOUR_USD
            discount_pct = k.V1_SERVERLESS_RESERVATION_3YR_DISCOUNT
            category = "SERVERLESS_3YR"
            label = "Serverless Reserved (3yr)"
            confirmed = k.AWS_SERVERLESS_RESERVATIONS_CONFIRMED_DATE

        # Reservations bill 24/7 for committed RPUs. Size the commitment from the workload:
        # use avg_slots × V3 ratio as the base RPU to commit (rounded up to nearest 8).
        avg_rpus = slots.avg_slots * k.V3_SLOT_TO_RPU_RATIO
        committed_rpus = max(8, math.ceil(avg_rpus / 8) * 8)
        # Regional RPU cap guardrail (verified 2026-07-15)
        max_rpus = k.SERVERLESS_MAX_RPU_BY_REGION.get(
            k.AWS_PRICING_REGION, k.SERVERLESS_DEFAULT_MAX_RPU
        )
        rpu_capped = committed_rpus > max_rpus
        if rpu_capped:
            committed_rpus = max_rpus

        # 24/7 cost for the committed RPUs
        compute_monthly = committed_rpus * rpu_rate * k.HOURS_PER_MONTH

        # Overflow: usage above committed RPUs billed at on-demand rate.
        # If avg exceeds the cap, overflow is persistent (full active hours);
        # otherwise peak-only overflow is transient (burst fraction of active hours).
        peak_rpus = slots.peak_slots * k.V3_SLOT_TO_RPU_RATIO
        overflow_rpus = max(0, peak_rpus - committed_rpus)
        active_hours = slots.active_hour_fraction * k.HOURS_PER_MONTH
        burst_factor = 1.0 if avg_rpus > committed_rpus else k.V1_OVERFLOW_BURST_FRACTION
        overflow_monthly = (
            overflow_rpus * k.V1_RPU_HOUR_USD * active_hours * burst_factor
        )

        storage_line = self._aws_s3_storage_line(total_physical_bytes, storage_basis)
        compute_line = CostLine(
            label=f"Serverless compute ({label})",
            monthly=round(compute_monthly + overflow_monthly, 4),
            monthly_low=None, monthly_high=None,
            confidence=ConfidenceLevel.MEDIUM,
            source_note=(
                f"{committed_rpus} RPUs committed @ ${rpu_rate}/RPU-hr 24/7 "
                f"({discount_pct:.0%} off on-demand). Billed whether active or idle "
                f"(verified {confirmed})"
            ),
        )
        lines = [storage_line, compute_line]
        total = round(_line_value(storage_line) + _line_value(compute_line), 4)

        active_frac = profile.active_hour_fraction or 0.5
        breakeven_util = 1 - discount_pct  # reserved_rate / ondemand_rate
        justification = (
            f"Serverless Reservation commits {committed_rpus} RPUs for {label} at "
            f"${rpu_rate}/RPU-hr ({discount_pct:.0%} discount). Unlike on-demand, reservations "
            f"bill 24/7 — cost-effective when utilization exceeds ~{breakeven_util:.0%} of hours. "
            f"Your workload is active {active_frac:.0%} of hours"
        )
        # Below break-even the 24/7 committed floor dwarfs the recommended option (34-95×
        # on sparse workloads) and "Reserved" reads as "should be cheaper" — demote the
        # scenario visibly so the big number isn't mistaken for a tool error (MRI-8).
        not_recommended_reason = ""
        if active_frac > breakeven_util:
            justification += " — the reservation pays for itself."
        else:
            justification += (
                " — below the break-even threshold; on-demand or provisioned may be cheaper."
            )
            not_recommended_reason = (
                f"Not recommended for your usage pattern: active {active_frac:.0%} of hours, "
                f"below the ~{breakeven_util:.0%} utilization where a 24/7 commitment breaks even"
            )

        fit_notes = [
            f"Committed {committed_rpus} RPUs (sized from avg {avg_rpus:.1f} RPU workload)",
            f"Reservation bills 24/7 — break-even at ~{breakeven_util:.0%} utilization",
            f"Your active fraction: {active_frac:.0%}",
        ]
        if rpu_capped:
            fit_notes.append(
                f"⚠️ Capped at regional maximum ({max_rpus} RPUs in {k.AWS_PRICING_REGION})"
            )

        return AWSScenario(
            label=label,
            category=category,
            lines=lines,
            monthly_total=total,
            confidence=ConfidenceLevel.MEDIUM,
            justification=justification,
            workload_fit_notes=fit_notes,
            not_recommended_reason=not_recommended_reason,
        )

    def _provisioned_scenario(
        self, total_bytes: int, total_physical_bytes: int, storage_basis: str,
        profile: WorkloadProfile, node_type: str, node_count: int,
        category: str, rate_key: str, label_suffix: str,
    ) -> AWSScenario:
        """Redshift Provisioned scenario at a specific commitment level (RG Graviton)."""
        node_spec = k.V7_RG_NODE_TYPES[node_type]
        rate = node_spec[rate_key]
        config_label = f"{node_count}× {node_type}"

        # Compute cost: nodes × rate × 730 hours/month (always-on)
        compute_monthly = node_count * rate * k.HOURS_PER_MONTH

        # Concurrency scaling overhead (customer-specific based on burst ratio)
        cs_fraction = self._concurrency_scaling_fraction(profile)
        cs_overhead = compute_monthly * cs_fraction
        compute_with_cs = compute_monthly + cs_overhead

        # Storage: data lives in S3 Tables (Iceberg) and is queried via external tables —
        # both serverless and provisioned share the SAME decoupled-lakehouse storage basis.
        # Provisioned does NOT load into Redshift Managed Storage (there is no native-DDL
        # path), so billing RMS would price the wrong storage product for what the migration
        # actually produces. Bill S3 Tables to match the real mapping.
        storage_line = self._aws_s3_storage_line(total_physical_bytes, storage_basis)
        storage_monthly = _line_value(storage_line)
        compute_line = CostLine(
            label=f"Compute ({config_label}, {label_suffix})",
            monthly=round(compute_with_cs, 4), monthly_low=None, monthly_high=None,
            confidence=ConfidenceLevel.HIGH,
            source_note=(
                f"{config_label} @ ${rate}/node-hr × {k.HOURS_PER_MONTH}h "
                f"+ {cs_fraction:.0%} concurrency scaling "
                f"(verified {k.AWS_PROVISIONED_CONFIRMED_DATE})"
            ),
        )

        total = round(storage_monthly + compute_with_cs, 4)
        justification = self._provisioned_justification(
            profile, node_type, node_count, rate_key, label_suffix, total
        )

        return AWSScenario(
            label=f"Redshift Provisioned {config_label} ({label_suffix})",
            category=category,
            lines=[storage_line, compute_line],
            monthly_total=total,
            confidence=ConfidenceLevel.HIGH,
            justification=justification,
            cluster_config=config_label,
            workload_fit_notes=self._provisioned_fit_notes(profile, node_type, node_count),
        )

    # ================================================================== Dominance Check

    _DEMOTION_SUFFIX = (
        " — but the committed floor exceeds the on-demand estimate, "
        "so on-demand is cheaper for your workload."
    )
    _PAYS_FOR_ITSELF = " — the reservation pays for itself."
    _BELOW_BREAKEVEN = " — below the break-even threshold; on-demand or provisioned may be cheaper."

    @staticmethod
    def _apply_dominance_demotion(scenario: AWSScenario, baseline_total: float) -> None:
        """Demote a scenario that costs more than the on-demand baseline (MRI-8/MRI-9)."""
        if scenario.not_recommended_reason:
            return
        if scenario.monthly_total > baseline_total:
            ratio = scenario.monthly_total / max(baseline_total, 0.01)
            if ratio > 999:
                ratio_text = "significantly more than"
            else:
                ratio_text = f"~{ratio:,.0f}× "
            scenario.not_recommended_reason = (
                f"Not recommended for your usage pattern: costs "
                f"{ratio_text}the on-demand Serverless estimate"
            )
            # Strip known contradicting conclusions from serverless reservation justifications
            # before appending the demotion. Provisioned justifications have no such clause.
            j = scenario.justification
            for suffix in (CostEstimator._PAYS_FOR_ITSELF,
                           CostEstimator._BELOW_BREAKEVEN):
                if j.endswith(suffix):
                    j = j[: -len(suffix)]
                    break
            scenario.justification = j + CostEstimator._DEMOTION_SUFFIX

    # ================================================================== Cluster Sizing

    def _build_workload_profile(self, slots: SlotUtilization | None, total_bytes: int) -> WorkloadProfile:
        """Extract customer-specific workload metrics for sizing and justification."""
        if slots is None or slots.total_slot_ms == 0:
            return WorkloadProfile(has_data=False, total_stored_gb=total_bytes * k.GB_PER_BYTE)

        days = max(slots.days_sampled, 1)
        # Use the shared calendar window for QPD (not just slot-bearing days) to avoid
        # inflation — same _window_days the cost line projects over.
        lookback_days = _window_days(slots)
        queries_per_day = slots.total_queries / lookback_days
        avg_query_duration_est = k.V6_AVG_QUERY_DURATION_SECONDS
        queries_per_second_avg = queries_per_day / 86_400
        avg_concurrent = queries_per_second_avg * avg_query_duration_est
        peak_concurrent = avg_concurrent * k.V6_PEAK_TO_AVG_CONCURRENCY_RATIO

        # Same scan-volume basis and calendar window as the BigQuery cost line
        # (_bq_ondemand) — the recommendation prose must quote the volume the customer
        # is actually billed on, not a different one.
        basis_bytes, _ = _scan_basis(slots)
        bytes_per_query = (
            basis_bytes / slots.total_queries if slots.total_queries > 0 else 0
        )
        monthly_scanned_tb = (
            (basis_bytes / lookback_days * k.DAYS_PER_MONTH) / (1024 ** 4)
        )

        return WorkloadProfile(
            has_data=True,
            total_stored_gb=total_bytes * k.GB_PER_BYTE,
            total_queries=slots.total_queries,
            days_sampled=days,
            lookback_days=lookback_days,
            queries_per_day=queries_per_day,
            queries_per_second_avg=queries_per_second_avg,
            avg_concurrent_queries=avg_concurrent,
            peak_concurrent_queries=peak_concurrent,
            avg_bytes_per_query=bytes_per_query,
            monthly_scanned_tb=monthly_scanned_tb,
            active_hour_fraction=slots.active_hour_fraction,
            total_slot_ms=slots.total_slot_ms,
            avg_slots=slots.avg_slots,
            p99_slots=slots.p99_slots,
            peak_slots=slots.peak_slots,
        )

    def _size_cluster(self, profile: WorkloadProfile) -> tuple[str, int]:
        """Determine the best-fit RG node type and count from workload metrics.

        Only rg.xlarge and rg.4xlarge exist (verified 2026-07-15). High-volume
        workloads (>500K QPD) scale via multi-node rg.4xlarge clusters.
        """
        qpd = profile.queries_per_day
        peak_concurrent = profile.peak_concurrent_queries or 4

        if qpd <= k.V6_QUERIES_PER_DAY_XLPLUS_MAX:
            node_type = "rg.xlarge"
        else:
            node_type = "rg.4xlarge"

        spec = k.V7_RG_NODE_TYPES[node_type]
        vcpu_per_node = spec["vcpu"]

        # Size by concurrency: enough vCPUs to handle peak concurrent queries
        vcpu_needed = peak_concurrent * k.V6_VCPU_PER_CONCURRENT_QUERY
        nodes_by_concurrency = math.ceil(vcpu_needed / vcpu_per_node)

        # Minimum 2 nodes (requirement), max from spec
        node_count = max(spec["min_nodes"], min(nodes_by_concurrency, spec["max_nodes"]))

        return node_type, node_count

    def _concurrency_scaling_fraction(self, profile: WorkloadProfile) -> float:
        """Estimate concurrency scaling overhead from workload burstiness.

        Uses actual peak_slots/avg_slots ratio from observed workload data rather than
        the synthetic peak_concurrent_queries (which is derived from a fixed 3× multiplier).
        """
        if not profile.has_data:
            return k.V6_CONCURRENCY_SCALING_OVERHEAD_FRACTION

        active_fraction = profile.active_hour_fraction or 0.5
        avg_slots = profile.avg_slots or 1
        peak_slots = profile.peak_slots or avg_slots
        peak_to_avg = peak_slots / max(avg_slots, 0.1)

        # Bursty workloads (high peak:avg, low active hours) need more CS
        if peak_to_avg > 5 and active_fraction < 0.3:
            return 0.35
        if peak_to_avg > 3:
            return 0.25
        if active_fraction > 0.6:
            return 0.10  # steady workload, less burst
        return 0.15

    # ================================================================== Recommendation

    def _generate_recommendation(
        self, scenarios: list[AWSScenario], profile: WorkloadProfile, bq_monthly: float
    ) -> AWSRecommendation:
        """Select the best scenario and write customer-specific justification."""
        if not profile.has_data:
            return AWSRecommendation(
                recommended_scenario=scenarios[0].label,
                reasoning=(
                    "No workload data available to size a provisioned cluster. "
                    "Redshift Serverless is recommended as the starting point — it requires "
                    "no capacity planning and scales automatically. Once the workload is "
                    "running on AWS, monitor SYS_SERVERLESS_USAGE to determine if a "
                    "provisioned cluster would be more cost-effective."
                ),
                workload_profile=profile,
                alternatives_considered=[s.label for s in scenarios],
            )

        qpd = profile.queries_per_day
        monthly_tb = profile.monthly_scanned_tb
        active_frac = profile.active_hour_fraction
        peak_conc = profile.peak_concurrent_queries

        # Decision logic: serverless (OD + reserved) vs provisioned
        # Serverless wins for: low/sporadic volume, unpredictable burst, <10k queries/day
        # Provisioned wins for: sustained high volume, predictable patterns, >50k queries/day
        serverless_od = next(s for s in scenarios if s.category == "SERVERLESS")
        serverless_reserved = [s for s in scenarios if s.category.startswith("SERVERLESS_") and s.category != "SERVERLESS"]
        provisioned_options = [s for s in scenarios if s.category.startswith("PROVISIONED")]

        # Best serverless option (OD or reserved)
        all_serverless = [serverless_od] + serverless_reserved
        serverless = min(all_serverless, key=lambda s: s.monthly_total)

        if not provisioned_options:
            return AWSRecommendation(
                recommended_scenario=serverless.label,
                reasoning="Serverless is the only evaluated option (insufficient data for provisioned sizing).",
                workload_profile=profile,
                alternatives_considered=[s.label for s in scenarios],
            )

        # Find cheapest provisioned option
        cheapest_prov = min(provisioned_options, key=lambda s: s.monthly_total)
        cheapest_prov_ri = next(
            (s for s in provisioned_options if s.category == "PROVISIONED_1YR"), cheapest_prov
        )

        # Decision factors
        is_high_volume = qpd > 10_000
        is_steady = active_frac > 0.3
        provisioned_saves = cheapest_prov_ri.monthly_total < serverless.monthly_total * 0.85

        # First check: if serverless (best of OD/reserved) beats all provisioned, recommend it
        serverless_wins = serverless.monthly_total < cheapest_prov_ri.monthly_total

        if is_high_volume and is_steady and provisioned_saves:
            # Provisioned is clearly better than serverless on-demand
            # Pick the best-value committed option (provisioned 1yr RI or serverless reserved)
            recommended = cheapest_prov_ri
            cheapest_3yr = next(
                (s for s in provisioned_options if s.category == "PROVISIONED_3YR"), None
            )
            reasoning = (
                f"Your workload runs {qpd:,.0f} queries/day ({profile.total_queries:,} total "
                f"over {profile.lookback_days} days) scanning {monthly_tb:,.0f} TB/month. "
                f"This is a sustained, high-volume pattern (active {active_frac:.0%} of hours) "
                f"with ~{peak_conc:.0f} peak concurrent queries. "
                f"Provisioned RG (Graviton4) with a 1-year RI saves "
                f"{_fmt_usd(serverless.monthly_total - recommended.monthly_total)}/month vs Serverless On-Demand "
                f"({_fmt_usd((serverless.monthly_total - recommended.monthly_total) * 12)}/year). "
                f"The steady query volume makes the commitment predictable and low-risk."
            )
            # Cost-parity sentence: bq_monthly=0.0 (unavailable) harmlessly makes this False.
            if cheapest_3yr and cheapest_3yr.monthly_total < bq_monthly * 1.1:
                reasoning += (
                    f" A 3-year RI at {_fmt_usd(cheapest_3yr.monthly_total)}/month achieves near "
                    f"cost-parity with your current BigQuery spend ({_fmt_usd(bq_monthly)}/month) "
                    f"— consider this if the workload will remain on AWS long-term."
                )
            elif cheapest_3yr:
                reasoning += (
                    f" A 3-year RI would save an additional "
                    f"{_fmt_usd(recommended.monthly_total - cheapest_3yr.monthly_total)}/month "
                    f"if the workload will remain on AWS long-term."
                )
        elif is_high_volume and provisioned_saves:
            recommended = cheapest_prov_ri
            reasoning = (
                f"Your workload runs {qpd:,.0f} queries/day scanning {monthly_tb:,.0f} TB/month. "
                f"Despite bursty patterns (active only {active_frac:.0%} of hours), the volume "
                f"is high enough that provisioned RG with concurrency scaling still beats serverless "
                f"by {_fmt_usd(serverless.monthly_total - recommended.monthly_total)}/month. "
                f"Consider starting with On-Demand provisioned to validate sizing, then "
                f"converting to a 1-year RI once the pattern is confirmed."
            )
        elif serverless_wins or qpd < 5_000 or (not is_steady and serverless.monthly_total < cheapest_prov_ri.monthly_total):
            recommended = serverless
            reasoning = (
                f"Your workload runs {qpd:,.0f} queries/day (active {active_frac:.0%} of hours). "
            )
            if serverless_wins:
                reasoning += (
                    f"Serverless at {_fmt_usd(serverless.monthly_total)}/month beats Provisioned 1yr RI "
                    f"({_fmt_usd(cheapest_prov_ri.monthly_total)}/month) due to the pay-per-second "
                    f"model and efficient RPU auto-scaling. "
                )
            else:
                reasoning += (
                    f"This is a {'sporadic' if active_frac < 0.2 else 'moderate-volume'} pattern "
                    f"where Serverless pay-per-use is more efficient than maintaining an always-on "
                    f"provisioned cluster. "
                )
            reasoning += (
                "Serverless auto-scales to zero during idle periods and "
                "handles burst without pre-provisioning."
            )
        else:
            # Marginal case — recommend provisioned on-demand as stepping stone
            prov_od = next(
                (s for s in provisioned_options if s.category == "PROVISIONED_ONDEMAND"),
                cheapest_prov_ri
            )
            recommended = prov_od
            reasoning = (
                f"Your workload ({qpd:,.0f} queries/day, {monthly_tb:,.0f} TB/month scanned, "
                f"active {active_frac:.0%} of hours) sits between clear serverless and "
                f"committed-provisioned territory. Recommend starting with Provisioned RG On-Demand "
                f"to validate cluster sizing without commitment. If costs are stable after 1-2 "
                f"months, convert to a 1-year RI to save "
                f"~{_fmt_usd(prov_od.monthly_total - cheapest_prov_ri.monthly_total)}/month."
            )

        return AWSRecommendation(
            recommended_scenario=recommended.label,
            reasoning=reasoning,
            workload_profile=profile,
            alternatives_considered=[s.label for s in scenarios if s.label != recommended.label],
        )

    # ================================================================== Compute Lines

    def _serverless_compute_line(
        self, slots: SlotUtilization | None
    ) -> tuple[CostLine, ConfidenceLevel]:
        """Serverless RPU compute (V1) via the slot→RPU bridge (V3)."""
        if slots is not None and slots.total_slot_ms > 0:
            rpu_hours = _rpu_hours_per_month(slots)
            # Billing floor: while processing, Serverless bills at least the minimum
            # base capacity (4 RPU). Slot-derived RPU-hours below that floor over the
            # workload's active hours understate always-on workloads (~97%-active
            # estates came in ~10% under). Charge whichever is larger.
            floor_hours = (
                k.SERVERLESS_MIN_RPU_FLOOR * slots.active_hour_fraction * k.HOURS_PER_MONTH
            )
            floored = rpu_hours < floor_hours
            rpu_hours = max(rpu_hours, floor_hours)
            usd = rpu_hours * k.V1_RPU_HOUR_USD
            # Capped at MEDIUM by the tool's own rubric ("priced using research-based
            # conversion ratios"): the dollar figure rides on the V3 slot→RPU ratio, an
            # unverifiable-before-migration assumption that dominates any sample-window
            # error. A HIGH badge above a note calling itself an assumption was the #1
            # confidence-confusion finding (2026-07-15). HIGH is unreachable here until
            # the ratio is empirically measured (i.e. post-migration).
            conf = ConfidenceLevel.MEDIUM
            line = CostLine(
                label="Redshift Serverless compute",
                monthly=round(usd, 4), monthly_low=None, monthly_high=None,
                confidence=conf,
                # Rate provenance and sizing assumption stated separately — the old
                # single-breath "ratio (assumption ...) (verified ...)" read as a
                # contradiction ("assumption or verified?").
                source_note=(
                    f"${k.V1_RPU_HOUR_USD}/RPU-hr (rate verified {k.AWS_CONFIRMED_DATE}); "
                    f"sized via slot-to-RPU ratio {k.V3_SLOT_TO_RPU_RATIO} — an assumption, "
                    f"validate with a pilot workload"
                    + (
                        f"; floored at the {k.SERVERLESS_MIN_RPU_FLOOR}-RPU minimum base "
                        f"capacity over {slots.active_hour_fraction:.0%} active hours"
                        if floored else ""
                    )
                ),
            )
            return line, conf

        hours = k.RANGE_ACTIVE_HOURS_PER_MONTH
        low = k.SERVERLESS_MIN_RPU_FLOOR * hours * k.V1_RPU_HOUR_USD
        high = k.SERVERLESS_DEFAULT_BASE_RPU * hours * k.V1_RPU_HOUR_USD
        line = CostLine(
            label="Redshift Serverless compute",
            monthly=None, monthly_low=round(low, 4), monthly_high=round(high, 4),
            confidence=ConfidenceLevel.LOW,
            source_note=(
                f"Redshift Serverless @ ${k.V1_RPU_HOUR_USD}/RPU-hr; {k.SERVERLESS_MIN_RPU_FLOOR}–"
                f"{k.SERVERLESS_DEFAULT_BASE_RPU} RPU range, LOW-confidence estimate — no query "
                f"logs/slots; provide query logs to refine (verified {k.AWS_CONFIRMED_DATE})"
            ),
        )
        return line, ConfidenceLevel.LOW

    def _aws_s3_storage_line(self, total_physical_bytes: int, basis: str = "measured") -> CostLine:
        """S3 Tables tiered storage (V2) — sized on physical (compressed) bytes.

        Includes the recurring object-monitoring charge S3 Tables adds over plain
        S3 (R18.1's "maintenance lines" — previously defined but never billed).
        Object count is estimated from physical bytes at the assumed post-compaction
        object size; compaction itself (per GB newly written) needs an ingestion
        rate we don't collect, so it stays disclosure-only in pricing_notes.

        Intelligent-Tiering (V2-INT, 2026-07-31): when the per-entity split
        (estimate() → self._int_tiers) has cold bytes, the line becomes a range —
        high = all data in Frequent Access (month 1; Frequent bills at the Standard
        tier rates), low = steady state with Infrequent/Archive Instant bytes at
        their flat rates. No cold bytes ⇒ the point line, unchanged.
        """
        gb = total_physical_bytes * k.GB_PER_BYTE
        est_objects = (total_physical_bytes / (k.V2_ASSUMED_OBJECT_SIZE_MB * 1e6)) if total_physical_bytes else 0.0
        monitoring_usd = est_objects / 1000.0 * k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH
        all_frequent_usd = _tiered_s3_tables_usd(gb) + monitoring_usd

        if basis == "measured":
            confidence = ConfidenceLevel.HIGH
            basis_phrase = "physical (from TABLE_STORAGE)"
        elif basis == "mixed":
            confidence = ConfidenceLevel.MEDIUM
            basis_phrase = f"physical (mixed: TABLE_STORAGE + {k.ASSUMED_PHYSICAL_RATIO}× logical fallback)"
        else:  # assumed
            confidence = ConfidenceLevel.MEDIUM
            basis_phrase = f"({k.ASSUMED_PHYSICAL_RATIO}× logical — TABLE_STORAGE unavailable)"

        note = (
            f"{fmt_size_exact(gb)} {basis_phrase} × tiered from "
            f"${k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER1}/GB-mo "
            f"+ ${monitoring_usd:,.0f} object monitoring "
            f"(~{est_objects:,.0f} objects @ {k.V2_ASSUMED_OBJECT_SIZE_MB:.0f} MB, "
            f"${k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH}/1k-mo) "
            f"{k.AWS_REGION_SCOPE} (verified {k.AWS_CONFIRMED_DATE})"
        )

        tiers = getattr(self, "_int_tiers", None) or {}
        cold_bytes = sum(
            tiers.get(key, {}).get("bytes", 0) for key in ("infrequent", "archive")
        )
        if cold_bytes > 0:
            # Steady state: Frequent bytes at the volume-tiered Standard rates (the
            # marginal-tier ladder applied to just the hot GB — Frequent fills the
            # cheapest tiers first, which slightly overstates steady state: the safe
            # direction); IA/AIA bytes at their flat rates; monitoring unchanged
            # (per-object, not tiered).
            freq_gb = tiers["frequent"]["bytes"] * k.GB_PER_BYTE
            ia_gb = tiers["infrequent"]["bytes"] * k.GB_PER_BYTE
            arc_gb = tiers["archive"]["bytes"] * k.GB_PER_BYTE
            steady_state_usd = (
                _tiered_s3_tables_usd(freq_gb)
                + ia_gb * k.V2_INT_IA_USD_PER_GB_MONTH
                + arc_gb * k.V2_INT_AIA_USD_PER_GB_MONTH
                + monitoring_usd
            )
        else:
            steady_state_usd = all_frequent_usd

        # Materiality guard: a range only when tiering moves the rounded figure.
        # Estates with a few cold KB would otherwise render "X – X" (the
        # 2026-07-31 test-env re-render caught exactly this).
        if round(steady_state_usd, 4) >= round(all_frequent_usd, 4):
            return CostLine(
                label="S3 Tables storage",
                monthly=round(all_frequent_usd, 4), monthly_low=None, monthly_high=None,
                confidence=confidence,
                source_note=note,
            )

        # Lead with the verdict, in the customer's numbers (2026-07-31 review:
        # the range explanation was buried mid-note). Plain text — source notes
        # render autoescaped, and |safe would open an injection path for notes
        # that carry customer identifiers.
        arc_tb = tiers["archive"]["bytes"] / 1e12
        ia_tb = tiers["infrequent"]["bytes"] / 1e12
        freq_tb = tiers["frequent"]["bytes"] / 1e12
        int_note = (
            f"${steady_state_usd:,.0f}/mo is your cost on today's storage "
            f"access pattern — {arc_tb:,.1f} TB unmodified "
            f"{k.V2_INT_ARCHIVE_THRESHOLD_DAYS}+ days bills at the Archive Instant "
            f"rate, {ia_tb:,.1f} TB ({k.V2_INT_INFREQUENT_THRESHOLD_DAYS}–"
            f"{k.V2_INT_ARCHIVE_THRESHOLD_DAYS - 1} days) at Infrequent, "
            f"{freq_tb:,.1f} TB at Frequent (derivation table below). "
            f"${all_frequent_usd:,.0f}/mo is month 1 only: all data "
            f"lands in the Frequent tier at migration and declines to "
            f"${steady_state_usd:,.0f} as files tier automatically at "
            f"{k.V2_INT_INFREQUENT_THRESHOLD_DAYS}/{k.V2_INT_ARCHIVE_THRESHOLD_DAYS} "
            f"days. The cost comparison uses the pattern-based figure. "
            f"Basis: {note}. Split uses last_modified as an access proxy: tables "
            f"read within {k.V2_INT_INFREQUENT_THRESHOLD_DAYS} days stay Frequent "
            f"regardless of age"
        )
        return CostLine(
            label="S3 Tables storage",
            monthly=None,
            monthly_low=round(steady_state_usd, 4),
            monthly_high=round(all_frequent_usd, 4),
            confidence=confidence,
            source_note=int_note,
            headline=round(steady_state_usd, 4),
        )

    def _storage_tier_rows(self, total_physical_bytes: int) -> list[dict]:
        """Per-tier derivation rows for the report's storage table (V2-INT).

        Empty when the estate has no cold bytes — the storage line is a point and
        there is nothing to derive. Row shape documented on
        CostComparison.storage_tier_breakdown. Must stay in lockstep with
        _aws_s3_storage_line's steady-state arithmetic: the total row's monthly
        equals the line's monthly_low.
        """
        tiers = getattr(self, "_int_tiers", None) or {}
        cold_bytes = sum(
            tiers.get(key, {}).get("bytes", 0) for key in ("infrequent", "archive")
        )
        if cold_bytes <= 0:
            return []

        est_objects = (
            total_physical_bytes / (k.V2_ASSUMED_OBJECT_SIZE_MB * 1e6)
            if total_physical_bytes else 0.0
        )
        monitoring_usd = est_objects / 1000.0 * k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH

        freq = tiers["frequent"]
        ia = tiers["infrequent"]
        arc = tiers["archive"]
        freq_gb = freq["bytes"] * k.GB_PER_BYTE
        ia_gb = ia["bytes"] * k.GB_PER_BYTE
        arc_gb = arc["bytes"] * k.GB_PER_BYTE
        freq_usd = _tiered_s3_tables_usd(freq_gb)
        ia_usd = ia_gb * k.V2_INT_IA_USD_PER_GB_MONTH
        arc_usd = arc_gb * k.V2_INT_AIA_USD_PER_GB_MONTH
        total_usd = freq_usd + ia_usd + arc_usd + monitoring_usd
        total_tables = freq["tables"] + ia["tables"] + arc["tables"]
        total_gb = freq_gb + ia_gb + arc_gb

        std_t1 = k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER1
        ia_pct = (1 - k.V2_INT_IA_USD_PER_GB_MONTH / std_t1) if std_t1 else 0
        arc_pct = (1 - k.V2_INT_AIA_USD_PER_GB_MONTH / std_t1) if std_t1 else 0

        return [
            {
                "tier": "frequent",
                "label": f"Frequent Access (modified < {k.V2_INT_INFREQUENT_THRESHOLD_DAYS} days)",
                "tables": freq["tables"], "gb": round(freq_gb, 3),
                "rate": std_t1, "rate_note": "",
                "monthly": round(freq_usd, 4),
            },
            {
                "tier": "infrequent",
                "label": (
                    f"Infrequent Access ({k.V2_INT_INFREQUENT_THRESHOLD_DAYS}–"
                    f"{k.V2_INT_ARCHIVE_THRESHOLD_DAYS - 1} days)"
                ),
                "tables": ia["tables"], "gb": round(ia_gb, 3),
                "rate": k.V2_INT_IA_USD_PER_GB_MONTH,
                "rate_note": f"−{ia_pct:.0%}",
                "monthly": round(ia_usd, 4),
            },
            {
                "tier": "archive",
                "label": f"Archive Instant Access ({k.V2_INT_ARCHIVE_THRESHOLD_DAYS}+ days)",
                "tables": arc["tables"], "gb": round(arc_gb, 3),
                "rate": k.V2_INT_AIA_USD_PER_GB_MONTH,
                "rate_note": f"−{arc_pct:.0%}",
                "monthly": round(arc_usd, 4),
            },
            {
                "tier": "monitoring",
                "label": f"Object monitoring (~{est_objects:,.0f} objects @ {k.V2_ASSUMED_OBJECT_SIZE_MB:.0f} MB)",
                "tables": None, "gb": None,
                "rate": k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH,
                "rate_note": "$/1k objects",
                "monthly": round(monitoring_usd, 4),
            },
            {
                "tier": "total",
                "label": "Storage total (steady state)",
                "tables": total_tables, "gb": round(total_gb, 3),
                "rate": None, "rate_note": "",
                "monthly": round(total_usd, 4),
            },
        ]

    # ================================================================== BigQuery

    def _bigquery_runrate(self, total_bytes, pricing, slots, override):
        """BigQuery monthly run-rate + breakdown + availability (R18.2 / R16.4 / 2026-08-10)."""
        if override is not None:
            line = CostLine(
                label="BigQuery (customer-provided)", monthly=round(float(override), 4),
                monthly_low=None, monthly_high=None, confidence=ConfidenceLevel.HIGH,
                source_note=(
                    "customer-supplied total monthly BigQuery bill via "
                    "--bigquery-monthly-cost (no price-list date; not decomposable "
                    "into compute vs storage)"
                ),
            )
            return float(override), [line], {"available": True, "basis": "customer_provided", "reason": ""}

        if pricing.model is BQPricingModel.CAPACITY:
            monthly, lines, unavailable_reason = self._bq_capacity(pricing, total_bytes, slots)
            if unavailable_reason is not None:
                return monthly, lines, {"available": False, "basis": "unavailable", "reason": unavailable_reason}
            return monthly, lines, {"available": True, "basis": "modelled", "reason": ""}

        monthly, lines = self._bq_ondemand(total_bytes, slots)
        return monthly, lines, {"available": True, "basis": "modelled", "reason": ""}

    def _bq_storage_line(self, total_bytes: int) -> CostLine:
        """BigQuery storage cost line — shared by on-demand and capacity paths.

        Active/long-term split from per-entity last_modified (V4: 90 idle days →
        ~50% rate). Assumes logical (uncompressed) billing, the BigQuery default;
        datasets on physical billing bill differently — measured TABLE_STORAGE
        data is needed to detect that, so it stays a scope note for now.
        """
        lt_bytes = min(getattr(self, "_longterm_bytes", 0), total_bytes)
        active_bytes = total_bytes - lt_bytes
        active_gib = active_bytes / (1024 ** 3)
        lt_gib = lt_bytes / (1024 ** 3)
        storage_usd = (
            active_gib * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
            + lt_gib * v4.V4_STORAGE_LONGTERM_LOGICAL_USD_PER_GIB_MONTH
        )
        if lt_bytes > 0:
            detail = (
                f"{active_gib:,.1f} GiB active × ${v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH}/GiB-mo "
                f"+ {lt_gib:,.1f} GiB long-term (≥{v4.V4_LONGTERM_THRESHOLD_DAYS} days unmodified) × "
                f"${v4.V4_STORAGE_LONGTERM_LOGICAL_USD_PER_GIB_MONTH}/GiB-mo"
            )
        else:
            detail = (
                f"{active_gib:,.1f} GiB stored × "
                f"${v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH}/GiB-mo"
            )
        return CostLine(
            label="BigQuery storage", monthly=round(storage_usd, 4),
            monthly_low=None, monthly_high=None, confidence=ConfidenceLevel.HIGH,
            source_note=(
                f"{detail} ({v4.V4_PRICING_REGION}) (verified {v4.V4_CONFIRMED_DATE})"
            ),
        )

    def _bq_ondemand(self, total_bytes, slots):
        storage_line = self._bq_storage_line(total_bytes)
        region = v4.V4_PRICING_REGION

        basis_bytes, basis_label = _scan_basis(slots)
        billed_zero_window = (
            slots is not None and slots.has_billed_bytes and slots.total_bytes_billed == 0
        )

        if slots is not None and slots.days_sampled > 0 and (basis_bytes > 0 or billed_zero_window):
            window_days = _window_days(slots)
            monthly_scanned_bytes = basis_bytes / window_days * k.DAYS_PER_MONTH
            scanned_tib = monthly_scanned_bytes / (1024 ** 4)
            scanned_usd = scanned_tib * v4.V4_ONDEMAND_USD_PER_TIB
            scan_conf = ConfidenceLevel.HIGH
            scan_note = (
                f"BigQuery on-demand @ ${v4.V4_ONDEMAND_USD_PER_TIB}/TiB ({region}); "
                f"{basis_bytes / (1024**4):,.2f} TiB {basis_label} across "
                f"{slots.total_queries} queries over a {window_days}-day window, "
                f"projected to monthly (verified {v4.V4_CONFIRMED_DATE})"
            )
        else:
            scanned_bytes = total_bytes * k.BQ_DAILY_SCAN_FRACTION * k.DAYS_PER_MONTH
            scanned_tib = scanned_bytes / (1024 ** 4)
            scanned_usd = scanned_tib * v4.V4_ONDEMAND_USD_PER_TIB
            scan_conf = ConfidenceLevel.LOW
            scan_note = (
                f"BigQuery on-demand @ ${v4.V4_ONDEMAND_USD_PER_TIB}/TiB ({region}); scan volume "
                f"estimated at {k.BQ_DAILY_SCAN_FRACTION:.0%}/day of stored bytes (LOW-confidence "
                f"proxy, no logs) (verified {v4.V4_CONFIRMED_DATE}); supply --bigquery-monthly-cost "
                f"for an exact figure"
            )

        lines = [
            storage_line,
            CostLine(
                label="BigQuery bytes scanned", monthly=round(scanned_usd, 4),
                monthly_low=None, monthly_high=None, confidence=scan_conf,
                source_note=scan_note,
            ),
        ]
        return round(_line_value(storage_line) + scanned_usd, 4), lines

    def _bq_capacity(self, pricing: PricingDetection, total_bytes: int,
                     slots: SlotUtilization | None = None):
        """Price a capacity Source from reservation data (baseline + autoscale).

        Returns (monthly, lines, unavailable_reason) where unavailable_reason is
        None on success or a human-readable string when the cost cannot be computed.

        When reservation details are unreadable (permission denied), returns a
        hard-stop "unavailable" state — no fabricated estimates.

        For STANDARD edition (no true commitments): always priceable from measured
        slot utilization at the single PAYG rate — reservation data is nice-to-have
        but not required.

        For ENTERPRISE/EP without reservation data: commitment type unknown →
        UNAVAILABLE (can't determine rate without knowing the plan).

        For shared reservations (assigned_count > 1), prorates by this project's
        measured slot-ms share of total reservation capacity. Falls back to equal
        headcount (1/assigned_count) if workload data is unavailable.
        """
        caveats: list[str] = []

        # Resolve edition first to determine whether reservation_readable hard-stops
        edition = pricing.edition if pricing.edition in v4.V4_EDITION_SLOT_HOUR_USD else None
        edition_known = edition is not None
        if not edition_known:
            if pricing.edition:
                caveats.append(f"unrecognized edition {pricing.edition!r}, priced at ENTERPRISE rates")
            edition = "ENTERPRISE"
        edition_rates = v4.V4_EDITION_SLOT_HOUR_USD[edition]

        # Check reservation_readable only for editions that require reservation data
        if not pricing.reservation_readable and edition in v4.V4_EDITIONS_WITH_CAPACITY_COMMITMENTS:
            # ENTERPRISE/EP without readable reservation → commitment type unknown
            return self._bq_capacity_unavailable(pricing, total_bytes)
        # STANDARD with unreadable reservation falls through to from-slots path below

        payg_rate = edition_rates["payg"]

        # Determine whether reservation data is truly absent (pre-auto-reader bundles)
        _has_reservation_data = (
            pricing.baseline_slots is not None
            or pricing.commitment_slots is not None
            or pricing.autoscale_slot_seconds is not None
        )

        if not _has_reservation_data:
            # No reservation data at all — edition determines path
            if edition not in v4.V4_EDITIONS_WITH_CAPACITY_COMMITMENTS:
                # STANDARD: single PAYG rate, no commitments — derive from slot-ms
                monthly, lines = self._bq_capacity_from_slots(
                    edition, edition_rates, slots, total_bytes, caveats
                )
                return monthly, lines, None
            else:
                # ENTERPRISE/EP: commitment type unknown → cannot price
                return self._bq_capacity_unavailable(pricing, total_bytes)

        baseline = pricing.baseline_slots
        if baseline is None:
            try:
                baseline = int(pricing.commitment_slots) if pricing.commitment_slots else 0
            except (TypeError, ValueError):
                baseline = 0
            if baseline:
                caveats.append("baseline_slots unavailable, using commitment_slots as proxy")
        else:
            try:
                baseline = int(baseline)
            except (TypeError, ValueError):
                baseline = 0

        # -- Baseline cost: blended rate across all commitments --
        # Each commitment covers its slot_count at its own plan rate; excess at PAYG.
        baseline_monthly = self._blended_baseline_cost(
            baseline, pricing, edition, edition_rates, caveats
        )

        # -- Autoscale cost: billed slot-seconds × PAYG rate / 3600 --
        autoscale_monthly = 0.0
        if pricing.autoscale_slot_seconds and pricing.timeline_window_seconds:
            window_seconds = pricing.timeline_window_seconds
            monthly_seconds = k.HOURS_PER_MONTH * 3600
            autoscale_monthly = (
                pricing.autoscale_slot_seconds / window_seconds
                * monthly_seconds * payg_rate / 3600
            )
            caveats.append(
                f"autoscale: {pricing.autoscale_slot_seconds:,.0f} slot-seconds over "
                f"{window_seconds / 86400:.1f} days → ${autoscale_monthly:,.2f}/mo @ PAYG"
            )

        total_monthly = baseline_monthly + autoscale_monthly

        # -- Proration for shared reservations (usage-based) --
        proration = 1.0
        if pricing.assigned_count > 1:
            # Compute reservation's total allocated slot-ms over the window
            window_seconds = pricing.timeline_window_seconds or int(k.HOURS_PER_MONTH * 3600)
            reservation_total_slot_ms = (
                baseline * window_seconds * 1000
                + (pricing.autoscale_slot_seconds or 0) * 1000
            )
            project_slot_ms = slots.total_slot_ms if slots else 0

            if reservation_total_slot_ms > 0 and project_slot_ms > 0:
                # consumed-slot-ms ÷ allocated-capacity shares sum to the reservation's
                # utilization ratio, not 1.0 — at low utilization most of the cost would
                # be attributed to nobody, understating BQ cost (and overstating AWS
                # savings). Floor each share at equal headcount so the idle-capacity
                # cost is still carried (verified 2026-08-10).
                usage_share = min(project_slot_ms / reservation_total_slot_ms, 1.0)
                headcount_share = 1.0 / pricing.assigned_count
                proration = max(usage_share, headcount_share)
                total_monthly *= proration
                basis = (
                    f"usage share {usage_share:.1%}"
                    if proration == usage_share
                    else f"usage share {usage_share:.1%}, floored at equal "
                    f"headcount 1/{pricing.assigned_count}"
                )
                caveats.append(
                    f"reservation shared by {pricing.assigned_count} assignees — "
                    f"prorated {proration:.1%} ({basis} of "
                    f"{reservation_total_slot_ms / _MS_PER_HOUR:,.0f} allocated slot-hours)"
                )
            else:
                proration = 1.0 / pricing.assigned_count
                total_monthly *= proration
                caveats.append(
                    f"reservation shared by {pricing.assigned_count} assignees — "
                    f"prorated 1/{pricing.assigned_count} (no usage data for share calc)"
                )

        conf = ConfidenceLevel.HIGH if baseline > 0 else ConfidenceLevel.MEDIUM
        if not edition_known:
            conf = ConfidenceLevel.LOW

        note = (
            f"BigQuery {edition} capacity: baseline {baseline} slots "
            f"= ${baseline_monthly:,.2f}/mo ({v4.V4_PRICING_REGION})"
        )
        if autoscale_monthly > 0:
            note += f" + autoscale ${autoscale_monthly:,.2f}/mo @ PAYG ${payg_rate}"
        if proration < 1.0:
            note += f" (prorated ×{proration:.3f})"
        note += f" (verified {v4.V4_CONFIRMED_DATE})"
        if caveats:
            note += " ⚠️ " + "; ".join(caveats)

        compute_line = CostLine(
            label=f"BigQuery capacity ({edition})", monthly=round(total_monthly, 4),
            monthly_low=None, monthly_high=None, confidence=conf, source_note=note,
        )
        storage_line = self._bq_storage_line(total_bytes)
        return round(total_monthly + _line_value(storage_line), 4), [storage_line, compute_line], None

    def _blended_baseline_cost(
        self, baseline: int, pricing: PricingDetection,
        edition: str, edition_rates: dict, caveats: list[str],
    ) -> float:
        """Compute baseline cost using blended rate across all commitments.

        Each commitment covers up to slot_count at its plan rate; slots beyond
        total commitments bill at PAYG. If no commitment list is available, falls
        back to the single-plan approach.
        """
        payg_rate = edition_rates["payg"]

        if not baseline:
            return 0.0

        commitments = pricing.commitments
        if not commitments:
            rate_key = k.COMMITMENT_PLAN_TO_RATE_KEY.get(
                pricing.commitment_plan or "FLEX", "payg"
            )
            if edition not in v4.V4_EDITIONS_WITH_CAPACITY_COMMITMENTS and rate_key != "payg":
                caveats.append(
                    f"{edition} has no true slot commitments — "
                    f"{pricing.commitment_plan} priced at PAYG"
                )
                rate_key = "payg"
            rate = edition_rates.get(rate_key)
            if rate is None:
                rate = payg_rate
            return baseline * rate * k.HOURS_PER_MONTH

        # Blended: allocate slots to each commitment at its rate, remainder at PAYG.
        remaining = baseline
        total_cost = 0.0
        rate_parts: list[str] = []

        for c in commitments:
            if remaining <= 0:
                break
            slots_covered = min(c.get("slot_count", 0), remaining)
            if slots_covered <= 0:
                continue
            plan = c.get("plan", "FLEX")
            rate_key = k.COMMITMENT_PLAN_TO_RATE_KEY.get(plan, "payg")
            if edition not in v4.V4_EDITIONS_WITH_CAPACITY_COMMITMENTS:
                rate_key = "payg"
            rate = edition_rates.get(rate_key)
            if rate is None:
                rate = payg_rate
            total_cost += slots_covered * rate * k.HOURS_PER_MONTH
            remaining -= slots_covered
            rate_parts.append(f"{slots_covered}@{plan}")

        if remaining > 0:
            total_cost += remaining * payg_rate * k.HOURS_PER_MONTH
            rate_parts.append(f"{remaining}@PAYG(uncovered)")

        if len(rate_parts) > 1:
            caveats.append(f"blended rate: {', '.join(rate_parts)}")

        return total_cost

    def _bq_capacity_from_slots(
        self, edition: str, edition_rates: dict,
        slots: SlotUtilization | None, total_bytes: int, caveats: list[str],
    ):
        """Price STANDARD capacity as a modelled range from measured slot use.

        STANDARD has a single public PAYG rate (no commitments exist), so the rate
        is certain — but Google bills SCALED capacity, not consumed slot-ms:
        autoscale steps are multiples of 50 slots with a 1-minute minimum, and the
        docs say not to reconcile billing from JOBS. slot_ms × rate is a measured
        minimum; the upper figure models 50-slot steps spread over busy hours.
        Neither endpoint is a hard bound: billing follows per-minute scaled peaks,
        so a bursty workload's real bill can EXCEED the upper estimate, and hourly
        aggregates can't distinguish that case (verified 2026-08-10). Totals
        use the upper estimate (headline) as the less-flattering-to-AWS anchor.
        HIGH confidence is reserved for the reservation-timeline path.
        """
        payg_rate = edition_rates["payg"]
        storage_line = self._bq_storage_line(total_bytes)

        if not slots or slots.total_slot_ms <= 0:
            caveats.append("no workload data — cannot estimate slot consumption")
            compute_line = CostLine(
                label=f"BigQuery capacity ({edition})", monthly=0.0,
                monthly_low=None, monthly_high=None,
                confidence=ConfidenceLevel.LOW,
                source_note=(
                    f"BigQuery {edition} capacity detected but no workload data to "
                    f"estimate slot consumption. Supply --bigquery-monthly-cost for "
                    f"an exact figure."
                ),
            )
            return round(_line_value(storage_line), 4), [storage_line, compute_line]

        # Measured minimum: consumed slot-hours at PAYG (what JOBS proves was used).
        consumed_slot_hours = slots.total_slot_ms / _MS_PER_HOUR
        window_days = max(slots.days_sampled, 1)
        floor_monthly = consumed_slot_hours / window_days * k.DAYS_PER_MONTH * payg_rate

        # Modelled upper estimate: 50-slot autoscale steps spread over busy hours.
        # NOT a hard ceiling — billing follows per-minute scaled peaks, which hourly
        # aggregates cannot reconstruct; a bursty workload's real bill can exceed it.
        active_frac = min(max(slots.active_hour_fraction, 1.0 / 720.0), 1.0)
        busy_concurrency = slots.avg_slots / active_frac
        billed_slots = max(50.0, math.ceil(busy_concurrency / 50.0) * 50.0)
        ceiling_monthly = max(
            billed_slots * active_frac * k.HOURS_PER_MONTH * payg_rate,
            floor_monthly,
        )

        caveats.append(
            f"billed capacity ≥ consumed: minimum = {consumed_slot_hours:,.0f} measured "
            f"slot-hrs over {window_days}d @ PAYG ${payg_rate}/slot-hr; upper estimate "
            f"models 50-slot autoscale steps ({billed_slots:.0f} slots × {active_frac:.0%} "
            f"active hours); {edition} has no commitment discounts"
        )
        caveats.append(
            "actual bill can exceed this range for bursty workloads — Google bills "
            "per-minute scaled peaks, not hourly averages; assumes standard "
            "autoscaling (1-minute minimum; fluid-scaling opt-in bills less). "
            "Supply --bigquery-monthly-cost for an exact figure"
        )

        note = (
            f"BigQuery {edition} capacity, modelled range: "
            f"${floor_monthly:,.2f}–${ceiling_monthly:,.2f}/mo "
            f"@ PAYG ${payg_rate}/slot-hr ({v4.V4_PRICING_REGION}, verified "
            f"{v4.V4_CONFIRMED_DATE})"
        )
        if caveats:
            note += " ⚠️ " + "; ".join(caveats)

        compute_line = CostLine(
            label=f"BigQuery capacity ({edition})", monthly=None,
            monthly_low=round(floor_monthly, 4), monthly_high=round(ceiling_monthly, 4),
            confidence=ConfidenceLevel.MEDIUM, source_note=note,
            headline=round(ceiling_monthly, 4),
        )
        total = round(_line_value(storage_line) + _line_value(compute_line), 4)
        return total, [storage_line, compute_line]

    def _bq_capacity_unavailable(self, pricing: PricingDetection, total_bytes: int):
        """Hard stop: capacity detected but rate AND quantity are unknowable — no estimate.

        Returns (0.0, lines, reason). The 0.0 is deliberate: the storage figure must
        not masquerade as the BigQuery bill in the comparison (2026-08-10 sandbox validation:
        storage-only $2,986 rendered as 'BigQuery (Current)' and produced a fabricated
        'Save $412/mo'). Storage is still shown as a line inside the notice.
        """
        admin_project = None
        if pricing.reservation_id:
            from bq_assess.core.reservation_reader import parse_admin_project
            parsed = parse_admin_project(pricing.reservation_id)
            if parsed:
                admin_project = parsed[0]

        edition = pricing.edition
        edition_phrase = f"{edition} " if edition else ""
        # Cheapest remedy first where applicable: reading an EXISTING Cloud Billing
        # BigQuery export typically needs only dataset-level read access — far easier
        # to grant than admin-project reservation permissions (2026-08-10 review).
        billing_export_hint = (
            " If a Cloud Billing BigQuery export already exists, read access to that "
            "dataset typically suffices to obtain the exact billed amount."
        )
        if not pricing.reservation_data_collected:
            reason = (
                f"This Source uses BigQuery {edition_phrase}capacity pricing, but this bundle was "
                "produced by an older collector that did not capture reservation details "
                "(baseline slots, commitments, autoscale usage). Re-collect with "
                "bq-collect >= 0.8, or supply your total monthly BigQuery bill via "
                "--bigquery-monthly-cost." + billing_export_hint
            )
        else:
            reason = (
                f"This Source uses BigQuery {edition_phrase}capacity pricing, but reservation "
                "details could not be read (permission denied). Grant "
                "roles/bigquery.resourceViewer on the reservation admin project"
                + (f" ({admin_project})" if admin_project else "")
                + " and re-collect, or supply your total monthly BigQuery bill via "
                "--bigquery-monthly-cost." + billing_export_hint
            )

        storage_line = self._bq_storage_line(total_bytes)
        label_edition = edition or "capacity-based"
        compute_line = CostLine(
            label=f"BigQuery capacity ({label_edition}) — UNAVAILABLE", monthly=None,
            monthly_low=None, monthly_high=None,
            confidence=ConfidenceLevel.LOW, source_note=reason,
        )
        return 0.0, [storage_line, compute_line], reason

    # ================================================================== Justification helpers

    def _redshift_elaborated_justification(
        self, slots: SlotUtilization, engine_recommendation, scenario_type: str,
    ) -> str:
        """Elaborated justification for recommended Redshift scenario (R19 unified surface).

        Composes 3-5 sentences from engine_recommendation data: scan volume vs crossover,
        pattern fit, confidence inline, revisit conditions, warmth caveat if present.
        """
        window_days = _window_days(slots)
        qpd = slots.total_queries / window_days
        basis_bytes, _ = _scan_basis(slots)
        monthly_scanned_tb = (basis_bytes / window_days * k.DAYS_PER_MONTH) / (1024 ** 4)
        daily_scanned_tb = monthly_scanned_tb / k.DAYS_PER_MONTH

        crossover = engine_recommendation.crossover_point_tb_day
        confidence_pct = int(engine_recommendation.confidence * 100)

        # Core justification: volume, crossover, pattern. The crossover-assumptions
        # sentence describes Serverless's 4-RPU minimum posture — only relevant when
        # the recommended scenario IS Serverless (a provisioned recommendation
        # mentioning "4 RPU" reads as a contradiction — sandbox feedback).
        justification = (
            f"Your workload scans {monthly_scanned_tb:.2f} TB/month ({daily_scanned_tb:.2f} TB/day, "
            f"{qpd:,.0f} queries/day) — far below the ~{crossover:.2f} TB/day crossover where "
            f"Redshift Serverless becomes cheaper. "
        )
        if scenario_type == "serverless":
            justification += (
                "The crossover assumes Redshift's minimum posture "
                "(4 RPU, ~8 active hours/day); always-on workloads break even nearer 8 TB/day. "
                "Note: if the workgroup auto-scales above 4 RPUs it will not scale back down "
                "automatically — the 4-RPU floor requires manual reset, so sustained bursts "
                "erode this posture. "
            )

        # Pattern fit
        active_frac = slots.active_hour_fraction
        if active_frac < 0.3:
            pattern = "intermittent"
        elif active_frac < 0.6:
            pattern = "moderate"
        else:
            pattern = "sustained"

        justification += (
            f"Peak concurrency of {int(slots.peak_slots * k.V3_SLOT_TO_RPU_RATIO)} RPU and a {pattern} "
            f"pattern (active {active_frac:.0%} of hours) suit Redshift's per-second billing — "
            f"you pay nothing between queries. "
        )

        # Confidence inline
        justification += f"Engine analysis: {confidence_pct}% confidence. "

        # Revisit conditions
        if scenario_type == "serverless":
            dml_quota = 200 if k.AWS_PRICING_REGION == "us-east-1" else 100
            justification += (
                f"Revisit if scan volume approaches {crossover:.2f} TB/day, sustained concurrency "
                f"approaches your account's Athena DML quota (default {dml_quota} in "
                f"{k.AWS_PRICING_REGION}, adjustable), or sub-3-second latency SLAs emerge."
            )
        else:
            justification += (
                f"Revisit if scan volume approaches {crossover:.2f} TB/day or query patterns become "
                f"more sustained (>60% active hours)."
            )

        # Warmth caveat (check for sla_warmth_caveat signal)
        for sig in engine_recommendation.reasoning:
            if sig.signal == 'sla_warmth_caveat':
                justification += (
                    f" Note: Sub-3s SLA requested but workload is idle {sig.value:.0%} of time — "
                    f"a suspended Redshift Serverless workgroup resumes in ~30s; neither engine "
                    f"guarantees sub-3s on a cold path."
                )
                break

        return justification

    def _provisioned_justification(
        self, profile: WorkloadProfile, node_type: str, node_count: int,
        rate_key: str, label_suffix: str, total: float,
    ) -> str:
        """Customer-specific justification for a provisioned scenario."""
        spec = k.V7_RG_NODE_TYPES[node_type]
        qpd = profile.queries_per_day
        # Same peak figure the fit notes use — quoting bare peak_concurrent_queries
        # here rendered "~0 peak" beside a fit note warning about 191 peak
        # (2026-08-04 audit).
        peak_conc = max(profile.peak_concurrent_queries, profile.peak_slots or 0)
        total_vcpu = node_count * spec["vcpu"]

        return (
            f"Sized for {qpd:,.0f} queries/day with ~{peak_conc:.0f} peak concurrent queries. "
            f"{node_count}× {node_type} (Graviton4) provides {total_vcpu} vCPUs and "
            f"{node_count * spec['memory_gb']} GB RAM — 30% better price/vCPU vs RA3. "
            f"Rate: ${spec[rate_key]}/node-hr ({label_suffix})."
        )

    def _provisioned_fit_notes(
        self, profile: WorkloadProfile, node_type: str, node_count: int
    ) -> list[str]:
        """Workload-fit notes for a provisioned scenario."""
        spec = k.V7_RG_NODE_TYPES[node_type]
        notes = []
        total_vcpu = node_count * spec["vcpu"]
        peak_conc = max(profile.peak_concurrent_queries, profile.peak_slots or 0)

        if peak_conc < 1:
            return notes
        if total_vcpu >= peak_conc * k.V6_VCPU_PER_CONCURRENT_QUERY:
            notes.append(f"✓ {total_vcpu} vCPUs handles {peak_conc:.0f} peak concurrent queries")
        else:
            notes.append(f"⚠ {total_vcpu} vCPUs may be tight for {peak_conc:.0f} peak concurrent queries — concurrency scaling will activate")

        active_frac = profile.active_hour_fraction
        if active_frac > 0.5:
            notes.append(f"✓ High utilization ({active_frac:.0%} active hours) — provisioned is cost-effective")
        elif active_frac < 0.2:
            notes.append(f"⚠ Low utilization ({active_frac:.0%} active hours) — cluster idle most of the time")

        return notes


def _serverless_fit_notes(slots: SlotUtilization | None) -> list[str]:
    """Workload-fit notes for the serverless scenario."""
    notes = []
    if slots is None or slots.total_slot_ms == 0:
        notes.append("No workload data — serverless is a safe default (scales to zero)")
        return notes

    qpd = slots.total_queries / _window_days(slots)
    if qpd > 50_000:
        notes.append(f"⚠ {qpd:,.0f} queries/day is high volume — serverless per-second billing adds up quickly")
    else:
        notes.append(f"✓ {qpd:,.0f} queries/day — moderate volume suits serverless pay-per-use")

    if slots.active_hour_fraction < 0.3:
        notes.append(f"✓ Active only {slots.active_hour_fraction:.0%} of hours — serverless scales to zero during idle")
    else:
        notes.append(f"⚠ Active {slots.active_hour_fraction:.0%} of hours — always-on provisioned may be cheaper")

    return notes


# ================================================================== Module helpers


def _window_days(slots: SlotUtilization) -> int:
    """The calendar window (days) scan volume is projected over — ONE definition.

    max(lookback_days, days_sampled, 1): lookback_days is the calendar span, but
    hand-built SlotUtilization values may set days_sampled above the defaulted
    lookback_days=30 — the clamp keeps the denominator ≥ the observed activity so the
    cost line and the workload profile can never disagree on the projection window.
    """
    return max(getattr(slots, "lookback_days", slots.days_sampled), slots.days_sampled, 1)


def _scan_basis(slots: SlotUtilization | None) -> tuple[int, str]:
    """The scan-volume basis both the BQ cost line and the workload profile share.

    Prefers total_bytes_billed (what on-demand billing charges — 10 MiB per-query
    minimums included) only when EVERY job in the window carried billed data
    (has_billed_bytes), honoring a genuine zero. A degraded window's total_bytes_billed
    is a PARTIAL sum (NULL-billed jobs' volume excluded — workload.py's billed policy),
    so pricing on it would be a silent underestimate; degraded windows fall back to
    total_bytes_processed, the labelled overestimate. Returns (bytes, label);
    (0, "") when slots is None.
    """
    if slots is None:
        return 0, ""
    if getattr(slots, "has_billed_bytes", False):
        return slots.total_bytes_billed, "billed"
    return slots.total_bytes_processed, "processed (billed unavailable)"


def _safe_num(value) -> float:
    try:
        n = float(value)
    except (ValueError, TypeError):
        return 0.0
    return n if n >= 0 else 0.0


def _longterm_bytes(entities, as_of: datetime | None) -> int:
    """Bytes in entities unmodified for V4_LONGTERM_THRESHOLD_DAYS+ days.

    ``as_of`` anchors the idle window (bundle collection time — NOT report time,
    which would drift tables into long-term as a bundle ages on disk). Entities
    without a usable last_modified count as active (conservative for the
    comparison: overstating the BQ side is the misleading direction).
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    cutoff = as_of - timedelta(days=v4.V4_LONGTERM_THRESHOLD_DAYS)
    total = 0
    for e in entities:
        lm = getattr(e, "last_modified", None)
        if lm is None:
            continue
        if lm.tzinfo is None:
            lm = lm.replace(tzinfo=timezone.utc)
        if lm < cutoff:
            total += _entity_bytes(e)
    return total


def _int_tier_breakdown(entities, as_of: datetime | None) -> dict:
    """Per-tier table counts and physical bytes for S3 Tables Intelligent-Tiering.

    Returns {"frequent"|"infrequent"|"archive": {"tables": int, "bytes": int}},
    weighted by PHYSICAL bytes (what S3 bills). Mirrors _longterm_bytes:
    last_modified is the recency signal, as_of anchors the window to bundle
    collection time, naive timestamps read as UTC.
    ⚠️ last_modified is a MODIFICATION proxy for ACCESS recency — a daily-read
    static table would stay Frequent in reality. Callers must pair the tiered
    figure with the all-Frequent bound and surface the caveat. Entities without
    last_modified count as Frequent (conservative: overstates the AWS bill,
    the same safe direction as _longterm_bytes' count-as-active).
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    ia_cutoff = as_of - timedelta(days=k.V2_INT_INFREQUENT_THRESHOLD_DAYS)
    arc_cutoff = as_of - timedelta(days=k.V2_INT_ARCHIVE_THRESHOLD_DAYS)

    tiers = {
        "frequent": {"tables": 0, "bytes": 0},
        "infrequent": {"tables": 0, "bytes": 0},
        "archive": {"tables": 0, "bytes": 0},
    }
    for e in entities:
        b = _entity_physical_bytes(e)
        lm = getattr(e, "last_modified", None)
        if lm is None:
            key = "frequent"  # conservative
        else:
            if lm.tzinfo is None:
                lm = lm.replace(tzinfo=timezone.utc)
            if lm < arc_cutoff:
                key = "archive"
            elif lm < ia_cutoff:
                key = "infrequent"
            else:
                key = "frequent"
        tiers[key]["tables"] += 1
        tiers[key]["bytes"] += b
    return tiers


def _int_tier_split(entities, as_of: datetime | None) -> tuple[float, float, float]:
    """Fractions of total physical bytes per Intelligent-Tiering tier.

    Returns (frequent, infrequent, archive_instant) summing to 1.0
    ((1.0, 0.0, 0.0) for an empty/zero-byte estate). Thin wrapper over
    _int_tier_breakdown — see its docstring for the signal and caveats.
    """
    tiers = _int_tier_breakdown(entities, as_of)
    total = sum(t["bytes"] for t in tiers.values())
    if total <= 0:
        return (1.0, 0.0, 0.0)
    ia = tiers["infrequent"]["bytes"] / total
    arc = tiers["archive"]["bytes"] / total
    return (1.0 - ia - arc, ia, arc)


def _entity_bytes(e) -> int:
    if hasattr(e, "num_bytes"):
        return int(e.num_bytes)
    if hasattr(e, "size_gb"):
        return int(e.size_gb * (1024 ** 3))
    return 0


def _entity_physical_bytes(e) -> int:
    """Get physical bytes from entity (physical_bytes field, or fallback via helper)."""
    from bq_assess.core.storage_stats import effective_physical_bytes
    return effective_physical_bytes(_entity_bytes(e), getattr(e, "physical_bytes", None))


def _tiered_s3_tables_usd(gb: float) -> float:
    remaining = gb
    usd = 0.0
    t1 = min(remaining, k.V2_TIER1_WIDTH_GB)
    usd += t1 * k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER1
    remaining -= t1
    if remaining > 0:
        t2 = min(remaining, k.V2_TIER2_WIDTH_GB)
        usd += t2 * k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER2
        remaining -= t2
    if remaining > 0:
        usd += remaining * k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER3
    return usd


def _rpu_hours_per_month(slots: SlotUtilization) -> float:
    slot_hours = slots.total_slot_ms / _MS_PER_HOUR
    return slot_hours * k.V3_SLOT_TO_RPU_RATIO


def _line_value(line: CostLine) -> float:
    """Get the effective monthly value from a CostLine.

    Precedence: headline (the totals-basis figure — see CostLine.headline) →
    point value → range midpoint.
    """
    if getattr(line, "headline", None) is not None:
        return line.headline
    if line.monthly is not None:
        return line.monthly
    low = line.monthly_low or 0
    high = line.monthly_high or low
    return (low + high) / 2


def _line_low(line: CostLine) -> float:
    if getattr(line, "headline", None) is not None:
        return line.headline
    return line.monthly if line.monthly is not None else (line.monthly_low or 0)


def _line_high(line: CostLine) -> float:
    if getattr(line, "headline", None) is not None:
        return line.headline
    return line.monthly if line.monthly is not None else (line.monthly_high or 0)


def _bq_breakdown_low(lines: list, total: float) -> float | None:
    """Measured-minimum total of a BQ breakdown, or None for point-estimate bases.

    Only meaningful when at least one line is a modelled range (monthly=None with
    monthly_low/high set — the STANDARD capacity-from-slots path). Point lines
    (storage, egress) contribute their point value on both bases; the range line
    contributes its low here vs its headline in the total. Call AFTER all lines
    are appended so both totals cover the same line set.
    """
    has_range = any(
        ln.monthly is None and ln.monthly_low is not None for ln in lines
    )
    if not has_range:
        return None
    low = sum(
        ln.monthly_low if ln.monthly is None and ln.monthly_low is not None
        else _line_value(ln)
        for ln in lines
    )
    return round(min(low, total), 4)


def _breakeven(onetime: float, monthly_delta: float) -> float:
    if monthly_delta <= 0:
        return k.BREAKEVEN_NEVER
    return onetime / monthly_delta


def collect_rms_bytes(
    entities,
    storage_placements: dict,
    engine_placements: dict,
) -> tuple[int, int]:
    """Sum the two RMS byte pools for the storage split (Redshift path only).

    Returns ``(table_rms_bytes, mv_rms_bytes)``:

    - Table pool: TABLE entities whose Stage 13a StoragePlacement is RMS.
    - MV pool: MATERIALIZED_VIEW entities whose Stage 13 engine placement homes
      them in Redshift — a native Redshift MV stores its materialized result set
      in RMS. Plain views and UDFs homed in Redshift contribute nothing (no
      materialized storage).

    Kept separate because their confidence differs (generated DDL vs BQ-bytes
    size proxy) — apply_rms_storage_split names each pool in the line's note.
    """
    from bq_assess.models import EntityType, StorageTarget

    table_rms = 0
    mv_rms = 0
    for e in entities:
        sp = storage_placements.get(e.full_name)
        if sp is not None and sp.target == StorageTarget.RMS:
            table_rms += _entity_physical_bytes(e)
            continue
        if e.entity_type == EntityType.MATERIALIZED_VIEW:
            ep = engine_placements.get(e.full_name)
            if ep is not None and ep.home == "REDSHIFT":
                mv_rms += _entity_physical_bytes(e)
    return table_rms, mv_rms


def apply_rms_storage_split(
    cost: CostComparison,
    rms_physical_bytes: int,
    total_physical_bytes: int,
    mv_physical_bytes: int = 0,
) -> None:
    """Split the storage line after Stage 13a/13 placed entities on RMS, in place.

    Serverless bills RMS storage separately by GB/month (serverless-billing.html,
    verified 2026-07-22) — RMS-resident bytes leave the S3 Tables tier and incur
    an RMS line instead. Two pools feed the RMS line:

    - ``rms_physical_bytes``: TABLE entities Stage 13a placed on RMS (ADR-0005
      fidelity exception). HIGH confidence — we generated the native DDL.
    - ``mv_physical_bytes``: MATERIALIZED_VIEW entities Stage 13 homed in Redshift.
      A native Redshift MV stores its materialized result set in RMS on both
      Serverless and Provisioned; the BQ MV's measured bytes are the size proxy.
      MEDIUM confidence — the Redshift materialization won't be byte-identical
      and refresh churn is not modeled.

    ``total_physical_bytes`` is the full estate the S3 line was priced on: the
    reduced line is recomputed through the tier function at the remaining volume
    (marginal bytes leave the TOP occupied tier — a flat tier-1 subtraction
    over-reduced the line on >50 TB estates) and the object-monitoring component
    is re-estimated for the remaining objects.

    The substitution is applied to EVERY Redshift scenario's lines and
    monthly_total, not just the headline (they all priced storage identically, so
    a headline-only split left scenario totals disagreeing with the breakdown by
    the RMS delta). Athena scenarios are skipped by category — Redshift Managed
    Storage cannot exist on an Athena deployment; a pristine copy of the storage
    line is stashed on ``cost.all_iceberg_storage_line`` for the Athena scenario
    assembled later (engine/comparison.py), which must price ALL bytes as Iceberg.
    RMS is priced via the region-applied V6 constant (apply_aws_region /
    live-rate override), so region correctness follows the rest of the AWS side.
    """
    total_rms_bytes = max(0, rms_physical_bytes) + max(0, mv_physical_bytes)
    if total_rms_bytes <= 0:
        return
    # Suppress a line that would render $0.00 (empty RMS-placed tables) —
    # a zero-value line item is noise; the placement itself still shows on
    # the entities (2026-08-04 audit: sandbox estate carried '0.0 GB × rate = $0.00').
    if total_rms_bytes * k.GB_PER_BYTE * k.V6_MANAGED_STORAGE_USD_PER_GB_MONTH < 0.005:
        return

    # Every non-Athena line list that carries a storage line gets the identical
    # substitution. aws_lines usually IS the best scenario's list — dedupe by
    # identity so shared references aren't split twice.
    line_lists: list[list[CostLine]] = []
    seen_ids: set[int] = set()
    scenario_by_list: dict[int, AWSScenario] = {}
    for scenario in cost.aws_scenarios:
        if "ATHENA" in (scenario.category or "").upper():
            continue  # hardening: RMS must never appear on an Athena option
        line_lists.append(scenario.lines)
        seen_ids.add(id(scenario.lines))
        scenario_by_list[id(scenario.lines)] = scenario
    if id(cost.aws_lines) not in seen_ids:
        line_lists.append(cost.aws_lines)

    first_s3 = next(
        (ln for lines in line_lists for ln in lines if ln.label == "S3 Tables storage"),
        None,
    )
    if first_s3 is None:
        return
    # Pristine all-Iceberg copy for the Athena scenario built at Stage 13b: if the
    # customer chose Athena there would be no RMS, so it prices the full estate as
    # S3 Tables. Without this, the Athena option inherited the reduced S3 line
    # from scenarios[0] and silently dropped the RMS bytes from its total.
    cost.all_iceberg_storage_line = dataclasses.replace(first_s3)

    rms_gb = total_rms_bytes * k.GB_PER_BYTE
    # Point OR range line (Intelligent-Tiering makes it a range with a headline —
    # `monthly is None` used to silently skip the split, so RMS-placed tables got
    # no RMS line and totals stopped reconciling with placement, 2026-08-03 audit).
    old_s3_usd = _line_value(first_s3)
    remaining_bytes = max(0, total_physical_bytes - total_rms_bytes)
    remaining_objects = remaining_bytes / (k.V2_ASSUMED_OBJECT_SIZE_MB * 1e6)
    reduced_s3_usd = (
        _tiered_s3_tables_usd(remaining_bytes * k.GB_PER_BYTE)
        + remaining_objects / 1000.0 * k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH
    )
    reduced_s3_usd = min(reduced_s3_usd, old_s3_usd)  # a split must never raise the line
    reduction = old_s3_usd - reduced_s3_usd
    rms_usd = rms_gb * k.V6_MANAGED_STORAGE_USD_PER_GB_MONTH

    # Componentized provenance: name each pool so the line is auditable.
    components = []
    if rms_physical_bytes > 0:
        components.append(
            f"{fmt_size_exact(rms_physical_bytes * k.GB_PER_BYTE)} RMS-placed tables"
        )
    if mv_physical_bytes > 0:
        components.append(
            f"{fmt_size_exact(mv_physical_bytes * k.GB_PER_BYTE)} Redshift-native MVs "
            f"(BQ MV bytes as size proxy; refresh churn not modeled)"
        )
    source_note = (
        f"{' + '.join(components)} × ${k.V6_MANAGED_STORAGE_USD_PER_GB_MONTH}/GB-mo "
        f"{k.AWS_REGION_SCOPE} — RMS is billed separately from compute on both "
        f"Serverless and Provisioned (verified {k.AWS_CONFIRMED_DATE})"
    )
    rms_confidence = (
        ConfidenceLevel.MEDIUM if mv_physical_bytes > 0 else ConfidenceLevel.HIGH
    )

    def _split_lines(lines: list[CostLine]) -> bool:
        """Apply the substitution to one line list; True when a split happened."""
        s3_line = next((ln for ln in lines if ln.label == "S3 Tables storage"), None)
        if s3_line is None:
            return False
        if s3_line.monthly is not None:
            s3_line.monthly = round(reduced_s3_usd, 4)
        else:
            # Range line: shift every figure by the reduction; the low/headline pair
            # stays the totals basis, the high bound keeps its month-1 meaning.
            s3_line.monthly_low = round(max(0.0, (s3_line.monthly_low or 0) - reduction), 4)
            s3_line.monthly_high = round(max(0.0, (s3_line.monthly_high or 0) - reduction), 4)
            if getattr(s3_line, "headline", None) is not None:
                s3_line.headline = round(max(0.0, s3_line.headline - reduction), 4)
        s3_line.source_note += f"; {fmt_size_exact(rms_gb)} moved to RMS by storage placement"
        lines.append(CostLine(
            label="Redshift Managed Storage (RMS)",
            monthly=round(rms_usd, 4), monthly_low=None, monthly_high=None,
            confidence=rms_confidence,
            source_note=source_note,
        ))
        return True

    for lines in line_lists:
        if not _split_lines(lines):
            continue
        scenario = scenario_by_list.get(id(lines))
        if scenario is not None:
            # Recompute from the lines rather than adding a delta — keeps the
            # total reconciling exactly with its own line items after rounding.
            scenario.monthly_total = round(sum(_line_value(ln) for ln in lines), 4)

    # Recompute headline figures from the (now-split) aws_lines.
    cost.aws_monthly_low = round(sum(_line_low(ln) for ln in cost.aws_lines), 4)
    cost.aws_monthly_high = round(sum(_line_high(ln) for ln in cost.aws_lines), 4)
    if cost.bq_cost_available:
        bq_low_basis = (
            cost.bigquery_monthly_low
            if cost.bigquery_monthly_low is not None
            else cost.bigquery_monthly
        )
        cost.monthly_delta_low = bq_low_basis - cost.aws_monthly_high
        cost.monthly_delta_high = cost.bigquery_monthly - cost.aws_monthly_low
        cost.annual_savings_low = cost.monthly_delta_low * 12
        cost.annual_savings_high = cost.monthly_delta_high * 12
        cost.breakeven_months_low = _breakeven(cost.migration_onetime, cost.monthly_delta_low)
        cost.breakeven_months_high = _breakeven(cost.migration_onetime, cost.monthly_delta_high)


def reprice_migration_effort(cost: CostComparison, effort_total) -> None:
    """Recompute migration_onetime + breakeven from an updated effort total, in place.

    Stage 13a (storage placement, ADR-0005) amends per-entity effort AFTER Stage 10
    priced the migration — RMS-placed entities gain a two-phase-load point. This
    re-applies the same formula estimate() used so the cost summary and the
    per-entity effort cards agree. Only the effort-derived fields change; monthly
    run-rate figures are untouched.
    """
    cost.migration_onetime = _safe_num(effort_total) * k.MIGRATION_USD_PER_EFFORT_POINT
    cost.breakeven_months_low = _breakeven(cost.migration_onetime, cost.monthly_delta_low)
    cost.breakeven_months_high = _breakeven(cost.migration_onetime, cost.monthly_delta_high)


def _fmt_usd(amount: float) -> str:
    """Format a USD monthly figure for customer-facing prose.

    Sub-dollar totals must NOT round to ``$0`` (that reads as free/broken when a tiny
    workload genuinely costs e.g. $0.12/mo). Show two decimals under $1, comma-grouped
    whole dollars at $1+.
    """
    a = abs(amount)
    sign = "-" if amount < 0 else ""
    if a < 1:
        return f"{sign}${a:.2f}"
    return f"{sign}${a:,.0f}"
