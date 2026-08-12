# Feature: bq-assess-lakehouse, issue 5.3: CostEstimator (R18)
"""Unit tests for the lakehouse Cost Estimator (BigQuery vs AWS run-rate comparison).

Drives ``CostEstimator.estimate()`` (R18): AWS = S3 Tables storage (V2) + Serverless RPU
compute (V1) via the slot→RPU bridge (V3), no node sizing (R18.6); BQ priced per the detected
model (R16.4); ``--bigquery-monthly-cost`` override wins (R18.2); compute is a point at MED/HIGH
with slots else a LOW-confidence range (R18.3/R18.4); every CostLine carries a dated source_note
and every constant is overridable (R18.7).

Inputs unique to this signature (PricingDetection, SlotUtilization) have no conftest strategy
yet (owed by 5.1/5.2 — they were TDD'd with ad-hoc inputs); built here as local helpers.
"""

from __future__ import annotations

import inspect

import pytest

from bq_assess.core import pricing_constants as v4
from bq_assess.engine.redshift import cost_constants as k
from bq_assess.engine.redshift.cost import CostEstimator
from bq_assess.models import (
    BQPricingModel,
    ConfidenceLevel,
    CostComparison,
    EntityPopulation,
    EntityType,
    PricingDetection,
    SlotUtilization,
)

# --- input helpers (local; PricingDetection/SlotUtilization have no conftest strategy yet) ----


def ondemand_pricing() -> PricingDetection:
    return PricingDetection(
        model=BQPricingModel.ON_DEMAND, confidence=ConfidenceLevel.HIGH,
        source_note="on-demand (test)",
    )


def capacity_pricing(edition="ENTERPRISE", baseline_slots=100, max_slots=200,
                     commitment_slots=100, commitment_plan="ANNUAL") -> PricingDetection:
    return PricingDetection(
        model=BQPricingModel.CAPACITY, confidence=ConfidenceLevel.HIGH,
        source_note="capacity (test)", edition=edition, baseline_slots=baseline_slots,
        max_slots=max_slots, commitment_slots=commitment_slots, commitment_plan=commitment_plan,
    )


def slot_util(*, total_slot_ms=730 * 3_600_000, days_sampled=14, avg=1.0, peak=2.0,
              active_fraction=0.5) -> SlotUtilization:
    return SlotUtilization(
        avg_slots=avg, p50_slots=avg, p99_slots=peak, peak_slots=peak,
        active_hour_fraction=active_fraction, total_slot_ms=total_slot_ms,
        days_sampled=days_sampled,
    )


def entity(size_gb=100.0, name="ds.t1"):
    """A minimal EntityReport carrying the size the cost model reads."""
    from bq_assess.models import EntityReport
    return EntityReport(
        full_name=name, entity_type=EntityType.TABLE, population=EntityPopulation.TABLE,
        rows=1000, size_gb=size_gb, depends_on=[], effort=None, conversion=None,
        load_sync_dml=None, complexity=None, rewrite_guidance=[], placement=None,
    )


def _estimate(entities=None, *, pricing=None, slots=None, override=None, effort=10.0):
    return CostEstimator(skip_live_pricing=True).estimate(
        entities if entities is not None else [entity()],
        pricing or ondemand_pricing(),
        slots,
        override,
        effort,
    )


# --- Phase A: signature + structure ---------------------------------------------------

