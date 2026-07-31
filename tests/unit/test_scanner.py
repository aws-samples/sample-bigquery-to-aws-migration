"""Unit tests for BigQuery scanner — credential validation, filtering, retry, and resilience."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from bq_assess.scanner import BigQueryScanner, ScannerError, _retry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scanner(**kwargs) -> BigQueryScanner:
    """Create a scanner with use_adc=True and inject a mock client."""
    scanner = BigQueryScanner(project_id="test-project", use_adc=True, **kwargs)
    scanner._client = MagicMock(spec=bigquery.Client)
    # Default: no routines in any dataset (tests that exercise tables override per-dataset).
    scanner._client.list_routines.return_value = []
    return scanner


def _make_schema_field(name: str, field_type: str = "STRING", mode: str = "NULLABLE", fields=()):
    """Build a mock SchemaField."""
    sf = MagicMock(spec=bigquery.SchemaField)
    sf.name = name
    sf.field_type = field_type
    sf.mode = mode
    sf.fields = [_make_schema_field(**f) if isinstance(f, dict) else f for f in fields]
    return sf


def _make_table(dataset_id: str, table_id: str, *, num_rows: int = 100, num_bytes: int = 2048):
    """Build a mock bigquery.Table with minimal metadata."""
    tbl = MagicMock(spec=bigquery.Table)
    tbl.table_id = table_id
    tbl.dataset_id = dataset_id
    tbl.table_type = "TABLE"
    tbl.num_rows = num_rows
    tbl.num_bytes = num_bytes
    tbl.schema = [_make_schema_field("id", "INT64", "REQUIRED")]
    tbl.time_partitioning = None
    tbl.range_partitioning = None
    tbl.clustering_fields = None
    tbl.view_query = None
    tbl.mview_query = None
    tbl.modified = datetime(2024, 6, 1, tzinfo=timezone.utc)
    return tbl


def _make_dataset_list_item(dataset_id: str):
    item = MagicMock(spec=bigquery.dataset.DatasetListItem)
    item.dataset_id = dataset_id
    return item


def _make_table_list_item(dataset_id: str, table_id: str):
    item = MagicMock(spec=bigquery.table.TableListItem)
    item.dataset_id = dataset_id
    item.table_id = table_id
    item.reference = MagicMock(spec=bigquery.TableReference)
    return item


# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------

class TestValidateCredentials:
    """Requirement 2.1 — lightweight metadata query to verify read access."""

    def test_success_returns_true(self):
        scanner = _make_scanner()
        scanner._client.list_datasets.return_value = [_make_dataset_list_item("ds1")]

        assert scanner.validate_credentials() is True
        scanner._client.list_datasets.assert_called_once_with(max_results=1)

    def test_failure_raises_scanner_error(self):
        scanner = _make_scanner()
        scanner._client.list_datasets.side_effect = Exception("403 Forbidden")

        with pytest.raises(ScannerError, match="permissions"):
            scanner.validate_credentials()

    def test_failure_project_not_found(self):
        scanner = _make_scanner()
        scanner._client.list_datasets.side_effect = Exception("404 Not Found")

        with pytest.raises(ScannerError, match="not found"):
            scanner.validate_credentials()

    def test_failure_invalid_credentials(self):
        scanner = _make_scanner()
        scanner._client.list_datasets.side_effect = Exception("401 invalid credentials")

        with pytest.raises(ScannerError, match="Invalid credentials"):
            scanner.validate_credentials()

    def test_no_credentials_raises_scanner_error(self):
        """When neither credentials_path nor use_adc is set, ScannerError is raised."""
        scanner = BigQueryScanner(project_id="test-project")
        with pytest.raises(ScannerError, match="No credentials provided"):
            scanner.validate_credentials()


# ---------------------------------------------------------------------------
# Dataset filtering  (Requirement 3.2)
# ---------------------------------------------------------------------------

class TestDatasetFiltering:

    def test_filter_limits_to_specified_datasets(self):
        scanner = _make_scanner()
        client = scanner._client

        ds_a = _make_dataset_list_item("alpha")
        ds_b = _make_dataset_list_item("beta")
        ds_c = _make_dataset_list_item("gamma")
        client.list_datasets.return_value = [ds_a, ds_b, ds_c]

        tbl_a = _make_table_list_item("alpha", "t1")
        tbl_b = _make_table_list_item("beta", "t2")
        client.list_tables.side_effect = lambda ds_id, **kw: {
            "alpha": [tbl_a],
            "beta": [tbl_b],
        }.get(ds_id, [])

        client.get_table.side_effect = lambda ref, **kw: {
            tbl_a.reference: _make_table("alpha", "t1"),
            tbl_b.reference: _make_table("beta", "t2"),
        }[ref]

        results = list(scanner.scan(dataset_filter=["alpha", "beta"]))

        assert len(results) == 2
        dataset_ids = {r.dataset_id for r in results}
        assert dataset_ids == {"alpha", "beta"}
        # gamma should never have been listed
        listed_ds_ids = [call.args[0] for call in client.list_tables.call_args_list]
        assert "gamma" not in listed_ds_ids

    def test_no_filter_scans_all_datasets(self):
        scanner = _make_scanner()
        client = scanner._client

        ds_a = _make_dataset_list_item("alpha")
        ds_b = _make_dataset_list_item("beta")
        client.list_datasets.return_value = [ds_a, ds_b]

        tbl_a = _make_table_list_item("alpha", "t1")
        tbl_b = _make_table_list_item("beta", "t2")
        client.list_tables.side_effect = lambda ds_id, **kw: {
            "alpha": [tbl_a],
            "beta": [tbl_b],
        }.get(ds_id, [])

        client.get_table.side_effect = lambda ref, **kw: {
            tbl_a.reference: _make_table("alpha", "t1"),
            tbl_b.reference: _make_table("beta", "t2"),
        }[ref]

        results = list(scanner.scan())

        assert len(results) == 2
        dataset_ids = {r.dataset_id for r in results}
        assert dataset_ids == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# Retry logic  (Requirement 15.1)
# ---------------------------------------------------------------------------

class TestRetryLogic:

    @patch("bq_assess.scanner.time.sleep")
    def test_retries_on_429_then_succeeds(self, mock_sleep):
        """Transient 429 on first call, success on second."""
        err = GoogleAPICallError("rate limited")
        err._code = 429
        err.code = 429

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise err
            return "ok"

        result = _retry(fn)
        assert result == "ok"
        assert call_count == 2
        mock_sleep.assert_called_once()  # slept once between attempts

    @patch("bq_assess.scanner.time.sleep")
    def test_retries_on_500_then_succeeds(self, mock_sleep):
        err = GoogleAPICallError("internal error")
        err._code = 500
        err.code = 500

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise err
            return "ok"

        result = _retry(fn)
        assert result == "ok"
        assert call_count == 3
        assert mock_sleep.call_count == 2

    @patch("bq_assess.scanner.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        err = GoogleAPICallError("service unavailable")
        err._code = 503
        err.code = 503

        with pytest.raises(GoogleAPICallError):
            _retry(lambda: (_ for _ in ()).throw(err))

        # 1 initial + 3 retries = 4 calls, 3 sleeps
        assert mock_sleep.call_count == 3

    @patch("bq_assess.scanner.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep):
        err = GoogleAPICallError("rate limited")
        err._code = 429
        err.code = 429

        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise err
            return "ok"

        _retry(fn)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert len(delays) == 3
        for i, actual in enumerate(delays):
            base = 1.0 * (2.0 ** i)
            assert base * 0.5 <= actual <= base * 1.5

    @patch("bq_assess.scanner.time.sleep")
    def test_non_retryable_error_raises_immediately(self, mock_sleep):
        """A 400 Bad Request should not be retried."""
        err = GoogleAPICallError("bad request")
        err._code = 400
        err.code = 400

        with pytest.raises(GoogleAPICallError):
            _retry(lambda: (_ for _ in ()).throw(err))

        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Per-table failure skipping  (Requirement 15.2)
# ---------------------------------------------------------------------------

class TestPerTableFailureSkipping:

    @patch("bq_assess.scanner.time.sleep")
    def test_failed_table_is_skipped_and_recorded(self, mock_sleep):
        scanner = _make_scanner()
        client = scanner._client

        ds = _make_dataset_list_item("ds1")
        client.list_datasets.return_value = [ds]

        tbl_ok = _make_table_list_item("ds1", "good_table")
        tbl_bad = _make_table_list_item("ds1", "bad_table")
        client.list_tables.return_value = [tbl_ok, tbl_bad]

        err = GoogleAPICallError("permanent failure")
        err._code = 400
        err.code = 400

        def get_table_side_effect(ref, **kw):
            if ref is tbl_bad.reference:
                raise err
            return _make_table("ds1", "good_table")

        client.get_table.side_effect = get_table_side_effect

        results = list(scanner.scan())

        # good_table yielded, bad_table skipped
        assert len(results) == 1
        assert results[0].entity_id == "good_table"

        # failure recorded
        assert len(scanner.failures) == 1
        assert scanner.failures[0].entity_name == "ds1.bad_table"
        assert "permanent failure" in scanner.failures[0].error

    @patch("bq_assess.scanner.time.sleep")
    def test_multiple_failures_still_yield_remaining(self, mock_sleep):
        scanner = _make_scanner()
        client = scanner._client

        ds = _make_dataset_list_item("ds1")
        client.list_datasets.return_value = [ds]

        tbl1 = _make_table_list_item("ds1", "t1")
        tbl2 = _make_table_list_item("ds1", "t2")
        tbl3 = _make_table_list_item("ds1", "t3")
        client.list_tables.return_value = [tbl1, tbl2, tbl3]

        err = GoogleAPICallError("fail")
        err._code = 400
        err.code = 400

        def get_table_side_effect(ref, **kw):
            if ref is tbl1.reference:
                raise err
            if ref is tbl3.reference:
                raise err
            return _make_table("ds1", "t2")

        client.get_table.side_effect = get_table_side_effect

        results = list(scanner.scan())

        assert len(results) == 1
        assert results[0].entity_id == "t2"
        assert len(scanner.failures) == 2
        failed_refs = {f.entity_name for f in scanner.failures}
        assert failed_refs == {"ds1.t1", "ds1.t3"}


# ---------------------------------------------------------------------------
# Rate limiting + throttling resilience (2026-07-27 scale review)
# ---------------------------------------------------------------------------

class TestRateLimiter:

    def test_slots_deplete_then_block(self):
        """A 2/s limiter grants its 1s burst window immediately, then paces
        callers at the configured interval."""
        import time as _time

        from bq_assess.core.scanner import RateLimiter

        limiter = RateLimiter(max_per_second=2.0)
        start = _time.monotonic()
        limiter.acquire()  # slot at now-1.0 (burst backlog)
        limiter.acquire()  # slot at now-0.5
        limiter.acquire()  # slot at now
        burst = _time.monotonic() - start
        assert burst < 0.1  # the 1s burst window is immediate

        start = _time.monotonic()
        limiter.acquire()  # next slot is now+0.5 → must wait
        waited = _time.monotonic() - start
        assert waited >= 0.3

    def test_thread_safety_no_over_grant(self):
        """N threads acquiring concurrently never exceed capacity + refill."""
        import threading as _threading
        import time as _time

        from bq_assess.core.scanner import RateLimiter

        limiter = RateLimiter(max_per_second=50.0)
        grants = []

        def worker():
            limiter.acquire()
            grants.append(_time.monotonic())

        threads = [_threading.Thread(target=worker) for _ in range(60)]
        start = _time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 60 acquires at 50/s capacity: the last ~10 must wait for refill —
        # total wall time must exceed 100ms (refill of 10 tokens at 50/s = 200ms,
        # allow generous scheduling slop).
        assert _time.monotonic() - start >= 0.1
        assert len(grants) == 60


class TestThrottlingRetries:

    @patch("bq_assess.core.scanner.time.sleep")
    def test_429_gets_deeper_ladder_than_500(self, mock_sleep):
        """429s retry up to max_retries_throttled (8); 500s stop at max_retries (3)."""
        err = GoogleAPICallError("rate limited")
        err._code = 429
        err.code = 429

        calls = 0

        def fn():
            nonlocal calls
            calls += 1
            if calls <= 6:  # would exhaust the old 3-retry ladder
                raise err
            return "ok"

        assert _retry(fn) == "ok"
        assert calls == 7

    @patch("bq_assess.core.scanner.time.sleep")
    def test_retry_after_header_honored(self, mock_sleep):
        """A Retry-After header sets the delay floor — jittered upward (so
        synchronized workers spread out) and clamped to max_delay_seconds."""
        err = GoogleAPICallError("rate limited")
        err._code = 429
        err.code = 429
        err._response = MagicMock()
        err._response.headers = {"Retry-After": "7"}

        calls = 0

        def fn():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise err
            return "ok"

        assert _retry(fn) == "ok"
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert 7.0 <= slept <= 7.0 * 1.5  # hint is the floor, jitter spreads up

    @patch("bq_assess.core.scanner.time.sleep")
    def test_retry_after_clamped_to_max_delay(self, mock_sleep):
        """A hostile/huge Retry-After (e.g. 3600s) is clamped to max_delay_seconds
        — a worker must never sleep an hour holding a pool slot."""
        from bq_assess.core.scanner import RETRY_CONFIG

        err = GoogleAPICallError("rate limited")
        err._code = 429
        err.code = 429
        err._response = MagicMock()
        err._response.headers = {"Retry-After": "3600"}

        calls = 0

        def fn():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise err
            return "ok"

        assert _retry(fn) == "ok"
        slept = mock_sleep.call_args[0][0]
        assert slept <= RETRY_CONFIG["max_delay_seconds"] * 1.5

    @patch("bq_assess.core.scanner.time.sleep")
    def test_per_class_budgets_independent(self, mock_sleep):
        """A 503 arriving after a burst of 429s gets its own full budget — the
        shared-attempt-counter bug gave it zero retries (2026-07-28 review)."""
        seq = [429, 429, 429, 429, 503, 503, None]  # 4x429, 2x503, then success
        calls = 0

        def fn():
            nonlocal calls
            code = seq[calls]
            calls += 1
            if code:
                e = GoogleAPICallError(f"e{code}")
                e.code = code
                raise e
            return "ok"

        assert _retry(fn) == "ok"
        assert calls == 7  # every error retried within its own class budget

    def test_rate_limiter_charged_per_attempt(self):
        """When a limiter is passed, retries consume tokens too — retry traffic
        must not bypass the global budget during a throttling storm."""
        acquires = []

        class _CountingLimiter:
            def acquire(self):
                acquires.append(1)

        err = GoogleAPICallError("rate limited")
        err.code = 429
        calls = 0

        def fn():
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise err
            return "ok"

        with patch("bq_assess.core.scanner.time.sleep"):
            assert _retry(fn, rate_limiter=_CountingLimiter()) == "ok"
        assert len(acquires) == 3  # one token per attempt, including retries

    @patch("bq_assess.core.scanner.time.sleep")
    def test_connection_errors_retried(self, mock_sleep):
        """Raw transport errors retry like a 503 instead of failing the entity."""
        import requests.exceptions as rex

        calls = 0

        def fn():
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise rex.ConnectionError("connection reset by peer")
            return "ok"

        assert _retry(fn) == "ok"
        assert calls == 3
        assert mock_sleep.call_count == 2


class TestCrossDatasetParallelScan:

    def test_scan_covers_all_datasets_in_one_pass(self):
        """Entities from multiple datasets all surface through the single shared pool."""
        scanner = _make_scanner()
        client = scanner._client

        client.list_datasets.return_value = [
            _make_dataset_list_item("ds1"), _make_dataset_list_item("ds2"),
        ]
        by_ds = {
            "ds1": [_make_table_list_item("ds1", "a")],
            "ds2": [_make_table_list_item("ds2", "b")],
        }
        client.list_tables.side_effect = lambda ds, **kw: by_ds[ds]
        client.get_table.side_effect = lambda ref, **kw: _make_table(
            next(ds for ds, items in by_ds.items() if items[0].reference is ref),
            next(items[0].table_id for items in by_ds.values() if items[0].reference is ref),
        )

        names = {e.full_name for e in scanner.scan()}
        assert names == {"ds1.a", "ds2.b"}
        assert scanner.failures == []


class TestScanAbort:
    """ScanAbortedError fires from inside the scanner — including on failure-only
    tails and dataset-listing failures the old consumer-loop check never saw."""

    @patch("bq_assess.core.scanner.time.sleep")
    def test_listing_failures_abort_by_count(self, _sleep):
        """>ABORT_MAX_LISTING_FAILURES failed dataset listings abort even though
        each is one FailureRecord (it hides an unknown number of entities)."""
        from bq_assess.core.scanner import ScanAbortedError

        scanner = _make_scanner()
        client = scanner._client
        client.list_datasets.return_value = [
            _make_dataset_list_item(f"ds{i}") for i in range(5)
        ]
        err = GoogleAPICallError("listing denied")
        err.code = 403
        client.list_tables.side_effect = err

        with pytest.raises(ScanAbortedError) as exc_info:
            list(scanner.scan())
        assert exc_info.value.listing_failures > 3

    @patch("bq_assess.core.scanner.time.sleep")
    def test_entity_failure_rate_aborts_on_failure_only_tail(self, _sleep):
        """A failure-only tail (entities keep failing without any yields between
        them) still aborts — the check runs on every recorded failure, not in a
        consumer's yield loop."""
        from bq_assess.core.scanner import ScanAbortedError

        scanner = _make_scanner()
        client = scanner._client
        client.list_datasets.return_value = [_make_dataset_list_item("ds1")]

        n = 600
        items = [_make_table_list_item("ds1", f"t{i}") for i in range(n)]
        client.list_tables.return_value = items
        good_refs = {id(items[i].reference) for i in range(500)}  # first 500 fine

        err = GoogleAPICallError("quota exhausted")
        err.code = 403  # non-retryable → immediate per-entity failure

        def get_table(ref, **kw):
            if id(ref) in good_refs:
                idx = next(i for i in range(n) if items[i].reference is ref)
                return _make_table("ds1", f"t{idx}")
            raise err

        client.get_table.side_effect = get_table

        with pytest.raises(ScanAbortedError):
            # consume the generator fully — the tail is all failures, so the
            # abort must come from the scanner itself, not the consumer
            list(scanner.scan())
        assert len(scanner.failures) > 0


