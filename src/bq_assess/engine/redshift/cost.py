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

import logging
import math

from bq_assess.core import pricing_constants as v4
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

        # ---- BigQuery side: per detected model, override wins (R18.2) ----
        bigquery_monthly, bq_breakdown = self._bigquery_runrate(
            total_bytes, pricing, slots, bq_monthly_override
        )

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
        monthly_delta_low = bigquery_monthly - aws_monthly_high
        monthly_delta_high = bigquery_monthly - aws_monthly_low
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
            scope_notes=self._scope_notes(),
            pricing_notes=self._pricing_notes(bq_location, region_known, bq_monthly_override),
            key_uncertainties=self._key_uncertainties(slots),
            estimate_basis_level=basis_level,
            estimate_basis=basis_text,
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
                "research-based assumption, set above the evidence midpoint (range "
                "0.06–0.25) so the AWS compute figure is deliberately estimated on the high side. "
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
            "The Athena-vs-Redshift crossover (~2.7 TB/day, ap-southeast-2) assumes Redshift "
            "Serverless at its 4-RPU/8-hour minimum posture; always-on workloads break even "
            "nearer 8 TB/day."
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
            "Athena priced at $5.00/TB scanned (on-demand, region-invariant; verified 2026-07-21); "
            "DDL and failed queries are not billed; a 10 MB per-query minimum applies.",
            "Athena one-time OPTIMIZE compaction estimated at $5/TB of stored data — an upper bound "
            "based on uncompressed BigQuery logical size (Parquet on S3 is typically several times smaller).",
            "Monthly figures normalize to a 30-day month; calendar months of 31 days "
            "bill ~3% higher.",
        ]
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
        return [
            "BigQuery side: covers on-demand analysis (bytes billed) and active logical "
            "storage only. Not modeled: streaming inserts, Storage Read/Write API, "
            "BI Engine, Data Transfer Service — these appear on the BigQuery bill and can "
            "be significant for ingestion- or extract-heavy projects. The on-demand free "
            "tier (1 TiB scan + 10 GiB storage/month) and negotiated discounts are also "
            "excluded.",
            "AWS side: one-time migration data transfer is not included; Redshift "
            "Spectrum, ML features, and cross-region replication are not modeled. "
            "Glue Data Catalog request charges and S3 request charges are not modeled for either Query Engine.",
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
            label=f"Provisioned {config_label} ({label_suffix})",
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
                # single-breath "ratio 0.2 (assumption ...) (verified ...)" read as a
                # contradiction ("assumption or verified?").
                source_note=(
                    f"${k.V1_RPU_HOUR_USD}/RPU-hr (rate verified {k.AWS_CONFIRMED_DATE}); "
                    f"sized via slot-to-RPU ratio {k.V3_SLOT_TO_RPU_RATIO} — an assumption, "
                    f"validate with a pilot workload"
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
        """S3 Tables tiered storage (V2) — sized on physical (compressed) bytes."""
        gb = total_physical_bytes * k.GB_PER_BYTE
        usd = _tiered_s3_tables_usd(gb)

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
            f"{gb:,.1f} GB {basis_phrase} × tiered from "
            f"${k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER1}/GB-mo "
            f"{k.AWS_REGION_SCOPE} (verified {k.AWS_CONFIRMED_DATE})"
        )

        return CostLine(
            label="S3 Tables storage",
            monthly=round(usd, 4), monthly_low=None, monthly_high=None,
            confidence=confidence,
            source_note=note,
        )

    # ================================================================== BigQuery

    def _bigquery_runrate(self, total_bytes, pricing, slots, override):
        """BigQuery monthly run-rate + explaining breakdown lines (R18.2 / R16.4)."""
        if override is not None:
            line = CostLine(
                label="BigQuery (operator override)", monthly=round(float(override), 4),
                monthly_low=None, monthly_high=None, confidence=ConfidenceLevel.HIGH,
                source_note="operator-supplied via --bigquery-monthly-cost (no price-list date)",
            )
            return float(override), [line]

        if pricing.model is BQPricingModel.CAPACITY:
            return self._bq_capacity(pricing)
        return self._bq_ondemand(total_bytes, slots)

    def _bq_ondemand(self, total_bytes, slots):
        gib = total_bytes / (1024 ** 3)
        storage_usd = gib * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
        region = v4.V4_PRICING_REGION

        basis_bytes, basis_label = _scan_basis(slots)
        # A carried-but-zero billed total (all-cached / reservation-served window) is a
        # genuine $0 scan month — don't fall through to the stored-bytes proxy branch.
        billed_zero_window = (
            slots is not None and slots.has_billed_bytes and slots.total_bytes_billed == 0
        )

        if slots is not None and slots.days_sampled > 0 and (basis_bytes > 0 or billed_zero_window):
            # Project over the CALENDAR window, not just active days: dividing by
            # days_sampled inflates sparse/batch workloads (10 TiB across 4 active days
            # in a 30-day window is ~10 TiB/month, not 75).
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
                f"proxy, no logs) (verified {v4.V4_CONFIRMED_DATE})"
            )

        lines = [
            CostLine(
                label="BigQuery storage", monthly=round(storage_usd, 4),
                monthly_low=None, monthly_high=None, confidence=ConfidenceLevel.HIGH,
                source_note=(
                    f"{gib:,.1f} GiB stored × ${v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH}/GiB-mo "
                    f"({region}) (verified {v4.V4_CONFIRMED_DATE})"
                ),
            ),
            CostLine(
                label="BigQuery bytes scanned", monthly=round(scanned_usd, 4),
                monthly_low=None, monthly_high=None, confidence=scan_conf,
                source_note=scan_note,
            ),
        ]
        return round(storage_usd + scanned_usd, 4), lines

    def _bq_capacity(self, pricing: PricingDetection):
        """Price a capacity Source from its reservation figures."""
        caveats: list[str] = []

        edition = pricing.edition if pricing.edition in v4.V4_EDITION_SLOT_HOUR_USD else None
        edition_known = edition is not None
        if not edition_known:
            if pricing.edition:
                caveats.append(f"unrecognized edition {pricing.edition!r}, priced at ENTERPRISE rates")
            edition = "ENTERPRISE"
        edition_rates = v4.V4_EDITION_SLOT_HOUR_USD[edition]

        rate_key = k.COMMITMENT_PLAN_TO_RATE_KEY.get(pricing.commitment_plan or "FLEX", "payg")
        if edition not in v4.V4_EDITIONS_WITH_CAPACITY_COMMITMENTS and rate_key != "payg":
            caveats.append(
                f"{edition} has no true slot commitments — {pricing.commitment_plan} priced at PAYG"
            )
            rate_key = "payg"
        slot_hour_usd = edition_rates.get(rate_key)
        if slot_hour_usd is None:
            slot_hour_usd = edition_rates["payg"]

        slots_n, basis = _first_positive(
            ("commitment", pricing.commitment_slots),
            ("baseline", pricing.baseline_slots),
            ("max", pricing.max_slots),
        )
        if slots_n is None:
            slots_n, conf = 0, ConfidenceLevel.LOW
            caveats.append("no reservation slot figures — supply --reservation-config")
        else:
            conf = ConfidenceLevel.HIGH if basis in ("commitment", "baseline") else ConfidenceLevel.MEDIUM
        if not edition_known:
            conf = ConfidenceLevel.LOW

        monthly = slots_n * slot_hour_usd * k.HOURS_PER_MONTH
        note = (
            f"BigQuery {edition} slot-hour @ ${slot_hour_usd} ({rate_key}, "
            f"{v4.V4_PRICING_REGION}) × {slots_n:g} slots × "
            f"{k.HOURS_PER_MONTH:g}h (verified {v4.V4_CONFIRMED_DATE})"
        )
        if caveats:
            note += " ⚠️ " + "; ".join(caveats)
        line = CostLine(
            label=f"BigQuery capacity ({edition})", monthly=round(monthly, 4),
            monthly_low=None, monthly_high=None, confidence=conf, source_note=note,
        )
        return round(monthly, 4), [line]

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

        # Core justification: volume, crossover, pattern
        justification = (
            f"Your workload scans {monthly_scanned_tb:.2f} TB/month ({daily_scanned_tb:.2f} TB/day, "
            f"{qpd:,.0f} queries/day) — far below the ~{crossover:.2f} TB/day crossover where "
            f"Redshift Serverless becomes cheaper. The crossover assumes Redshift's minimum posture "
            f"(4 RPU, ~8 active hours/day); always-on workloads break even nearer 8 TB/day. "
            f"Note: if the workgroup auto-scales above 4 RPUs it will not scale back down "
            f"automatically — the 4-RPU floor requires manual reset, so sustained bursts "
            f"erode this posture. "
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
            justification += (
                f"Revisit if scan volume approaches {crossover:.2f} TB/day, sustained concurrency "
                f"approaches your account's Athena DML quota (default 100 in ap-southeast-2, "
                f"adjustable), or sub-3-second latency SLAs emerge."
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
        peak_conc = profile.peak_concurrent_queries
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
        peak_conc = profile.peak_concurrent_queries

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


def _first_positive(*pairs):
    for label, value in pairs:
        if value is not None:
            coerced = _safe_num(value)
            if coerced > 0:
                return coerced, label
    return None, None


def _safe_num(value) -> float:
    try:
        n = float(value)
    except (ValueError, TypeError):
        return 0.0
    return n if n >= 0 else 0.0


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
    """Get the effective monthly value from a CostLine (point or range midpoint)."""
    if line.monthly is not None:
        return line.monthly
    low = line.monthly_low or 0
    high = line.monthly_high or low
    return (low + high) / 2


def _line_low(line: CostLine) -> float:
    return line.monthly if line.monthly is not None else (line.monthly_low or 0)


def _line_high(line: CostLine) -> float:
    return line.monthly if line.monthly is not None else (line.monthly_high or 0)


def _breakeven(onetime: float, monthly_delta: float) -> float:
    if monthly_delta <= 0:
        return k.BREAKEVEN_NEVER
    return onetime / monthly_delta


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