def test_estimate_signature_matches_contract() -> None:
    """estimate() params match design.md exactly; entities/effort_total unannotated.

    ``location`` (keyword-only, default None) was added by the 2026-07-02 region-cascade
    amendment — see SCRUM_NOTES § Signature amendment 2026-07-02. Default None preserves
    the pre-amendment behavior for existing positional callers.
    ``storage_basis`` (keyword-only, default "assumed") was added by the 2026-07-08
    physical-bytes storage sizing feature (Task 4).
    ``as_of`` (keyword-only, default None) was added by the 2026-07-23 long-term
    storage split — anchors the 90-day idle window to bundle collection time;
    None falls back to now() for in-process (assess) runs.
    """
    sig = inspect.signature(CostEstimator.estimate)
    params = list(sig.parameters)
    assert params == ["self", "entities", "pricing", "slots", "bq_monthly_override",
                      "effort_total", "location", "storage_basis", "as_of", "egress_gib"]
    assert sig.parameters["location"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["location"].default is None
    assert sig.parameters["storage_basis"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["storage_basis"].default == "assumed"
    assert sig.parameters["as_of"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["as_of"].default is None
    assert sig.parameters["egress_gib"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["egress_gib"].default is None


def test_returns_costcomparison() -> None:
    """estimate() returns a fully-populated CostComparison."""
    result = _estimate()
    assert isinstance(result, CostComparison)
    assert isinstance(result.bq_pricing_model, BQPricingModel)
    assert isinstance(result.compute_confidence, ConfidenceLevel)


def _aws(line_label, result):
    return next(line for line in result.aws_lines if line_label in line.label)


def test_aws_has_storage_and_compute_line() -> None:
    """AWS run-rate = storage line + compute line, both always present (R18.1)."""
    r = _estimate(slots=slot_util())
    labels = " ".join(line.label for line in r.aws_lines)
    assert "storage" in labels.lower()
    assert "compute" in labels.lower()
    assert len(r.aws_lines) >= 2


def test_aws_total_sums_all_lines_point_case() -> None:
    """With slots (point compute), aws bounds equal the sum of all line points (R18.1)."""
    r = _estimate(slots=slot_util())
    total = sum(line.monthly for line in r.aws_lines)
    assert r.aws_monthly_low == r.aws_monthly_high          # point case: bounds equal
    assert round(r.aws_monthly_low, 4) == round(total, 4)


# --- Phase B: compute switch (slots present → point MED/HIGH; absent → range LOW) -----

def test_compute_point_when_slots_present() -> None:
    """R18.3: with slots the compute line is a point at MED/HIGH, never LOW."""
    r = _estimate(slots=slot_util(days_sampled=14))
    compute = _aws("compute", r)
    assert compute.monthly is not None
    assert compute.monthly_low is None and compute.monthly_high is None
    assert compute.confidence in (ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH)
    assert r.compute_confidence is compute.confidence


def test_compute_confidence_capped_at_medium_with_slots() -> None:
    """Serverless compute capped at MEDIUM regardless of sample days (slot→RPU ratio
    is an unverified assumption); without slots → LOW."""
    assert _estimate(slots=slot_util(days_sampled=7)).compute_confidence is ConfidenceLevel.MEDIUM
    assert _estimate(slots=slot_util(days_sampled=3)).compute_confidence is ConfidenceLevel.MEDIUM


def test_compute_range_when_slots_absent() -> None:
    """R18.4: no slots → compute is a range (low < high) at LOW, never a point."""
    r = _estimate(slots=None)
    compute = _aws("compute", r)
    assert compute.monthly is None                          # strictly not a point
    assert compute.monthly_low < compute.monthly_high
    assert compute.confidence is ConfidenceLevel.LOW
    assert r.compute_confidence is ConfidenceLevel.LOW


def test_aws_bounds_spread_when_compute_is_range() -> None:
    """The range branch must spread aws bounds via the compute low/high fallback (audit G3)."""
    r = _estimate(slots=None)
    assert r.aws_monthly_low < r.aws_monthly_high


def test_zero_slot_ms_workload_yields_range_not_confident_zero() -> None:
    """A SlotUtilization with total_slot_ms==0 carries no compute signal → LOW range, not a
    confident $0 point (review #5/#12). All-cached/metadata jobs produce this."""
    r = _estimate(slots=slot_util(total_slot_ms=0, days_sampled=14))
    compute = _aws("compute", r)
    assert compute.monthly is None                 # NOT a confident $0.00 point
    assert compute.monthly_low < compute.monthly_high
    assert r.compute_confidence is ConfidenceLevel.LOW


def test_compute_range_labels_estimate() -> None:
    """R18.4 / R20.6: the range source_note marks it an estimate + suggests query logs."""
    note = _aws("compute", _estimate(slots=None)).source_note.lower()
    assert "estimate" in note
    assert "query log" in note


def test_compute_depends_on_slot_ms_not_active_day_count() -> None:
    """Compute tracks total_slot_ms over the lookback window, NOT days_sampled (active days).
    Scaling by active-day count over-extrapolates a sparse workload (review #2)."""
    # active_fraction kept below the 4-RPU floor's bind point — this test pins the
    # slot-derived path (the floor has its own tests below).
    sparse = _estimate(slots=slot_util(total_slot_ms=365 * 3_600_000, days_sampled=3,
                                       active_fraction=0.01))
    dense = _estimate(slots=slot_util(total_slot_ms=365 * 3_600_000, days_sampled=30,
                                      active_fraction=0.01))
    # Same slot-ms over the same window → same monthly compute regardless of how many distinct
    # days saw activity (the old code inflated `sparse` 10× by dividing by days_sampled).
    assert _aws("compute", sparse).monthly == _aws("compute", dense).monthly
    # And the figure is the honest slot-hours × V3 ratio × rate (rounded to 4 dp).
    assert _aws("compute", dense).monthly == round(
        365 * k.V3_SLOT_TO_RPU_RATIO * k.V1_RPU_HOUR_USD, 4)


# --- Phase C: BigQuery pricing paths --------------------------------------------------

def test_ondemand_prices_storage_plus_bytes_scanned() -> None:
    """R18.2a: on-demand BQ = storage line + bytes-scanned line; scanned cites $/TiB."""
    r = _estimate(pricing=ondemand_pricing(), slots=slot_util())
    labels = [line.label.lower() for line in r.bigquery_breakdown]
    assert any("storage" in x for x in labels)
    assert any("scan" in x for x in labels)
    scan_line = next(line for line in r.bigquery_breakdown if "scan" in line.label.lower())
    assert str(v4.V4_ONDEMAND_USD_PER_TIB) in scan_line.source_note


def test_bq_breakdown_sums_to_bigquery_monthly() -> None:
    """The BQ breakdown lines explain (sum to) bigquery_monthly (audit G17)."""
    r = _estimate(pricing=ondemand_pricing(), slots=slot_util())
    total = sum(line.monthly for line in r.bigquery_breakdown)
    assert round(total, 4) == round(r.bigquery_monthly, 4)


def test_ondemand_proxy_fallback_mentions_override_flag() -> None:
    """No logs → LOW proxy estimate must tell the customer how to supply the real bill."""
    result = _estimate(pricing=ondemand_pricing(), slots=None)
    scan_line = next(ln for ln in result.bigquery_breakdown if "scanned" in ln.label.lower())
    assert scan_line.confidence is ConfidenceLevel.LOW
    assert "--bigquery-monthly-cost" in scan_line.source_note


def test_capacity_prices_from_reservation_figures() -> None:
    """R18.2b: capacity priced from edition × plan-rate × slots × 730 + storage; no on-demand scan."""
    pricing = capacity_pricing(edition="ENTERPRISE", commitment_slots=100, commitment_plan="ANNUAL")
    r = _estimate(pricing=pricing)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]   # ANNUAL → commit_1yr
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH  # default entity 100 GiB
    assert round(r.bigquery_monthly, 4) == round(100 * rate * k.HOURS_PER_MONTH + storage, 4)
    assert not any("scan" in line.label.lower() for line in r.bigquery_breakdown)


def test_capacity_plan_maps_to_rate_key() -> None:
    """commitment_plan vocabulary maps to V4 rate keys; FLEX/MONTHLY → payg (audit G7)."""
    ent = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
    for plan, key in [("ANNUAL", "commit_1yr"), ("THREE_YEAR", "commit_3yr"),
                      ("MONTHLY", "payg"), ("FLEX", "payg")]:
        r = _estimate(pricing=capacity_pricing(baseline_slots=10, commitment_slots=10, commitment_plan=plan))
        assert round(r.bigquery_monthly, 4) == round(10 * ent[key] * k.HOURS_PER_MONTH + storage, 4)


def test_capacity_baseline_is_billing_basis() -> None:
    """Baseline slots are what's always allocated and billed — the billing basis."""
    ent_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
    # baseline present → used as billing basis
    r1 = _estimate(pricing=capacity_pricing(commitment_slots=50, baseline_slots=10, max_slots=200))
    assert round(r1.bigquery_monthly, 4) == round(10 * ent_rate * k.HOURS_PER_MONTH + storage, 4)
    # baseline None → falls back to commitment_slots
    r2 = _estimate(pricing=capacity_pricing(commitment_slots=50, baseline_slots=None, max_slots=200))
    assert round(r2.bigquery_monthly, 4) == round(50 * ent_rate * k.HOURS_PER_MONTH + storage, 4)
    # baseline + commitment None, but autoscale present → zero baseline (only autoscale matters)
    p3 = capacity_pricing(commitment_slots=None, baseline_slots=None, max_slots=200)
    p3.autoscale_slot_seconds = 0  # Has reservation data (zero autoscale usage), so priceable
    p3.timeline_window_seconds = int(k.HOURS_PER_MONTH * 3600)
    r3 = _estimate(pricing=p3)
    assert round(r3.bigquery_monthly, 4) == round(0 * ent_rate * k.HOURS_PER_MONTH + storage, 4)


def test_commitment_slots_zero_falls_through_to_baseline() -> None:
    """commitment_slots=0 means 'no commitment purchased' — must NOT shadow a valid baseline."""
    ent_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
    r = _estimate(pricing=capacity_pricing(commitment_slots=0, baseline_slots=100, max_slots=200))
    assert round(r.bigquery_monthly, 4) == round(100 * ent_rate * k.HOURS_PER_MONTH + storage, 4)


def test_standard_edition_commitment_priced_at_payg_not_commit() -> None:
    """STANDARD has no true slot commitments (V4): a STANDARD+ANNUAL config is priced at PAYG,
    flagged, NOT at the commit_1yr rate at HIGH confidence (review #6/#11)."""
    pricing = capacity_pricing(edition="STANDARD", commitment_slots=100, commitment_plan="ANNUAL")
    r = _estimate(pricing=pricing)
    payg = v4.V4_EDITION_SLOT_HOUR_USD["STANDARD"]["payg"]
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
    assert round(r.bigquery_monthly, 4) == round(100 * payg * k.HOURS_PER_MONTH + storage, 4)
    # compute line is index 1 (storage is 0)
    compute_line = r.bigquery_breakdown[1]
    assert "no true slot commitments" in compute_line.source_note.lower() or "payg" in compute_line.source_note.lower()


def test_unknown_edition_priced_as_enterprise_fallback_low_conf() -> None:
    """An unrecognized edition is priced at ENTERPRISE rates as a labelled LOW-confidence
    fallback, not silently (review #9)."""
    pricing = capacity_pricing(edition="GALACTIC", commitment_slots=100, commitment_plan="ANNUAL")
    r = _estimate(pricing=pricing)
    assert r.bigquery_monthly > 0
    # compute line is index 1 (storage is 0)
    compute_line = r.bigquery_breakdown[1]
    assert compute_line.confidence is ConfidenceLevel.LOW
    assert "galactic" in compute_line.source_note.lower()


def test_zero_rate_override_not_replaced_by_payg(monkeypatch) -> None:
    """A 0.0 rate override is a legitimate value (credit/promo) — not swallowed by `or payg` (#7)."""
    rates = dict(v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"])
    rates["commit_1yr"] = 0.0
    monkeypatch.setitem(v4.V4_EDITION_SLOT_HOUR_USD, "ENTERPRISE", rates)
    r = _estimate(pricing=capacity_pricing(edition="ENTERPRISE", commitment_slots=100,
                                           commitment_plan="ANNUAL"))
    # Compute is $0 (0.0 rate), but storage still applies
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
    assert r.bigquery_monthly == storage


def test_malformed_capacity_config_does_not_raise() -> None:
    """A string commitment_slots degrades to 0 rather than crashing str×float (review #8/#16)."""
    pricing = capacity_pricing(commitment_slots="oops", baseline_slots=None, max_slots=None)
    r = _estimate(pricing=pricing)
    assert isinstance(r.bigquery_monthly, float)   # no TypeError


def test_capacity_never_falls_back_to_ondemand() -> None:
    """R16.4: a CAPACITY model with no figures is still capacity-priced, never on-demand."""
    pricing = capacity_pricing(commitment_slots=None, baseline_slots=None, max_slots=None)
    r = _estimate(pricing=pricing)
    assert r.bq_pricing_model is BQPricingModel.CAPACITY
    assert not any("scan" in line.label.lower() for line in r.bigquery_breakdown)
    assert not any(str(v4.V4_ONDEMAND_USD_PER_TIB) in line.source_note for line in r.bigquery_breakdown)


def test_bq_pricing_model_equals_detected_model() -> None:
    """R16.4 / R19.2: bq_pricing_model == pricing.model exactly, for all three models."""
    for model, pricing in [
        (BQPricingModel.ON_DEMAND, ondemand_pricing()),
        (BQPricingModel.CAPACITY, capacity_pricing()),
        (BQPricingModel.UNKNOWN, PricingDetection(
            model=BQPricingModel.UNKNOWN, confidence=ConfidenceLevel.LOW, source_note="?")),
    ]:
        assert _estimate(pricing=pricing).bq_pricing_model is model


def test_capacity_includes_storage_line() -> None:
    """Capacity customers also pay for storage — the cost line must appear."""
    pricing = capacity_pricing(commitment_slots=100)
    r = _estimate(entities=[entity(size_gb=1000.0)], pricing=pricing)
    storage_lines = [x for x in r.bigquery_breakdown if "storage" in x.label.lower()]
    assert len(storage_lines) == 1
    # _entity_bytes returns size_gb * 1024^3; _bq_storage_line divides by 1024^3 → gib = size_gb
    expected = round(1000.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH, 4)
    assert storage_lines[0].monthly == pytest.approx(expected, rel=0.01)


def test_capacity_standard_no_reservation_data_uses_slot_ms() -> None:
    """STANDARD with no reservation data estimates a modelled range (2026-08-10)."""
    pricing = capacity_pricing(
        edition="STANDARD", commitment_slots=None, baseline_slots=None, max_slots=None,
    )
    slots = slot_util(total_slot_ms=100 * 3_600_000, days_sampled=30, active_fraction=0.9)
    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing, slots=slots)
    compute_line = next(x for x in r.bigquery_breakdown if "capacity" in x.label.lower())
    # Now a range, not a point
    assert compute_line.monthly is None
    assert compute_line.monthly_low is not None
    assert compute_line.monthly_high is not None
    assert compute_line.monthly_high >= compute_line.monthly_low
    # Confidence capped at MEDIUM (slot→billing reconstruction)
    assert compute_line.confidence is ConfidenceLevel.MEDIUM


class TestStandardCapacityRange:
    """STANDARD capacity: slot_ms is a billing FLOOR, not the bill (2026-08-10).

    Google bills scaled capacity (50-slot steps, 1-min minimum), not consumed
    slot-ms — 'don't use the jobs information schema to match the billing'.
    """

    def _standard_no_reservation_data(self) -> PricingDetection:
        return PricingDetection(
            model=BQPricingModel.CAPACITY, confidence=ConfidenceLevel.MEDIUM,
            source_note="capacity (test)", edition="STANDARD",
            baseline_slots=None, max_slots=None, commitment_slots=None,
            commitment_plan=None,
        )

    def _compute_line(self, result):
        return next(ln for ln in result.bigquery_breakdown if "capacity" in ln.label.lower())

    def test_standard_estimates_a_range_not_a_point(self) -> None:
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(total_slot_ms=100 * 3_600_000, days_sampled=14,
                            avg=0.3, peak=40.0, active_fraction=0.2),
        )
        assert result.bq_cost_available is True
        line = self._compute_line(result)
        assert line.monthly is None
        assert line.monthly_low is not None and line.monthly_high is not None
        assert line.monthly_high >= line.monthly_low > 0

    def test_ceiling_reflects_50_slot_autoscale_floor(self) -> None:
        # bursty: avg 0.5 slots but only 10% active hours → busy concurrency 5,
        # billed >= 50 slots during busy hours → ceiling far above the floor
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(total_slot_ms=int(0.5 * 730 * 3_600_000), days_sampled=30,
                            avg=0.5, peak=20.0, active_fraction=0.1),
        )
        line = self._compute_line(result)
        assert line.monthly_high > line.monthly_low * 2

    def test_standard_confidence_never_high_without_timeline(self) -> None:
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(days_sampled=30),
        )
        line = self._compute_line(result)
        assert line.confidence is not ConfidenceLevel.HIGH

    def test_wide_range_adds_flag_guidance(self) -> None:
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(total_slot_ms=int(0.5 * 730 * 3_600_000), days_sampled=30,
                            avg=0.5, peak=20.0, active_fraction=0.05),
        )
        line = self._compute_line(result)
        assert line.monthly_high > 3 * line.monthly_low
        assert "--bigquery-monthly-cost" in line.source_note

    def test_range_always_discloses_burst_and_fluid_scaling_limits(self) -> None:
        # The bursty-workload + fluid-scaling caveat is always-on, not spread-gated:
        # the two-sided error is uncorrelated with the range's width (2026-08-10 review)
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(total_slot_ms=100 * 3_600_000, days_sampled=14,
                            avg=0.3, peak=40.0, active_fraction=0.2),
        )
        line = self._compute_line(result)
        assert "can exceed this range" in line.source_note
        assert "fluid-scaling" in line.source_note
        assert "--bigquery-monthly-cost" in line.source_note

    def test_range_basis_savings_floor_uses_bq_minimum(self) -> None:
        """monthly_delta_low is computed against the BQ measured minimum, not the
        modelled upper estimate — savings anchored to the upper figure alone
        flatter AWS one-sidedly (2026-08-11 MRI-1)."""
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(total_slot_ms=int(0.5 * 730 * 3_600_000), days_sampled=30,
                            avg=0.5, peak=20.0, active_fraction=0.1),
        )
        line = self._compute_line(result)
        assert result.bigquery_monthly_low is not None
        assert result.bigquery_monthly_low < result.bigquery_monthly
        # bigquery_monthly_low = storage points + the range line's low
        point_sum = sum(
            ln.monthly for ln in result.bigquery_breakdown
            if ln.monthly is not None
        )
        assert result.bigquery_monthly_low == pytest.approx(
            point_sum + line.monthly_low, rel=0.01
        )
        # and the committable delta is anchored to it
        assert result.monthly_delta_low == pytest.approx(
            result.bigquery_monthly_low - result.aws_monthly_high, rel=0.01
        )
        assert result.monthly_delta_high == pytest.approx(
            result.bigquery_monthly - result.aws_monthly_low, rel=0.01
        )

    def test_point_basis_has_no_bigquery_monthly_low(self) -> None:
        """Point-estimate bases (on-demand, reservation path) leave the range
        basis unset so the template renders a single hero figure."""
        result = _estimate(slots=slot_util())    # default on-demand pricing
        assert result.bigquery_monthly_low is None

    def test_headline_uses_ceiling(self) -> None:
        # conservative: understating BQ cost overstates nothing — the headline
        # (totals basis) is the reconstruction, the floor stays visible as the low
        result = _estimate(
            pricing=self._standard_no_reservation_data(),
            slots=slot_util(total_slot_ms=100 * 3_600_000, days_sampled=14,
                            avg=0.3, peak=40.0, active_fraction=0.2),
        )
        line = self._compute_line(result)
        assert line.headline == line.monthly_high

    def test_no_workload_data_still_prompts_not_prices(self) -> None:
        result = _estimate(pricing=self._standard_no_reservation_data(), slots=None)
        assert result.bq_cost_available is True     # storage still modelled
        line = self._compute_line(result)
        assert (line.monthly in (None, 0.0)) and not line.monthly_high
        assert "--bigquery-monthly-cost" in line.source_note

    def test_standard_with_timeline_data_uses_reservation_path(self) -> None:
        p = PricingDetection(
            model=BQPricingModel.CAPACITY, confidence=ConfidenceLevel.HIGH,
            source_note="capacity (test)", edition="STANDARD",
            baseline_slots=None, max_slots=None, commitment_slots=None,
            commitment_plan=None, autoscale_slot_seconds=360_000,
            timeline_window_seconds=604_800,
        )
        result = _estimate(pricing=p, slots=slot_util())
        line = next(ln for ln in result.bigquery_breakdown if "capacity" in ln.label.lower())
        assert "autoscale" in line.source_note          # reservation path, not from_slots
        assert line.monthly is not None                 # point, not range