class TestScanResumeHooks:
    """skip_datasets + on_chunk_complete — the scanner side of checkpoint/resume."""

    def test_skip_datasets_excluded_from_scan(self):
        scanner = _make_scanner()
        client = scanner._client
        client.list_datasets.return_value = [
            _make_dataset_list_item("done_ds"), _make_dataset_list_item("todo_ds"),
        ]
        item = _make_table_list_item("todo_ds", "t1")
        client.list_tables.side_effect = lambda ds, **kw: [item] if ds == "todo_ds" else []
        client.get_table.side_effect = lambda ref, **kw: _make_table("todo_ds", "t1")

        results = list(scanner.scan(skip_datasets={"done_ds"}))
        assert [e.full_name for e in results] == ["todo_ds.t1"]
        listed = [c.args[0] for c in client.list_tables.call_args_list]
        assert "done_ds" not in listed  # skipped datasets are never even listed

    def test_on_chunk_complete_receives_whole_datasets(self):
        """The callback fires after a chunk fully streams, with its dataset ids
        and every entity — the durable-checkpoint consistency point."""
        scanner = _make_scanner()
        client = scanner._client
        client.list_datasets.return_value = [
            _make_dataset_list_item("ds_a"), _make_dataset_list_item("ds_b"),
        ]
        items = {
            "ds_a": [_make_table_list_item("ds_a", "t1")],
            "ds_b": [_make_table_list_item("ds_b", "t2")],
        }
        client.list_tables.side_effect = lambda ds, **kw: items[ds]
        tables = {
            id(items["ds_a"][0].reference): _make_table("ds_a", "t1"),
            id(items["ds_b"][0].reference): _make_table("ds_b", "t2"),
        }
        client.get_table.side_effect = lambda ref, **kw: tables[id(ref)]

        checkpoints = []
        consumed = []
        for e in scanner.scan(
            on_chunk_complete=lambda ds_ids, ents: checkpoints.append(
                (sorted(ds_ids), sorted(x.full_name for x in ents))
            )
        ):
            consumed.append(e.full_name)

        # both datasets fit one 25-dataset chunk → exactly one checkpoint,
        # fired only after every entity in the chunk was yielded to the consumer
        assert checkpoints == [(["ds_a", "ds_b"], ["ds_a.t1", "ds_b.t2"])]
        assert sorted(consumed) == ["ds_a.t1", "ds_b.t2"]