def test_capacity_enterprise_no_reservation_data_is_unavailable() -> None:
    """ENTERPRISE with no reservation data → UNAVAILABLE (commitment type unknown)."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", commitment_slots=None, baseline_slots=None, max_slots=None,
    )
    slots = slot_util(total_slot_ms=100 * 3_600_000, days_sampled=30, active_fraction=0.9)
    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing, slots=slots)
    compute_line = next(x for x in r.bigquery_breakdown if "capacity" in x.label.lower())
    assert "UNAVAILABLE" in compute_line.label
    assert compute_line.confidence is ConfidenceLevel.LOW
    assert compute_line.monthly is None


def test_capacity_autoscale_adds_to_baseline() -> None:
    """Autoscale slot-seconds are priced at PAYG rate on top of baseline."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", commitment_slots=100, baseline_slots=100,
        commitment_plan="ANNUAL",
    )
    # 1000 autoscale slot-seconds over 86400 seconds (1 day)
    pricing.autoscale_slot_seconds = 1000
    pricing.timeline_window_seconds = 86400
    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    payg_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["payg"]
    baseline_monthly = 100 * rate * k.HOURS_PER_MONTH
    # autoscale: (1000 / 86400) * (730*3600) * payg / 3600
    autoscale_monthly = 1000 / 86400 * (k.HOURS_PER_MONTH * 3600) * payg_rate / 3600
    assert r.bigquery_monthly == pytest.approx(baseline_monthly + autoscale_monthly, rel=0.01)


def test_capacity_proration_usage_based() -> None:
    """Shared reservations prorate by this project's slot-ms share of total capacity."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", commitment_slots=100, baseline_slots=100,
        commitment_plan="ANNUAL",
    )
    # Reservation shared by 3 assignees, timeline window = 30 days
    pricing.assigned_count = 3
    pricing.assigned_projects = ["proj-a", "proj-b", "proj-c"]
    pricing.timeline_window_seconds = 30 * 86400
    pricing.autoscale_slot_seconds = 0

    # This project used 50% of the reservation's total allocated capacity
    # (above the 1/3 headcount floor, so the usage share governs).
    # Total reservation slot-ms = baseline × window_seconds × 1000
    # = 100 × 30×86400 × 1000 = 259_200_000_000
    project_slot_ms = int(100 * 30 * 86400 * 1000 * 0.50)
    slots = slot_util(total_slot_ms=project_slot_ms, days_sampled=30)

    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing, slots=slots)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    full_monthly = 100 * rate * k.HOURS_PER_MONTH
    expected = full_monthly * 0.50  # 50% share
    assert r.bigquery_monthly == pytest.approx(expected, rel=0.01)


def test_capacity_proration_floored_at_equal_headcount() -> None:
    """Usage share below 1/assigned_count is floored at equal headcount (2026-08-11).

    Consumed-slot-ms ÷ allocated-capacity shares sum to the reservation's
    utilization ratio, not 1.0 — a low-utilization reservation would attribute
    most of its (fully billed) cost to no project, understating BQ cost and
    overstating AWS savings. The equal-headcount floor keeps the idle-capacity
    cost carried.
    """
    pricing = capacity_pricing(
        edition="ENTERPRISE", commitment_slots=100, baseline_slots=100,
        commitment_plan="ANNUAL",
    )
    pricing.assigned_count = 4
    pricing.assigned_projects = ["a", "b", "c", "d"]
    pricing.timeline_window_seconds = 30 * 86400
    pricing.autoscale_slot_seconds = 0

    # Project consumed only 5% of allocated capacity — below the 25% headcount floor
    project_slot_ms = int(100 * 30 * 86400 * 1000 * 0.05)
    slots = slot_util(total_slot_ms=project_slot_ms, days_sampled=30)

    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing, slots=slots)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    full_monthly = 100 * rate * k.HOURS_PER_MONTH
    expected = full_monthly / 4  # floored at 1/assigned_count, not 5%
    assert r.bigquery_monthly == pytest.approx(expected, rel=0.01)
    compute_line = next(x for x in r.bigquery_breakdown if "capacity" in x.label.lower())
    assert "floored at equal headcount" in compute_line.source_note


def test_capacity_proration_fallback_equal_without_slots() -> None:
    """Without workload data, proration falls back to 1/assigned_count."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", commitment_slots=100, baseline_slots=100,
        commitment_plan="ANNUAL",
    )
    pricing.assigned_count = 4
    pricing.assigned_projects = ["a", "b", "c", "d"]
    pricing.timeline_window_seconds = 30 * 86400
    pricing.autoscale_slot_seconds = 0

    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing, slots=None)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    full_monthly = 100 * rate * k.HOURS_PER_MONTH
    expected = full_monthly / 4  # equal share fallback
    assert r.bigquery_monthly == pytest.approx(expected, rel=0.01)


def test_capacity_unavailable_when_unreadable() -> None:
    """reservation_readable=False → compute line shows UNAVAILABLE, 0 cost."""
    pricing = capacity_pricing(commitment_slots=100, baseline_slots=100)
    pricing.reservation_readable = False
    pricing.reservation_id = "admin-proj:us-central1.my-reservation"
    r = _estimate(entities=[entity(size_gb=100)], pricing=pricing)
    compute_line = next(x for x in r.bigquery_breakdown if "capacity" in x.label.lower())
    assert "UNAVAILABLE" in compute_line.label
    assert compute_line.monthly is None
    # Storage is still reported
    storage_lines = [x for x in r.bigquery_breakdown if "storage" in x.label.lower()]
    assert len(storage_lines) == 1


def test_capacity_baseline_used_even_with_slot_ms() -> None:
    """When reservation figures exist, slot-ms data does NOT override them."""
    pricing = capacity_pricing(commitment_slots=100, baseline_slots=100)
    slots = slot_util(total_slot_ms=9999 * 3_600_000)
    r = _estimate(pricing=pricing, slots=slots)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    storage = 100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
    expected = round(100 * rate * k.HOURS_PER_MONTH + storage, 4)
    assert r.bigquery_monthly == pytest.approx(expected, rel=0.001)


def test_capacity_blended_rate_mixed_commitments() -> None:
    """Mixed commitments (ANNUAL + FLEX) price each tier at its own rate, not one rate for all."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", baseline_slots=700, commitment_slots=700,
        commitment_plan="ANNUAL",
    )
    # 500 ANNUAL + 200 FLEX — each billed at its own rate
    pricing.commitments = [
        {"slot_count": 500, "plan": "ANNUAL", "edition": "ENTERPRISE"},
        {"slot_count": 200, "plan": "FLEX", "edition": "ENTERPRISE"},
    ]
    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing)
    annual_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    payg_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["payg"]
    expected = (500 * annual_rate + 200 * payg_rate) * k.HOURS_PER_MONTH
    assert r.bigquery_monthly == pytest.approx(expected, rel=0.001)


def test_capacity_blended_rate_excess_at_payg() -> None:
    """Baseline exceeding total commitments charges excess at PAYG."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", baseline_slots=300, commitment_slots=200,
        commitment_plan="ANNUAL",
    )
    pricing.commitments = [
        {"slot_count": 200, "plan": "ANNUAL", "edition": "ENTERPRISE"},
    ]
    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing)
    annual_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    payg_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["payg"]
    expected = (200 * annual_rate + 100 * payg_rate) * k.HOURS_PER_MONTH
    assert r.bigquery_monthly == pytest.approx(expected, rel=0.001)


def test_capacity_autoscale_uses_calendar_window() -> None:
    """Autoscale extrapolation uses the calendar window (lookback_days × 86400),
    not the sparse per_second_details count."""
    pricing = capacity_pricing(
        edition="ENTERPRISE", baseline_slots=100, commitment_slots=100,
        commitment_plan="ANNUAL",
    )
    # 10000 autoscale slot-seconds over 7 days (calendar window = 7*86400 = 604800s)
    pricing.autoscale_slot_seconds = 10000
    pricing.timeline_window_seconds = 7 * 86400  # calendar-based, not COUNT(*)
    r = _estimate(entities=[entity(size_gb=0)], pricing=pricing)
    rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"]
    payg_rate = v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["payg"]
    baseline_monthly = 100 * rate * k.HOURS_PER_MONTH
    monthly_seconds = k.HOURS_PER_MONTH * 3600
    autoscale_monthly = 10000 / (7 * 86400) * monthly_seconds * payg_rate / 3600
    assert r.bigquery_monthly == pytest.approx(baseline_monthly + autoscale_monthly, rel=0.01)


def test_override_takes_precedence() -> None:
    """R18.2c: --bigquery-monthly-cost wins regardless of model."""
    r = _estimate(pricing=capacity_pricing(), override=4242.0)
    assert r.bigquery_monthly == 4242.0


# --- Phase D: deltas / annual / break-even (R18.5, bound cross) -----------------------

def test_delta_and_annual_identities() -> None:
    """R18.5 / P23: bound-crossed delta + annual = delta × 12."""
    r = _estimate(slots=slot_util())
    assert r.monthly_delta_low == r.bigquery_monthly - r.aws_monthly_high
    assert r.monthly_delta_high == r.bigquery_monthly - r.aws_monthly_low
    assert r.annual_savings_low == r.monthly_delta_low * 12
    assert r.annual_savings_high == r.monthly_delta_high * 12


def test_breakeven_from_effort_not_table_count() -> None:
    """R18.5 / R9.2: migration_onetime tracks aggregate Effort, not entity count."""
    low_effort = _estimate(effort=10.0)
    high_effort = _estimate(effort=100.0)
    assert high_effort.migration_onetime > low_effort.migration_onetime
    # Vary entity count, hold effort → one-time cost unchanged.
    few = _estimate([entity()], effort=50.0)
    many = _estimate([entity(name=f"ds.t{i}") for i in range(50)], effort=50.0)
    assert few.migration_onetime == many.migration_onetime


def test_breakeven_never_when_no_savings() -> None:
    """D6: AWS ≥ BQ (delta ≤ 0) → break-even = BREAKEVEN_NEVER (JSON-safe sentinel)."""
    # Make BQ tiny so AWS dwarfs it → negative delta.
    r = _estimate(slots=None, override=0.0)
    assert r.monthly_delta_high <= 0
    assert r.breakeven_months_low == k.BREAKEVEN_NEVER
    assert r.breakeven_months_high == k.BREAKEVEN_NEVER


def test_breakeven_mixed_when_bounds_straddle_zero() -> None:
    """When the savings range straddles zero, low break-even = BREAKEVEN_NEVER, high is finite."""
    # Tune override so bigquery sits between aws_low and aws_high.
    probe = _estimate(slots=None)
    mid = (probe.aws_monthly_low + probe.aws_monthly_high) / 2
    r = _estimate(slots=None, override=mid)
    assert r.monthly_delta_low < 0 < r.monthly_delta_high
    assert r.breakeven_months_low == k.BREAKEVEN_NEVER
    assert 0 < r.breakeven_months_high < k.BREAKEVEN_NEVER


def test_breakeven_never_is_json_serializable() -> None:
    """BREAKEVEN_NEVER must be finite so it serializes to JSON (RFC 7159).
    Regression guard: math.inf would pass equality tests but crash json.dumps."""
    import json
    import math
    r = _estimate(slots=None, override=0.0)
    assert r.breakeven_months_low == k.BREAKEVEN_NEVER
    assert math.isfinite(r.breakeven_months_low)
    assert math.isfinite(r.breakeven_months_high)
    json.dumps([r.breakeven_months_low, r.breakeven_months_high])  # ValueError if inf/nan


def test_negative_savings_not_clamped() -> None:
    """Negative deltas/annual are reported as negative, not clamped to 0 (audit G13)."""
    r = _estimate(slots=None, override=0.0)
    assert r.monthly_delta_low < 0
    assert r.annual_savings_low < 0


def test_zero_entities_no_crash() -> None:
    """Empty project → zero storage, both AWS lines still present, no divide-by-zero (audit G9)."""
    r = _estimate([], slots=None)
    assert isinstance(r, CostComparison)
    assert len(r.aws_lines) >= 2


# --- Phase E: decoupling & no node sizing (R18.6) -------------------------------------

def test_compute_not_sized_by_stored_bytes() -> None:
    """R18.6: compute is a function of slots only — identical when only storage scales."""
    small = _estimate([entity(size_gb=1.0)], slots=slot_util())
    large = _estimate([entity(size_gb=10_000.0)], slots=slot_util())
    assert _aws("compute", small).monthly == _aws("compute", large).monthly
    assert _aws("storage", small).monthly < _aws("storage", large).monthly   # storage does scale


def test_no_node_strings_anywhere() -> None:
    """R18.6: no node/RA3/provisioned/advisor leakage in any label or source_note."""
    for slots in (slot_util(), None):
        for pricing in (ondemand_pricing(), capacity_pricing()):
            r = _estimate(pricing=pricing, slots=slots)
            blob = " ".join(
                line.label + " " + line.source_note
                for line in r.aws_lines + r.bigquery_breakdown
            ).lower()
            for banned in ("node", "ra3", "xlplus", "4xlarge", "16xlarge", "provisioned",
                           "deploymentadvisor", "redshift_type"):
                assert banned not in blob


def test_costcomparison_has_no_node_fields() -> None:
    """R18.6: the dataclass exposes no node/type/advisor field (regression vs legacy)."""
    import dataclasses
    names = {f.name for f in dataclasses.fields(CostComparison)}
    for banned in ("node", "node_type", "redshift_type", "deployment", "advisor"):
        assert banned not in names


# --- Phase F: provenance + overridability (R18.7) -------------------------------------

def test_every_costline_has_dated_source_note() -> None:
    """R18.7: every line carries a non-empty source_note containing a date token."""
    for slots in (slot_util(), None):
        for pricing in (ondemand_pricing(), capacity_pricing()):
            r = _estimate(pricing=pricing, slots=slots)
            for line in r.aws_lines + r.bigquery_breakdown:
                assert line.source_note
                assert "2026-" in line.source_note          # V1/V2 (2026-06-15) or V4 (2026-06-11)


def test_overriding_v1_changes_compute(monkeypatch) -> None:
    """R18.7: V1 RPU rate is overridable, not hardcoded — compute line moves."""
    base = _aws("compute", _estimate(slots=slot_util())).monthly
    monkeypatch.setattr(k, "V1_RPU_HOUR_USD", k.V1_RPU_HOUR_USD * 2)
    assert _aws("compute", _estimate(slots=slot_util())).monthly > base


def test_overriding_v3_ratio_changes_compute(monkeypatch) -> None:
    """R18.7 / D4: the V3 slot→RPU ratio is the one tunable; compute scales with it.

    active_fraction pinned near zero so the 4-RPU floor cannot bind — this test
    exercises the slot-derived path only."""
    base = _aws("compute", _estimate(slots=slot_util(active_fraction=0.001))).monthly
    monkeypatch.setattr(k, "V3_SLOT_TO_RPU_RATIO", k.V3_SLOT_TO_RPU_RATIO * 3)
    assert _aws("compute", _estimate(slots=slot_util(active_fraction=0.001))).monthly == pytest.approx(base * 3, rel=1e-3)


def test_overriding_v2_changes_storage(monkeypatch) -> None:
    """R18.7: V2 S3 Tables storage rate is overridable (audit G11)."""
    base = _aws("storage", _estimate()).monthly
    monkeypatch.setattr(k, "V2_S3_TABLES_USD_PER_GB_MONTH_TIER1", k.V2_S3_TABLES_USD_PER_GB_MONTH_TIER1 * 2)
    assert _aws("storage", _estimate()).monthly > base


def test_overriding_v4_changes_bq(monkeypatch) -> None:
    """R18.7: V4 BQ on-demand rate is overridable — BQ scanned line moves (audit G11)."""
    # Large entity so monthly scan exceeds the 1 TiB free tier and the rate actually bites.
    big = [entity(size_gb=500_000.0)]
    base = _estimate(big, pricing=ondemand_pricing(), slots=slot_util()).bigquery_monthly
    monkeypatch.setattr(v4, "V4_ONDEMAND_USD_PER_TIB", v4.V4_ONDEMAND_USD_PER_TIB * 5)
    assert _estimate(big, pricing=ondemand_pricing(), slots=slot_util()).bigquery_monthly > base


def test_v3_source_note_labels_assumption() -> None:
    """R18.7 / R20.6: the compute source_note flags V3 as a LOW-confidence assumption."""
    note = _aws("compute", _estimate(slots=slot_util())).source_note.lower()
    assert "assumption" in note


# --- Multi-scenario fixes: storage basis + money formatting ---------------------------
# These cover two bugs found reviewing the multi-scenario engine against a live run:
#   1. provisioned scenarios billed RMS storage instead of the shared S3 Tables basis
#   2. sub-dollar totals rendered as "$0/month" in the recommendation prose
from bq_assess.engine.redshift.cost import _fmt_usd


def _scenarios(result):
    return result.aws_scenarios


def test_provisioned_storage_uses_s3_tables_not_rms() -> None:
    """All scenarios — serverless AND provisioned — share the S3 Tables storage basis.

    Data lives in S3 Tables (decoupled lakehouse) and is queried via external tables, so the
    storage line must NOT change with the query engine. Provisioned must not bill RMS.
    """
    result = _estimate(slots=slot_util())
    prov = [s for s in _scenarios(result) if s.category.startswith("PROVISIONED")]
    assert prov, "expected provisioned scenarios when slot data is present"
    for s in prov:
        storage_lines = [ln for ln in s.lines if "storage" in ln.label.lower()]
        assert storage_lines, f"{s.label} has no storage line"
        for ln in storage_lines:
            assert "S3 Tables" in ln.label
            blob = (ln.label + " " + ln.source_note).lower()
            assert "managed storage" not in blob
            assert "ra3" not in blob


def test_storage_value_identical_across_all_scenarios() -> None:
    """Storage cost must not vary by engine — same bytes, same S3 Tables basis everywhere."""
    result = _estimate(slots=slot_util())
    storage_vals = []
    for s in _scenarios(result):
        ln = next(x for x in s.lines if "storage" in x.label.lower())
        storage_vals.append(round(ln.monthly, 6))
    assert len(set(storage_vals)) == 1, f"storage differs across scenarios: {storage_vals}"


def test_below_breakeven_reservations_carry_demotion_reason() -> None:
    """A sparse workload (active fraction below the reservation break-even) gets its
    Serverless Reserved scenarios demoted with a not_recommended_reason — the 24/7
    committed floor is correct but reads as 'tool broken' next to the recommended
    option (2026-07-14 Montu MRI-8). The reason names both fractions."""
    result = _estimate(slots=slot_util(active_fraction=0.05))
    reserved = [s for s in _scenarios(result) if s.category.startswith("SERVERLESS_") and s.category != "SERVERLESS"]
    assert reserved, "reservation scenarios must be evaluated when workload data exists"
    for s in reserved:
        assert s.not_recommended_reason, f"{s.label} must be demoted at 5% active fraction"
        assert "5%" in s.not_recommended_reason
        assert not s.is_recommended


def test_above_breakeven_reservations_are_not_demoted() -> None:
    """A near-24/7 workload with usage ABOVE the commitment floor exceeds every
    reservation's break-even — no demotion; the justification says the reservation
    pays for itself. At 0.15 ratio, need avg=200 slots (30 RPUs → committed 32) so
    that on-demand RPU-hours exceed the 24/7 committed cost."""
    result = _estimate(slots=slot_util(
        total_slot_ms=200 * 730 * 3_600_000, avg=200.0, peak=250.0, active_fraction=0.99,
    ))
    reserved = [s for s in _scenarios(result) if s.category.startswith("SERVERLESS_") and s.category != "SERVERLESS"]
    assert reserved
    for s in reserved:
        assert s.not_recommended_reason == ""


def test_reservation_dominated_by_ondemand_is_demoted_despite_high_active_fraction() -> None:
    """The Montu cdp-prod shape: active 97% of hours but avg usage far below the 8-RPU
    commitment floor — the reservation costs ~34× on-demand yet the fraction test alone
    says 'pays for itself'. Dominance check: a reservation pricier than the on-demand
    scenario is always demoted, and its justification must not contradict the badge."""
    result = _estimate(slots=slot_util(
        total_slot_ms=int(7.6 * 730 * 3_600_000), avg=7.6, peak=845.0, active_fraction=0.97,
    ))
    od = next(s for s in _scenarios(result) if s.category == "SERVERLESS")
    reserved = [s for s in _scenarios(result) if s.category.startswith("SERVERLESS_") and s.category != "SERVERLESS"]
    assert reserved
    for s in reserved:
        assert s.monthly_total > od.monthly_total  # precondition: the pathological gap
        assert s.not_recommended_reason, f"{s.label} must be demoted when OD is cheaper"
        assert "pays for itself" not in s.justification


def test_recommended_scenario_never_carries_demotion_reason() -> None:
    """is_recommended and not_recommended_reason are mutually exclusive on every
    scenario — a demoted-but-chosen option would render a contradictory report."""
    for frac in (0.05, 0.5, 0.99):
        result = _estimate(slots=slot_util(active_fraction=frac))
        for s in _scenarios(result):
            assert not (s.is_recommended and s.not_recommended_reason), (
                f"{s.label} at active_fraction={frac} is both recommended and demoted"
            )


def test_estimate_basis_medium_with_billed_slots() -> None:
    """estimate_basis_level = MEDIUM when slots + billed bytes present (ratio caps it)."""
    slots = slot_util(days_sampled=14)
    slots.has_billed_bytes = True
    slots.total_bytes_billed = 1_000_000
    r = _estimate(slots=slots)
    assert r.estimate_basis_level is ConfidenceLevel.MEDIUM
    assert "billed bytes" in r.estimate_basis


def test_key_uncertainties_name_slot_rpu_bridge_once() -> None:
    """The slot→RPU caveat has exactly one home: key_uncertainties, not the basis
    sentence (2026-07-16 restructure — it was previously worded in two places)."""
    slots = slot_util(days_sampled=14)
    slots.has_billed_bytes = True
    r = _estimate(slots=slots)
    joined = " ".join(r.key_uncertainties)
    assert "SYS_SERVERLESS_USAGE" in joined
    assert "pilot workload" in joined
    assert "assumption" not in r.estimate_basis.lower()
    assert "RPU" not in r.estimate_basis


def test_key_uncertainties_flag_thin_sample_window() -> None:
    """A window under the measured threshold surfaces as a thin-sample uncertainty."""
    r = _estimate(slots=slot_util(days_sampled=3))
    assert any("3 active day(s)" in n for n in r.key_uncertainties)
    r14 = _estimate(slots=slot_util(days_sampled=14))
    assert not any("thin window" in n for n in r14.key_uncertainties)


def test_key_uncertainties_name_dominant_uncertainty_without_slots() -> None:
    """No slot data → the card must name the range estimate itself as the largest
    uncertainty (2026-07-16 audit MRI-2 — omitting it understates by construction)."""
    for slots in (None, slot_util(total_slot_ms=0, days_sampled=14)):
        r = _estimate(slots=slots)
        assert any("largest uncertainty" in n for n in r.key_uncertainties)
        assert any("rough range" in n for n in r.key_uncertainties)


def test_pricing_notes_suppress_bq_rate_when_operator_supplied() -> None:
    """--bigquery-monthly-cost → no rate-table assertion for a hand-entered number."""
    r = _estimate(slots=slot_util(), override=5000.0)
    joined = " ".join(r.pricing_notes)
    assert "operator-supplied" in joined
    assert "/TiB" not in joined
    r_normal = _estimate(slots=slot_util())
    assert any("/TiB" in n for n in r_normal.pricing_notes)


def test_estimate_basis_low_without_slots() -> None:
    """estimate_basis_level = LOW when no slot data (everything falls to range estimates)."""
    r = _estimate(slots=None)
    assert r.estimate_basis_level is ConfidenceLevel.LOW


def test_estimate_basis_mentions_window_days() -> None:
    """The basis text names the observation window length (lookback_days, not days_sampled)."""
    slots = slot_util(days_sampled=14)
    slots.has_billed_bytes = True
    r = _estimate(slots=slots)
    assert "30 days" in r.estimate_basis


def test_fmt_usd_does_not_round_subdollar_to_zero() -> None:
    """Sub-dollar amounts keep cents; $1+ are comma-grouped whole dollars."""
    assert _fmt_usd(0.1151) == "$0.12"
    assert _fmt_usd(0.0) == "$0.00"
    assert _fmt_usd(0.9) == "$0.90"
    assert _fmt_usd(1.0) == "$1"
    assert _fmt_usd(893.23) == "$893"
    assert _fmt_usd(43210.0) == "$43,210"
    assert _fmt_usd(-0.10) == "-$0.10"


def test_recommendation_prose_never_says_zero_dollars_for_real_cost() -> None:
    """A sub-dollar serverless workload must not be described as '$0/month'."""
    result = _estimate(
        [entity(size_gb=0.03)],
        slots=slot_util(total_slot_ms=7_000_000, days_sampled=30, avg=0.003, peak=0.03),
    )
    rec = result.recommendation
    assert rec is not None
    assert "$0/month" not in rec.reasoning


# --- Region cascade (2026-07-02): price both clouds in the Source's geography ----------
# Root cause of the Montu underestimate: an australia-southeast1 Source was priced at US
# multi-region rates ($6.25/TiB vs Sydney's $8.125). These tests pin the cascade.

@pytest.fixture()
def _restore_region_constants():
    """Snapshot/restore the module constants the region cascade mutates."""
    v4_snap = {n: getattr(v4, n) for n in (
        "V4_ONDEMAND_USD_PER_TIB", "V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH",
        "V4_STORAGE_LONGTERM_LOGICAL_USD_PER_GIB_MONTH",
        "V4_STORAGE_ACTIVE_PHYSICAL_USD_PER_GIB_MONTH",
        "V4_STORAGE_LONGTERM_PHYSICAL_USD_PER_GIB_MONTH",
        "V4_EDITION_SLOT_HOUR_USD", "V4_EDITION_RESOURCE_CUD_SLOT_HOUR_USD",
        "V4_PRICING_REGION", "V4_REGION_SCOPE",
    )}
    k_snap = {n: getattr(k, n) for n in (
        "V1_RPU_HOUR_USD", "V1_SERVERLESS_1YR_ALL_UPFRONT_RPU_HOUR_USD",
        "V1_SERVERLESS_1YR_NO_UPFRONT_RPU_HOUR_USD", "V1_SERVERLESS_3YR_RPU_HOUR_USD",
        "V2_S3_TABLES_USD_PER_GB_MONTH_TIER1", "V2_S3_TABLES_USD_PER_GB_MONTH_TIER2",
        "V2_S3_TABLES_USD_PER_GB_MONTH_TIER3", "V6_MANAGED_STORAGE_USD_PER_GB_MONTH",
        "AWS_PRICING_REGION", "AWS_REGION_SCOPE",
    )}
    import copy
    node_snap = (copy.deepcopy(k.V7_RG_NODE_TYPES), copy.deepcopy(k.V6_RA3_NODE_TYPES))
    yield
    for n, val in v4_snap.items():
        setattr(v4, n, val)
    for n, val in k_snap.items():
        setattr(k, n, val)
    k.V7_RG_NODE_TYPES.update(node_snap[0])
    k.V6_RA3_NODE_TYPES.update(node_snap[1])


def _estimate_at(location, **kwargs):
    return CostEstimator(skip_live_pricing=True).estimate(
        kwargs.pop("entities", [entity()]),
        kwargs.pop("pricing", None) or ondemand_pricing(),
        kwargs.pop("slots", None),
        None, 10.0, location=location,
    )


def test_sydney_source_priced_at_sydney_rates(_restore_region_constants) -> None:
    """An australia-southeast1 Source uses $8.125/TiB, not the US $6.25 (Montu bug)."""
    r = _estimate_at("australia-southeast1", slots=slot_util())
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "8.125" in scan.source_note
    assert r.bq_pricing_region == "australia-southeast1"
    assert r.aws_pricing_region == "ap-southeast-2"


def test_sydney_costs_more_than_us_for_same_workload(_restore_region_constants) -> None:
    """Same workload, Sydney region → strictly higher BQ estimate than US."""
    big = [entity(size_gb=500_000.0)]
    slots = slot_util()
    us = _estimate_at("US", entities=big, slots=slots).bigquery_monthly
    syd = _estimate_at("australia-southeast1", entities=big, slots=slots).bigquery_monthly
    assert syd > us


def test_aws_side_repriced_with_bq_region(_restore_region_constants) -> None:
    """The AWS comparison uses the mapped region's rates (Sydney RPU $0.419, not $0.375)."""
    r = _estimate_at("australia-southeast1", slots=slot_util())
    compute = next(ln for ln in r.aws_lines if "compute" in ln.label.lower())
    assert "0.419" in compute.source_note


def test_unknown_location_falls_back_to_us_with_caveat(_restore_region_constants) -> None:
    """An unmapped location prices at US rates and carries a pricing-note caveat."""
    r = _estimate_at("mars-north1", slots=slot_util())
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "6.25" in scan.source_note
    assert any("mars-north1" in n for n in r.pricing_notes)


def test_no_location_preserves_module_constants(_restore_region_constants, monkeypatch) -> None:
    """location=None (legacy callers/tests) must not re-point overridden constants (R18.7)."""
    monkeypatch.setattr(v4, "V4_ONDEMAND_USD_PER_TIB", 99.0)
    r = _estimate(pricing=ondemand_pricing(), slots=slot_util())
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "99.0" in scan.source_note


def test_scope_notes_disclose_unmodeled_skus(_restore_region_constants) -> None:
    """The estimate must disclose out-of-scope SKUs on BOTH sides of the comparison."""
    r = _estimate_at("US", slots=slot_util())
    joined = " ".join(r.scope_notes).lower()
    assert "storage read/write api" in joined
    assert "streaming" in joined
    assert "spectrum" in joined
    assert "data transfer" in joined


def test_pricing_notes_name_both_pricing_regions(_restore_region_constants) -> None:
    """The pricing notes must say which geography each side was priced in."""
    r = _estimate_at("australia-southeast1", slots=slot_util())
    joined = " ".join(r.pricing_notes)
    assert "australia-southeast1" in joined
    assert "30-day month" in joined


def test_billed_bytes_preferred_over_processed(_restore_region_constants) -> None:
    """Scan cost bills on total_bytes_billed (10 MiB minimums) when available."""
    processed = 10 * (1024 ** 4)
    billed = 15 * (1024 ** 4)   # small-query minimums push billed above processed
    s = SlotUtilization(
        avg_slots=1.0, p50_slots=1.0, p99_slots=2.0, peak_slots=2.0,
        active_hour_fraction=0.5, total_slot_ms=730 * 3_600_000, days_sampled=14,
        total_bytes_processed=processed, total_bytes_billed=billed,
        has_billed_bytes=True, total_queries=100,
    )
    s_proc_only = SlotUtilization(
        avg_slots=1.0, p50_slots=1.0, p99_slots=2.0, peak_slots=2.0,
        active_hour_fraction=0.5, total_slot_ms=730 * 3_600_000, days_sampled=14,
        total_bytes_processed=processed, total_bytes_billed=0, total_queries=100,
    )
    with_billed = _estimate_at("US", slots=s)
    with_processed = _estimate_at("US", slots=s_proc_only)
    assert with_billed.bigquery_monthly > with_processed.bigquery_monthly
    scan = next(ln for ln in with_billed.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "billed" in scan.source_note
    fallback = next(ln for ln in with_processed.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "billed unavailable" in fallback.source_note


def test_degraded_window_never_prices_on_partial_billed_sum(_restore_region_constants) -> None:
    """A degraded window (has_billed_bytes=False) prices on processed bytes even when a
    POSITIVE partial billed sum is present — pricing on the partial sum would silently
    exclude the NULL-billed jobs' volume (2026-07-08 review: the `or billed > 0` clause
    defeated workload.py's degradation policy)."""
    processed = 10 * (1024 ** 4)
    partial_billed = 8 * (1024 ** 4)   # 2 of 10 jobs NULL-billed: sum covers only 8
    s = SlotUtilization(
        avg_slots=1.0, p50_slots=1.0, p99_slots=2.0, peak_slots=2.0,
        active_hour_fraction=0.5, total_slot_ms=730 * 3_600_000, days_sampled=14,
        total_bytes_processed=processed, total_bytes_billed=partial_billed,
        has_billed_bytes=False, total_queries=10, lookback_days=14,
    )
    result = _estimate_at("US", slots=s)
    scan = next(ln for ln in result.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "billed unavailable" in scan.source_note   # processed fallback, labelled
    # Priced on the 10 TiB processed overestimate, not the 8 TiB partial sum.
    assert f"{processed / (1024**4):,.2f} TiB" in scan.source_note


# --- Review fixes (2026-07-03): cascade ordering, projection window, billed-zero -------

def test_cascade_does_not_clobber_live_rates(_restore_region_constants, monkeypatch) -> None:
    """CLI ordering: cascade applied in Stage 9b, live rates layered on top — estimate()
    passing the SAME location must not re-apply the hardcoded table over the live rates."""
    v4.apply_bq_region("australia-southeast1")
    k.apply_aws_region("ap-southeast-2")
    # Live refresh drifts the rates (simulating apply_live_rates)
    monkeypatch.setattr(v4, "V4_ONDEMAND_USD_PER_TIB", 9.99)
    monkeypatch.setattr(k, "V1_RPU_HOUR_USD", 0.5)
    r = _estimate_at("australia-southeast1", slots=slot_util())
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "9.99" in scan.source_note      # live rate survived
    compute = next(ln for ln in r.aws_lines if "compute" in ln.label.lower())
    assert "0.5" in compute.source_note


def test_unknown_region_resets_to_us_not_previous(_restore_region_constants) -> None:
    """An unknown location after a regional estimate must reset to US rates, not keep
    the previous region's (the 'priced at US rates' caveat must be true)."""
    _estimate_at("australia-southeast1", slots=slot_util())
    r = _estimate_at("mars-north1", slots=slot_util())
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "6.25" in scan.source_note and "8.125" not in scan.source_note
    assert r.bq_pricing_region == "us"


def test_us_central1_has_verified_rates_no_scary_caveat(_restore_region_constants) -> None:
    """us-central1 (the most common single US region) is in the verified table — no
    unknown-region caveat, storage at its regional rate."""
    r = _estimate_at("us-central1", slots=slot_util())
    assert not any("No verified rate table" in n for n in r.pricing_notes)
    storage = next(ln for ln in r.bigquery_breakdown if "storage" in ln.label.lower())
    assert "0.023" in storage.source_note


def test_scan_projected_over_calendar_window_not_active_days(_restore_region_constants) -> None:
    """A batch workload active 4 of 30 days projects over the 30-day window (~1×), not
    the 4 active days (7.5× inflation)."""
    ten_tib = 10 * (1024 ** 4)
    s = SlotUtilization(
        avg_slots=1.0, p50_slots=1.0, p99_slots=2.0, peak_slots=2.0,
        active_hour_fraction=0.1, total_slot_ms=4 * 3_600_000, days_sampled=4,
        total_bytes_processed=ten_tib, total_bytes_billed=ten_tib,
        has_billed_bytes=True, total_queries=40, lookback_days=30,
    )
    r = _estimate_at("US", slots=s)
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert scan.monthly == pytest.approx(10 * 6.25, rel=0.05)   # ~10 TiB/mo, not ~75


def test_billed_zero_window_is_zero_scan_not_fallback(_restore_region_constants) -> None:
    """A window that carried the billed column with a genuine zero (all-cached /
    reservation-served) bills $0 scan — no fallback to processed bytes."""
    s = SlotUtilization(
        avg_slots=1.0, p50_slots=1.0, p99_slots=2.0, peak_slots=2.0,
        active_hour_fraction=0.5, total_slot_ms=730 * 3_600_000, days_sampled=14,
        total_bytes_processed=5 * (1024 ** 4), total_bytes_billed=0,
        has_billed_bytes=True, total_queries=100, lookback_days=14,
    )
    r = _estimate_at("US", slots=s)
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert scan.monthly == 0
    assert "billed unavailable" not in scan.source_note


def test_workload_profile_scan_volume_matches_cost_basis(_restore_region_constants) -> None:
    """The recommendation's monthly_scanned_tb quotes the same (billed) basis and window
    as the BigQuery cost line — the report must not contradict itself."""
    processed, billed = 10 * (1024 ** 4), 15 * (1024 ** 4)
    s = SlotUtilization(
        avg_slots=1.0, p50_slots=1.0, p99_slots=2.0, peak_slots=2.0,
        active_hour_fraction=0.5, total_slot_ms=730 * 3_600_000, days_sampled=30,
        total_bytes_processed=processed, total_bytes_billed=billed,
        has_billed_bytes=True, total_queries=1000, lookback_days=30,
    )
    r = _estimate_at("US", slots=s)
    wp = r.recommendation.workload_profile
    assert wp.monthly_scanned_tb == pytest.approx((billed / 30 * 30.0) / (1024 ** 4), rel=1e-6)


def test_edition_commitments_use_catalog_factors(_restore_region_constants) -> None:
    """Regional edition commitments follow the catalog's ×0.8/×0.6 SKU factors (Sydney
    ENTERPRISE 1yr $0.0648 = 0.081×0.8, verified), not a fabricated ×0.9/×0.8."""
    v4.apply_bq_region("australia-southeast1")
    assert v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_1yr"] == pytest.approx(0.0648)
    assert v4.V4_EDITION_SLOT_HOUR_USD["ENTERPRISE"]["commit_3yr"] == pytest.approx(0.0486)


# --- Round-2 review fixes (2026-07-04) --------------------------------------------------

def test_live_rates_survive_for_region_outside_hardcoded_table(_restore_region_constants) -> None:
    """A live catalog lookup for a region NOT in V4_REGIONAL_RATES stamps the region tag;
    estimate() must then keep those rates instead of resetting to hardcoded US."""
    from bq_assess.core.price_lookup import GCPRates, PricingRates, apply_live_rates
    live = PricingRates(bq_location="asia-east1", aws_region="us-east-1")
    live.gcp = GCPRates(ondemand_usd_per_tib=6.75, storage_active_logical_usd_per_gib=0.023,
                        fetched_at="2026-07-04", source="GCP Cloud Billing Catalog API (asia-east1)")
    apply_live_rates(live)
    assert v4.V4_PRICING_REGION == "asia-east1"

    r = _estimate_at("asia-east1", slots=slot_util())
    scan = next(ln for ln in r.bigquery_breakdown if "scanned" in ln.label.lower())
    assert "6.75" in scan.source_note                      # live rate survived the cascade
    assert r.bq_pricing_region == "asia-east1"
    assert not any("No verified rate table" in n for n in r.pricing_notes)


def test_hardcoded_fallback_source_does_not_stamp_freshness(_restore_region_constants) -> None:
    """The GCP fallback's source reads 'hardcoded (verified …)' — apply_live_rates must not
    treat it as live (no date stamp, no region tag update)."""
    from bq_assess.core.price_lookup import GCPRates, PricingRates, apply_live_rates
    before_date, before_region = v4.V4_CONFIRMED_DATE, v4.V4_PRICING_REGION
    fallback = PricingRates(bq_location="australia-southeast1")
    fallback.gcp = GCPRates(ondemand_usd_per_tib=6.25, fetched_at="2026-06-24",
                            source="hardcoded (verified 2026-06-24)")
    apply_live_rates(fallback)
    assert v4.V4_CONFIRMED_DATE == before_date
    assert v4.V4_PRICING_REGION == before_region


def test_default_edition_commitments_match_catalog_factors() -> None:
    """The module-default edition table (location=None path) must carry the same catalog
    x0.8/x0.6 commitment factors apply_bq_region derives — not the fabricated x0.9/x0.8."""
    for edition, rates in v4.V4_EDITION_SLOT_HOUR_USD.items():
        assert rates["commit_1yr"] == pytest.approx(rates["payg"] * 0.8), edition
        assert rates["commit_3yr"] == pytest.approx(rates["payg"] * 0.6), edition


def test_reused_estimator_refreshes_per_region_pair(_restore_region_constants, monkeypatch) -> None:
    """A reused CostEstimator must attempt a live refresh for EACH region pair, not once."""
    calls: list[tuple[str, str]] = []

    class _FakeLookup:
        def __init__(self, aws_region="us-east-1", bq_location="us", use_cache=True):
            calls.append((bq_location, aws_region))
        def fetch(self, gcp_client=None):
            raise RuntimeError("no network in tests")

    import bq_assess.core.price_lookup as pl
    monkeypatch.setattr(pl, "PriceLookup", _FakeLookup)
    est = CostEstimator(skip_live_pricing=False)
    est.estimate([entity()], ondemand_pricing(), slot_util(), None, 10.0, location="US")
    est.estimate([entity()], ondemand_pricing(), slot_util(), None, 10.0,
                 location="australia-southeast1")
    est.estimate([entity()], ondemand_pricing(), slot_util(), None, 10.0, location="US")
    assert ("us", "us-east-1") in calls
    assert ("australia-southeast1", "ap-southeast-2") in calls
    assert len(calls) == 2       # third call = repeat pair, no re-fetch


# --- Physical bytes storage sizing (Task 4) ---------------------------------------------------

def test_s3_storage_line_uses_physical_bytes_when_measured():
    """S3 storage line uses physical_bytes when measured=True, HIGH confidence."""
    entities = [entity(size_gb=100.0, name=f"ds.t{i}") for i in range(3)]
    for e in entities:
        e.num_bytes = int(e.size_gb * (1024 ** 3))
        e.physical_bytes = round(e.num_bytes * 0.4)  # ~2.5× compression

    est = CostEstimator(skip_live_pricing=True)
    result = est.estimate(entities, ondemand_pricing(), slot_util(), None, 10.0,
                          storage_basis="measured")
    storage = _aws("storage", result)

    assert storage.confidence == ConfidenceLevel.HIGH
    total_physical = sum(e.physical_bytes for e in entities)
    expected_gb = total_physical * k.GB_PER_BYTE
    assert expected_gb > 0
    assert "TABLE_STORAGE" in storage.source_note
    assert str(k.ASSUMED_PHYSICAL_RATIO) not in storage.source_note


def test_s3_storage_line_drops_to_medium_confidence_when_fallback():
    """S3 storage line uses MEDIUM confidence when basis=assumed (0.75× fallback)."""
    entities = [entity(size_gb=100.0, name=f"ds.t{i}") for i in range(3)]
    for e in entities:
        e.num_bytes = int(e.size_gb * (1024 ** 3))
        e.physical_bytes = round(e.num_bytes * k.ASSUMED_PHYSICAL_RATIO)

    est = CostEstimator(skip_live_pricing=True)
    result = est.estimate(entities, ondemand_pricing(), slot_util(), None, 10.0,
                          storage_basis="assumed")
    storage = _aws("storage", result)

    assert storage.confidence == ConfidenceLevel.MEDIUM
    assert str(k.ASSUMED_PHYSICAL_RATIO) in storage.source_note
    assert "TABLE_STORAGE unavailable" in storage.source_note


def test_s3_storage_line_mixed_basis():
    """S3 storage line uses MEDIUM confidence for mixed basis."""
    entities = [entity(size_gb=100.0, name=f"ds.t{i}") for i in range(3)]
    for e in entities:
        e.num_bytes = int(e.size_gb * (1024 ** 3))
        e.physical_bytes = round(e.num_bytes * k.ASSUMED_PHYSICAL_RATIO)

    est = CostEstimator(skip_live_pricing=True)
    result = est.estimate(entities, ondemand_pricing(), slot_util(), None, 10.0,
                          storage_basis="mixed")
    storage = _aws("storage", result)

    assert storage.confidence == ConfidenceLevel.MEDIUM
    assert "mixed" in storage.source_note


# --- Long-term storage split (2026-07-23) ---------------------------------------------


def _meta_entity(size_gb: float, name: str, last_modified):
    """EntityMetadata with a real last_modified (EntityReport carries none)."""
    from bq_assess.models import EntityMetadata
    return EntityMetadata(
        entity_id=name.split(".")[-1], dataset_id=name.split(".")[0], full_name=name,
        entity_type=EntityType.TABLE, population=EntityPopulation.TABLE,
        num_rows=1000, num_bytes=int(size_gb * (1024 ** 3)), columns=[],
        time_partitioning=None, range_partitioning=None, clustering_fields=None,
        view_query=None, mview_query=None, routine=None, depends_on=[],
        last_modified=last_modified,
    )


class TestLongTermStorageSplit:
    def test_idle_table_bills_at_longterm_rate(self) -> None:
        from datetime import datetime, timezone
        as_of = datetime(2026, 7, 16, tzinfo=timezone.utc)
        stale = _meta_entity(100.0, "ds.cold", datetime(2026, 1, 1, tzinfo=timezone.utc))
        cc = CostEstimator(skip_live_pricing=True).estimate(
            [stale], ondemand_pricing(), None, None, 0, as_of=as_of)
        line = next(ln for ln in cc.bigquery_breakdown if ln.label == "BigQuery storage")
        assert line.monthly == pytest.approx(
            100.0 * v4.V4_STORAGE_LONGTERM_LOGICAL_USD_PER_GIB_MONTH, rel=1e-6)
        assert "long-term" in line.source_note

    def test_recent_table_bills_active(self) -> None:
        from datetime import datetime, timezone
        as_of = datetime(2026, 7, 16, tzinfo=timezone.utc)
        hot = _meta_entity(100.0, "ds.hot", datetime(2026, 7, 1, tzinfo=timezone.utc))
        cc = CostEstimator(skip_live_pricing=True).estimate(
            [hot], ondemand_pricing(), None, None, 0, as_of=as_of)
        line = next(ln for ln in cc.bigquery_breakdown if ln.label == "BigQuery storage")
        assert line.monthly == pytest.approx(
            100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH, rel=1e-6)
        assert "long-term" not in line.source_note

    def test_mixed_estate_splits(self) -> None:
        from datetime import datetime, timezone
        as_of = datetime(2026, 7, 16, tzinfo=timezone.utc)
        hot = _meta_entity(100.0, "ds.hot", datetime(2026, 7, 1, tzinfo=timezone.utc))
        cold = _meta_entity(300.0, "ds.cold", datetime(2025, 12, 1, tzinfo=timezone.utc))
        cc = CostEstimator(skip_live_pricing=True).estimate(
            [hot, cold], ondemand_pricing(), None, None, 0, as_of=as_of)
        line = next(ln for ln in cc.bigquery_breakdown if ln.label == "BigQuery storage")
        expected = (100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH
                    + 300.0 * v4.V4_STORAGE_LONGTERM_LOGICAL_USD_PER_GIB_MONTH)
        assert line.monthly == pytest.approx(expected, rel=1e-6)

    def test_entities_without_last_modified_count_active(self) -> None:
        # EntityReport has no last_modified — conservative default is ACTIVE.
        cc = _estimate([entity(size_gb=50.0)])
        line = next(ln for ln in cc.bigquery_breakdown if ln.label == "BigQuery storage")
        assert line.monthly == pytest.approx(
            50.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH, rel=1e-6)

    def test_longterm_state_does_not_leak_across_calls(self) -> None:
        from datetime import datetime, timezone
        as_of = datetime(2026, 7, 16, tzinfo=timezone.utc)
        est = CostEstimator(skip_live_pricing=True)
        cold = _meta_entity(100.0, "ds.cold", datetime(2026, 1, 1, tzinfo=timezone.utc))
        est.estimate([cold], ondemand_pricing(), None, None, 0, as_of=as_of)
        # Second call, all-active estate — must NOT inherit the previous split.
        cc2 = est.estimate([entity(size_gb=100.0)], ondemand_pricing(), None, None, 0)
        line = next(ln for ln in cc2.bigquery_breakdown if ln.label == "BigQuery storage")
        assert line.monthly == pytest.approx(
            100.0 * v4.V4_STORAGE_ACTIVE_LOGICAL_USD_PER_GIB_MONTH, rel=1e-6)


# --- Serverless 4-RPU active-hours billing floor (2026-07-23) --------------------------


class TestServerlessComputeFloor:
    def test_floor_binds_for_always_on_light_workload(self) -> None:
        # ~97% active but tiny slot usage (the pdp22 shape): slot-derived RPU-hours
        # land far below 4 RPU × active hours — the floor must lift the line.
        s = slot_util(total_slot_ms=100 * 3_600_000, active_fraction=0.97)
        line = _aws("compute", _estimate(slots=s))
        floor_hours = k.SERVERLESS_MIN_RPU_FLOOR * 0.97 * k.HOURS_PER_MONTH
        assert line.monthly == round(floor_hours * k.V1_RPU_HOUR_USD, 4)
        assert "floored" in line.source_note

    def test_floor_does_not_bind_for_heavy_workload(self) -> None:
        # Slot-derived hours far above the floor: figure unchanged, no floor note.
        # Read the Serverless OD scenario directly — a heavy workload may get a
        # reserved/provisioned recommendation, changing what aws_lines carries.
        s = slot_util(total_slot_ms=100_000 * 3_600_000, active_fraction=0.5)
        r = _estimate(slots=s)
        od = next(sc for sc in r.aws_scenarios if sc.category == "SERVERLESS")
        line = next(ln for ln in od.lines if "compute" in ln.label.lower())
        assert line.monthly == round(
            100_000 * k.V3_SLOT_TO_RPU_RATIO * k.V1_RPU_HOUR_USD, 4)
        assert "floored" not in line.source_note

    def test_idle_workload_floor_scales_with_active_fraction(self) -> None:
        # 1% active: floor is 4 RPU × 1% of the month — nearly slot-derived only.
        s = slot_util(total_slot_ms=1 * 3_600_000, active_fraction=0.01)
        line = _aws("compute", _estimate(slots=s))
        floor_hours = k.SERVERLESS_MIN_RPU_FLOOR * 0.01 * k.HOURS_PER_MONTH
        assert line.monthly == round(floor_hours * k.V1_RPU_HOUR_USD, 4)


# --- S3 Tables Intelligent-Tiering split (2026-07-31) ----------------------------------


class TestIntelligentTieringSplit:
    """_int_tier_split mirrors _longterm_bytes: last_modified signal, as_of anchor,
    missing-timestamp entities count Frequent (conservative)."""

    AS_OF = None  # set in setup

    def setup_method(self):
        from datetime import datetime, timezone
        type(self).AS_OF = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def _aged(self, days_old, size_gb=100.0, name="ds.aged"):
        from datetime import timedelta
        return _meta_entity(size_gb, name, self.AS_OF - timedelta(days=days_old))

    def test_all_fresh_is_all_frequent(self) -> None:
        from bq_assess.engine.redshift.cost import _int_tier_split
        freq, ia, arc = _int_tier_split(
            [self._aged(5), self._aged(10, name="ds.b")], as_of=self.AS_OF)
        assert (freq, ia, arc) == (1.0, 0.0, 0.0)

    def test_three_way_split_by_bytes(self) -> None:
        from bq_assess.engine.redshift.cost import _int_tier_split
        ents = [
            self._aged(5, size_gb=100.0, name="ds.hot"),      # frequent
            self._aged(45, size_gb=100.0, name="ds.warm"),    # infrequent (30-89d)
            self._aged(200, size_gb=200.0, name="ds.cold"),   # archive (90d+)
        ]
        freq, ia, arc = _int_tier_split(ents, as_of=self.AS_OF)
        assert freq == pytest.approx(0.25)
        assert ia == pytest.approx(0.25)
        assert arc == pytest.approx(0.50)
        assert freq + ia + arc == pytest.approx(1.0)

    def test_missing_last_modified_counts_frequent(self) -> None:
        from bq_assess.engine.redshift.cost import _int_tier_split
        unknown = entity(size_gb=100.0, name="ds.unknown")  # EntityReport: no last_modified
        cold = self._aged(200, size_gb=100.0, name="ds.cold")
        freq, _ia, arc = _int_tier_split([unknown, cold], as_of=self.AS_OF)
        assert freq == pytest.approx(0.5)
        assert arc == pytest.approx(0.5)

    def test_naive_datetime_treated_utc(self) -> None:
        from datetime import timedelta

        from bq_assess.engine.redshift.cost import _int_tier_split
        e = _meta_entity(100.0, "ds.naive",
                         self.AS_OF.replace(tzinfo=None) - timedelta(days=200))
        _freq, _ia, arc = _int_tier_split([e], as_of=self.AS_OF)
        assert arc == pytest.approx(1.0)

    def test_anchors_to_as_of_not_now(self) -> None:
        """A table 45 days old AT COLLECTION stays infrequent even if the
        report is generated much later (bundle-ages-on-disk rule)."""
        from datetime import datetime, timedelta, timezone

        from bq_assess.engine.redshift.cost import _int_tier_split
        collected = datetime(2026, 6, 1, tzinfo=timezone.utc)
        e = _meta_entity(100.0, "ds.t", collected - timedelta(days=45))
        _freq, ia, _arc = _int_tier_split([e], as_of=collected)
        assert ia == pytest.approx(1.0)

    def test_empty_entities(self) -> None:
        from bq_assess.engine.redshift.cost import _int_tier_split
        assert _int_tier_split([], as_of=self.AS_OF) == (1.0, 0.0, 0.0)


class TestIntelligentTieringStorageLine:
    """Storage line prices the IT steady state as the range low bound; the
    structured per-tier breakdown feeds the report's derivation table."""

    AS_OF = None

    def setup_method(self):
        from datetime import datetime, timezone
        type(self).AS_OF = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def _aged(self, days_old, size_gb, name):
        from datetime import timedelta
        return _meta_entity(size_gb, name, self.AS_OF - timedelta(days=days_old))

    def _est(self, ents):
        return CostEstimator(skip_live_pricing=True).estimate(
            ents, ondemand_pricing(), None, None, 0, as_of=self.AS_OF)

    def test_storage_line_is_range_with_cold_data(self) -> None:
        r = self._est([
            self._aged(5, 1000.0, "ds.hot"),
            self._aged(200, 1000.0, "ds.cold"),
        ])
        storage = next(ln for ln in r.aws_lines if ln.label == "S3 Tables storage")
        assert storage.monthly is None
        assert storage.monthly_low is not None and storage.monthly_high is not None
        assert storage.monthly_low < storage.monthly_high
        # note leads with the verdict in the customer's numbers
        assert "your cost on today's storage access pattern" in storage.source_note
        assert "month 1 only" in storage.source_note

    def test_storage_line_stays_point_when_all_fresh(self) -> None:
        r = self._est([self._aged(5, 1000.0, "ds.hot")])
        storage = next(ln for ln in r.aws_lines if ln.label == "S3 Tables storage")
        assert storage.monthly is not None
        assert storage.monthly_low is None

    def test_steady_state_bills_cold_bytes_at_aia_rate(self) -> None:
        """All-archive estate: low bound ≈ AIA flat rate + monitoring."""
        r = self._est([self._aged(200, 1000.0, "ds.cold")])
        storage = next(ln for ln in r.aws_lines if ln.label == "S3 Tables storage")
        gb = 1000.0 * (1024 ** 3) * k.ASSUMED_PHYSICAL_RATIO * k.GB_PER_BYTE
        objects = (1000.0 * (1024 ** 3) * k.ASSUMED_PHYSICAL_RATIO) / (k.V2_ASSUMED_OBJECT_SIZE_MB * 1e6)
        monitoring = objects / 1000.0 * k.V2_OBJECT_MONITORING_USD_PER_1K_OBJECTS_MONTH
        expected_low = gb * k.V2_INT_AIA_USD_PER_GB_MONTH + monitoring
        assert storage.monthly_low == pytest.approx(expected_low, rel=1e-3)

    def test_headline_uses_pattern_based_storage_not_range(self) -> None:
        """The cost comparison prices storage on the observed access pattern
        (steady state) as a single figure — the month-1 all-Frequent bound is
        breakdown-display only and must NOT widen aws_monthly_low/high
        (2026-07-31 decision: one figure for the comparison)."""
        r = self._est([
            self._aged(5, 1000.0, "ds.hot"),
            self._aged(200, 1000.0, "ds.cold"),
        ])
        storage = next(ln for ln in r.aws_lines if ln.label == "S3 Tables storage")
        # the line still displays the range in the breakdown...
        assert storage.monthly_low is not None and storage.monthly_high is not None
        assert storage.monthly_low < storage.monthly_high
        # ...but totals count only the pattern-based (steady-state) figure
        assert storage.headline == storage.monthly_low
        # no compute line here (slots=None → range floor), so check via a
        # hot-only estate of the same total bytes: the high bound must NOT
        # include the all-Frequent storage figure.
        hot_only = self._est([self._aged(5, 2000.0, "ds.hot")])
        assert r.aws_monthly_high < hot_only.aws_monthly_high

    def test_tier_breakdown_structure(self) -> None:
        r = self._est([
            self._aged(5, 100.0, "ds.hot"),
            self._aged(45, 100.0, "ds.warm"),
            self._aged(200, 100.0, "ds.cold"),
        ])
        bd = r.storage_tier_breakdown
        assert [row["tier"] for row in bd] == [
            "frequent", "infrequent", "archive", "monitoring", "total"]
        by = {row["tier"]: row for row in bd}
        assert by["frequent"]["tables"] == 1
        assert by["infrequent"]["tables"] == 1
        assert by["archive"]["tables"] == 1
        assert by["total"]["tables"] == 3
        # steady-state total row matches the line's low bound
        storage = next(ln for ln in r.aws_lines if ln.label == "S3 Tables storage")
        assert by["total"]["monthly"] == pytest.approx(storage.monthly_low, rel=1e-6)

    def test_tier_breakdown_empty_without_cold_data(self) -> None:
        r = self._est([self._aged(5, 100.0, "ds.hot")])
        assert r.storage_tier_breakdown == []

    def test_int_pricing_note_present_with_cold_data(self) -> None:
        r = self._est([self._aged(200, 100.0, "ds.cold")])
        notes = " ".join(r.pricing_notes)
        assert "Intelligent-Tiering" in notes
        assert "access" in notes.lower()


def test_comparison_module_honors_headline() -> None:
    """engine/comparison.py must use the canonical _line_low/_line_high (which
    honor CostLine.headline) — its local duplicates silently reverted the
    single-figure comparison for every engine-recommendation report
    (2026-07-31 sandbox regeneration)."""
    from bq_assess.engine import comparison
    from bq_assess.engine.redshift import cost as cost_mod
    assert comparison._line_low is cost_mod._line_low
    assert comparison._line_high is cost_mod._line_high

    from bq_assess.models import CostLine
    ln = CostLine(label="s", monthly=None, monthly_low=1246.0, monthly_high=3684.0,
                  confidence=ConfidenceLevel.MEDIUM, source_note="x", headline=1246.0)
    assert comparison._line_low(ln) == 1246.0
    assert comparison._line_high(ln) == 1246.0


class TestRangeLineTotalsSafety:
    """No cost-total arithmetic may read CostLine.monthly directly — a range
    line (monthly=None) silently contributes $0 (2026-08-03: pdp22's Athena
    scenario showed $2,223 '92% cheaper' with $18,900 storage missing)."""

    def test_no_raw_monthly_arithmetic_in_engine(self) -> None:
        """Structural guard: engine code must total lines via _line_value/
        _line_low/_line_high (which honor headline/range), never `.monthly or 0`
        or `.monthly +`. The helpers' own definitions are the sole exception."""
        import re
        from pathlib import Path

        engine_dir = Path(__file__).resolve().parents[2] / "src" / "bq_assess" / "engine"
        offenders = []
        pattern = re.compile(r"\.monthly\s*(?:or\s+0|[+*])|[+]\s*\w+_line\.monthly\b")
        for py in engine_dir.rglob("*.py"):
            for i, line in enumerate(py.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "def _line_" in stripped:
                    continue
                if pattern.search(line):
                    offenders.append(f"{py.relative_to(engine_dir)}:{i}: {stripped[:80]}")
        assert not offenders, (
            "Raw CostLine.monthly arithmetic (breaks on range lines) — use "
            f"_line_value/_line_low/_line_high: {offenders}"
        )

    def test_athena_scenario_includes_range_storage(self) -> None:
        """The Athena scenario's total must include storage when the storage
        line is an Intelligent-Tiering range."""
        from decimal import Decimal

        from bq_assess.engine.comparison import _build_athena_scenario
        from bq_assess.models import (
            CostLine,
            EngineCostEstimate,
            WorkloadProfile,
        )

        storage = CostLine(
            label="S3 Tables storage", monthly=None,
            monthly_low=18900.0, monthly_high=18947.0,
            confidence=ConfidenceLevel.MEDIUM, source_note="range",
            headline=18900.0,
        )
        est = EngineCostEstimate(
            engine_id="athena", monthly_total=Decimal(2223),
            monthly_compute=Decimal(2223), monthly_storage=Decimal(0),
            pricing_mode="on_demand", confidence="MEDIUM", source_note="t",
            one_time_migration=Decimal(0),
        )
        profile = WorkloadProfile()
        scenario = _build_athena_scenario(est, storage, profile)
        assert scenario.monthly_total == pytest.approx(2223 + 18900)

    def test_rms_split_applies_to_range_storage_line(self) -> None:
        """apply_rms_storage_split must not silently no-op when the storage
        line is a range — RMS-placed tables need their RMS line either way."""
        from bq_assess.engine.redshift.cost import apply_rms_storage_split
        from bq_assess.models import CostComparison, CostLine

        storage = CostLine(
            label="S3 Tables storage", monthly=None,
            monthly_low=1000.0, monthly_high=3000.0,
            confidence=ConfidenceLevel.MEDIUM, source_note="range",
            headline=1000.0,
        )
        cc = CostComparison(
            bq_pricing_model=BQPricingModel.ON_DEMAND, bigquery_monthly=5000.0,
            bigquery_breakdown=[], aws_lines=[storage],
            aws_monthly_low=1000.0, aws_monthly_high=1000.0,
            monthly_delta_low=4000.0, monthly_delta_high=4000.0,
            annual_savings_low=48000.0, annual_savings_high=48000.0,
            migration_onetime=100.0, breakeven_months_low=1.0,
            breakeven_months_high=1.0, compute_confidence=ConfidenceLevel.MEDIUM,
        )
        one_tb = 1024 ** 4
        apply_rms_storage_split(cc, rms_physical_bytes=one_tb,
                                total_physical_bytes=10 * one_tb)
        rms_lines = [ln for ln in cc.aws_lines if "RMS" in ln.label]
        assert rms_lines, "RMS line missing — split no-opped on a range line"
        assert "moved to RMS" in storage.source_note
        # range bounds and headline all shifted down, never negative
        assert storage.monthly_low < 1000.0
        assert storage.headline == storage.monthly_low


class TestBQCostAvailability:
    """CostComparison must be able to represent 'BQ cost unknowable' (2026-08-10)."""

    def test_defaults_available_modelled(self) -> None:
        result = _estimate(pricing=ondemand_pricing(), slots=slot_util())
        assert result.bq_cost_available is True
        assert result.bq_cost_basis == "modelled"
        assert result.bq_cost_unavailable_reason == ""

    def test_override_sets_customer_provided_basis(self) -> None:
        result = _estimate(pricing=ondemand_pricing(), slots=slot_util(), override=5000.0)
        assert result.bq_cost_available is True
        assert result.bq_cost_basis == "customer_provided"


class TestEnterpriseUnavailableHardStop:
    """ENTERPRISE/EP capacity without reservation data: no BQ figure, no savings (2026-08-10)."""

    def _unavailable_pricing(self, edition="ENTERPRISE") -> PricingDetection:
        return PricingDetection(
            model=BQPricingModel.CAPACITY, confidence=ConfidenceLevel.MEDIUM,
            source_note="capacity (test)", edition=edition,
            baseline_slots=None, max_slots=None, commitment_slots=None,
            commitment_plan=None,
        )

    def test_enterprise_no_data_is_unavailable(self) -> None:
        result = _estimate(pricing=self._unavailable_pricing(), slots=slot_util())
        assert result.bq_cost_available is False
        assert result.bq_cost_basis == "unavailable"
        assert result.bigquery_monthly == 0.0          # storage must NOT leak into the headline
        assert result.monthly_delta_low == 0.0
        assert result.annual_savings_low == 0.0
        assert result.breakeven_months_low == k.BREAKEVEN_NEVER

    def test_enterprise_plus_no_data_is_unavailable(self) -> None:
        result = _estimate(pricing=self._unavailable_pricing("ENTERPRISE_PLUS"), slots=slot_util())
        assert result.bq_cost_available is False

    def test_unreadable_reservation_is_unavailable_with_permission_reason(self) -> None:
        p = self._unavailable_pricing()
        p.reservation_readable = False
        result = _estimate(pricing=p, slots=slot_util())
        assert result.bq_cost_available is False
        assert "permission" in result.bq_cost_unavailable_reason.lower()

    def test_old_bundle_reason_names_recollection_not_permissions(self) -> None:
        p = self._unavailable_pricing()
        p.reservation_data_collected = False
        result = _estimate(pricing=p, slots=slot_util())
        assert result.bq_cost_available is False
        reason = result.bq_cost_unavailable_reason.lower()
        assert "older collector" in reason or "re-collect" in reason
        assert "permission denied" not in reason

    def test_flag_override_restores_comparison(self) -> None:
        result = _estimate(pricing=self._unavailable_pricing(), slots=slot_util(), override=8000.0)
        assert result.bq_cost_available is True
        assert result.bq_cost_basis == "customer_provided"
        assert result.bigquery_monthly == 8000.0
        assert result.monthly_delta_low != 0.0

    def test_enterprise_with_reservation_data_still_prices(self) -> None:
        result = _estimate(pricing=capacity_pricing(edition="ENTERPRISE"), slots=slot_util())
        assert result.bq_cost_available is True
        assert result.bigquery_monthly > 0

    def test_standard_edition_with_unreadable_reservation_still_priceable_from_slots(self) -> None:
        """STANDARD with permission-denied reservation → from-slots range path, NOT unavailable."""
        p = PricingDetection(
            model=BQPricingModel.CAPACITY, confidence=ConfidenceLevel.MEDIUM,
            source_note="capacity (test)", edition="STANDARD",
            baseline_slots=None, max_slots=None, commitment_slots=None,
            commitment_plan=None, reservation_readable=False,
        )
        result = _estimate(pricing=p, slots=slot_util())
        assert result.bq_cost_available is True, "STANDARD without reservation should be priceable from slots"
        assert result.bq_cost_basis == "modelled"
        # Check that compute line is a range, not UNAVAILABLE
        compute = next((l for l in result.bigquery_breakdown if "capacity" in l.label.lower()), None)
        assert compute is not None
        assert compute.monthly is None, "from-slots path returns a range (monthly=None)"
        assert compute.monthly_low is not None
        assert compute.monthly_high is not None
        assert "UNAVAILABLE" not in compute.label
        # Total bigquery_monthly should be > 0 (uses headline from compute + storage)
        assert result.bigquery_monthly > 0

    def test_enterprise_with_unreadable_reservation_still_hard_stops(self) -> None:
        """ENTERPRISE + reservation_readable=False → UNAVAILABLE (commitment type unknown)."""
        p = PricingDetection(
            model=BQPricingModel.CAPACITY, confidence=ConfidenceLevel.MEDIUM,
            source_note="capacity (test)", edition="ENTERPRISE",
            baseline_slots=None, max_slots=None, commitment_slots=None,
            commitment_plan=None, reservation_readable=False,
        )
        result = _estimate(pricing=p, slots=slot_util())
        assert result.bq_cost_available is False
        assert result.bq_cost_basis == "unavailable"
        assert "permission" in result.bq_cost_unavailable_reason.lower()
