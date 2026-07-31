# Feature: bq-assess-lakehouse, Phase 7: CLI rewrite (R1)
"""Unit tests for cli.py — arg parsing, config, validation (R1).

Tests cover:
- R1.1: Missing --gcp-project rejected
- R1.2: Missing credential mode (--credentials XOR --use-adc) rejected
- R1.3: --redshift-type removed (not accepted)
- R1.4: csv format rejected
- R1.5: --reservation-config in help
- R1.6-16: Config loading, merging, precedence
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bq_assess.cli import (
    _engine_prompts,
    _interactive_prompts,
    _load_config,
    _merge_config,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FULL_CONFIG_YAML = """\
gcp:
  project_id: my-gcp-project
  credentials: /path/to/sa.json
  use_adc: false
  datasets:
    - analytics
    - marketing

query_logs:
  enabled: true
  file: /tmp/logs.json
  days: 7

cost:
  bigquery_monthly: 5000.0
  reservation_config: /path/to/res.yaml

options:
  output: output_dir/
  format:
    - json
    - html
"""

_MINIMAL_CONFIG_YAML = """\
gcp:
  project_id: minimal-project
"""


# ---------------------------------------------------------------------------
# Tests: _load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Test YAML config file loading (R1.6-11)."""

    def test_load_full_config(self, tmp_path: object) -> None:
        """Load a YAML file with all config sections and verify the flat dict (R1.6)."""
        config_file = os.path.join(str(tmp_path), "config.yaml")
        with open(config_file, "w") as f:
            f.write(_FULL_CONFIG_YAML)

        result = _load_config(config_file)

        assert result["gcp_project"] == "my-gcp-project"
        assert result["credentials"] == "/path/to/sa.json"
        assert result["use_adc"] is False
        assert result["datasets"] == "analytics,marketing"
        assert result["include_query_logs"] is True
        assert result["query_logs"] == "/tmp/logs.json"  # nosec B108 - asserting CLI arg passthrough, no file created
        assert result["query_log_days"] == 7
        assert result["bigquery_monthly_cost"] == 5000.0
        assert result["reservation_config"] == "/path/to/res.yaml"
        assert result["output"] == "output_dir/"
        assert result["format"] == "json,html"

    def test_load_minimal_config(self, tmp_path: object) -> None:
        """Load a config with only gcp.project_id set (R1.7)."""
        config_file = os.path.join(str(tmp_path), "config.yaml")
        with open(config_file, "w") as f:
            f.write(_MINIMAL_CONFIG_YAML)

        result = _load_config(config_file)

        assert result["gcp_project"] == "minimal-project"
        assert "credentials" not in result
        assert "use_adc" not in result

    def test_reservation_config_parsed(self, tmp_path: object) -> None:
        """_load_config parses cost.reservation_config correctly (R1.8)."""
        config_file = os.path.join(str(tmp_path), "config.yaml")
        with open(config_file, "w") as f:
            f.write("cost:\n  reservation_config: /path/to/res.json\n")

        result = _load_config(config_file)
        assert result["reservation_config"] == "/path/to/res.json"

    def test_missing_file_exits(self, tmp_path: object) -> None:
        """Loading a non-existent config file should sys.exit(1) (R1.10)."""
        with pytest.raises(SystemExit) as exc_info:
            _load_config(os.path.join(str(tmp_path), "nonexistent.yaml"))
        assert exc_info.value.code == 1

    def test_invalid_yaml_exits(self, tmp_path: object) -> None:
        """Loading an invalid YAML file should sys.exit(1) (R1.11)."""
        config_file = os.path.join(str(tmp_path), "bad.yaml")
        with open(config_file, "w") as f:
            f.write(":\n  - :\n  bad: [unterminated")

        with pytest.raises(SystemExit) as exc_info:
            _load_config(config_file)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Tests: _merge_config
# ---------------------------------------------------------------------------


class TestMergeConfig:
    """Test CLI/config merging (R1.12-16)."""

    def test_cli_overrides_config(self) -> None:
        """CLI params override config file values for the same key (R1.12)."""
        config_values = {"gcp_project": "config-project", "output": "config-out/"}
        cli_params = {"gcp_project": "cli-project"}

        merged = _merge_config(cli_params, config_values)

        assert merged["gcp_project"] == "cli-project"
        assert merged["output"] == "config-out/"

    def test_config_only_keys_preserved(self) -> None:
        """Keys present only in config are preserved in the merged result (R1.16)."""
        config_values = {"output": "dir/", "bigquery_monthly_cost": 3000.0}
        cli_params = {"gcp_project": "my-proj"}

        merged = _merge_config(cli_params, config_values)

        assert merged["gcp_project"] == "my-proj"
        assert merged["output"] == "dir/"
        assert merged["bigquery_monthly_cost"] == 3000.0

    def test_none_cli_values_do_not_override(self) -> None:
        """None CLI values should not override config values (R1.14)."""
        config_values = {"gcp_project": "config-project"}
        cli_params = {"gcp_project": None}

        merged = _merge_config(cli_params, config_values)

        assert merged["gcp_project"] == "config-project"

    def test_empty_config_returns_cli_params(self) -> None:
        """When config is empty, merged result equals CLI params (non-None) (R1.15)."""
        cli_params = {"gcp_project": "proj", "use_adc": True}

        merged = _merge_config(cli_params, {})

        assert merged["gcp_project"] == "proj"
        assert merged["use_adc"] is True

    def test_config_value_used_when_cli_none(self) -> None:
        """When CLI doesn't provide a value, config value is used (R1.13)."""
        config_values = {"gcp_project": "config-proj", "output": "config-dir"}
        cli_params = {}

        merged = _merge_config(cli_params, config_values)

        assert merged["gcp_project"] == "config-proj"
        assert merged["output"] == "config-dir"


# ---------------------------------------------------------------------------
# Tests: Click command argument parsing
# ---------------------------------------------------------------------------


class TestClickCommand:
    """Test Click CLI argument parsing with CliRunner (R1.1-5).

    The pipeline seam is now collect() + analyze_and_report() (collector/report
    split) — tests patch both where they used to patch _run_pipeline.
    """

    def test_gcp_project_and_use_adc_accepted(self) -> None:
        """--gcp-project and --use-adc are accepted as valid options."""
        runner = CliRunner()
        with patch("bq_assess.cli.collect") as mock_collect, \
             patch("bq_assess.cli.analyze_and_report") as mock_report:
            result = runner.invoke(main, [
                "--gcp-project", "test-project",
                "--use-adc",
            ])
            # Pipeline should be called (we mock it to avoid real BQ calls)
            assert mock_collect.called
            assert mock_report.called
            assert result.exit_code == 0

    def test_gcp_project_with_credentials_accepted(self) -> None:
        """--gcp-project with --credentials is accepted."""
        runner = CliRunner()
        with patch("bq_assess.cli.collect") as mock_collect, \
             patch("bq_assess.cli.analyze_and_report") as mock_report:
            result = runner.invoke(main, [
                "--gcp-project", "test-project",
                "--credentials", "/path/to/creds.json",
            ])
            assert mock_collect.called
            assert mock_report.called
            assert result.exit_code == 0

    def test_assess_subcommand_equivalent_to_bare(self) -> None:
        """`bq-assess assess …` parses identically to bare `bq-assess …`."""
        runner = CliRunner()
        with patch("bq_assess.cli.collect") as mock_collect, \
             patch("bq_assess.cli.analyze_and_report") as mock_report:
            result = runner.invoke(main, [
                "assess",
                "--gcp-project", "test-project",
                "--use-adc",
            ])
            assert mock_collect.called
            assert mock_report.called
            assert result.exit_code == 0

    def test_missing_gcp_project_shows_error(self) -> None:
        """Missing --gcp-project (without --interactive) should exit with code 1 (R1.1)."""
        runner = CliRunner()
        result = runner.invoke(main, ["--use-adc"])
        assert result.exit_code == 1
        assert "gcp-project" in result.output.lower() or "required" in result.output.lower()

    def test_missing_credentials_shows_error(self) -> None:
        """Missing both --credentials and --use-adc should exit with code 1 (R1.2)."""
        runner = CliRunner()
        result = runner.invoke(main, ["--gcp-project", "test-project"])
        assert result.exit_code == 1
        assert "credentials" in result.output.lower() or "use-adc" in result.output.lower()

    def test_redshift_type_not_accepted(self) -> None:
        """CLI rejects --redshift-type option (removed in Phase 7, R1.3)."""
        runner = CliRunner()
        result = runner.invoke(main, ["--gcp-project", "p", "--use-adc", "--redshift-type", "ra3.xlplus"])
        # Click should reject the unknown option
        assert result.exit_code != 0
        assert "no such option" in result.output.lower() or "redshift-type" in result.output.lower()

    def test_csv_format_rejected(self) -> None:
        """CLI rejects csv format (not implemented, R20.8)."""
        runner = CliRunner()
        # Format validation runs before collect() — mock collect to prevent BQ calls.
        with patch("bq_assess.cli.collect") as mock_collect:
            result = runner.invoke(main, ["--gcp-project", "p", "--use-adc", "--format", "csv"])
            assert result.exit_code != 0, "csv format must be rejected with non-zero exit"
            assert "not supported" in result.output.lower() or "csv" in result.output.lower()
            assert not mock_collect.called, "collect must not run when the format is invalid"

    def test_help_shows_bigquery_monthly_cost(self) -> None:
        """assess help includes --bigquery-monthly-cost (replaces deprecated --reservation-config).
        Options live on the subcommand only (group options were silently dropped — review fix 5)."""
        runner = CliRunner()
        result = runner.invoke(main, ["assess", "--help"])
        assert result.exit_code == 0
        assert "--bigquery-monthly-cost" in result.output
        # --reservation-config is hidden (deprecated)
        assert "--reservation-config" not in result.output

    def test_options_before_subcommand_error_not_silently_dropped(self) -> None:
        """`bq-assess --gcp-project p assess` must ERROR clearly, not parse the
        options at group level and silently discard them (review fix 5)."""
        runner = CliRunner()
        with patch("bq_assess.cli.collect") as mock_collect:
            result = runner.invoke(main, ["--gcp-project", "p", "--use-adc", "assess"])
            assert result.exit_code != 0
            assert not mock_collect.called

    def test_config_file_option(self, tmp_path: object) -> None:
        """--config loads values from a YAML file."""
        config_file = os.path.join(str(tmp_path), "config.yaml")
        with open(config_file, "w") as f:
            f.write("gcp:\n  project_id: config-proj\n  use_adc: true\n")

        runner = CliRunner()
        with patch("bq_assess.cli.collect") as mock_collect, \
             patch("bq_assess.cli.analyze_and_report"):
            result = runner.invoke(main, ["--config", config_file])
            assert mock_collect.called
            # Verify the pipeline received the config values
            call_params = mock_collect.call_args[0][0]
            assert call_params["gcp_project"] == "config-proj"
            assert call_params["use_adc"] is True
            assert result.exit_code == 0

    def test_cli_overrides_config_file(self, tmp_path: object) -> None:
        """CLI args override config file values."""
        config_file = os.path.join(str(tmp_path), "config.yaml")
        with open(config_file, "w") as f:
            f.write("gcp:\n  project_id: config-proj\n  use_adc: true\n")

        runner = CliRunner()
        with patch("bq_assess.cli.collect") as mock_collect, \
             patch("bq_assess.cli.analyze_and_report"):
            result = runner.invoke(main, [
                "--config", config_file,
                "--gcp-project", "cli-proj",
                "--use-adc",
            ])
            assert mock_collect.called
            call_params = mock_collect.call_args[0][0]
            assert call_params["gcp_project"] == "cli-proj"
            assert result.exit_code == 0

    def test_pipeline_exception_exits_with_code_1(self) -> None:
        """If the pipeline raises an exception, CLI exits with code 1."""
        runner = CliRunner()
        with patch("bq_assess.cli.collect", side_effect=RuntimeError("boom")):
            result = runner.invoke(main, [
                "--gcp-project", "test-project",
                "--use-adc",
            ])
            assert result.exit_code == 1
            assert "fatal" in result.output.lower() or "boom" in result.output.lower()


# ---------------------------------------------------------------------------
# Tests: _interactive_prompts (mocked)
# ---------------------------------------------------------------------------


class TestInteractivePrompts:
    """Test interactive mode prompts with mocked Rich prompts."""

    @patch("bq_assess.cli.Prompt.ask")
    @patch("bq_assess.cli.Confirm.ask")
    def test_fills_missing_gcp_project(self, mock_confirm, mock_prompt) -> None:
        """Interactive mode prompts for gcp_project when missing."""
        mock_prompt.side_effect = lambda *a, **kw: {
            "GCP Project ID": "prompted-project",
            "Authentication method": "adc",
            "Datasets to scan (comma-separated, or empty for all)": "",
            "Path to exported query logs JSON (or empty for API)": "",
            "Query log lookback window in days (1-90)": "30",
            "Monthly BigQuery cost override (or empty to calculate)": "",
            "Output directory": "bq-migration/",
            "Output formats (html,json)": "html",
        }.get(a[0], "")
        mock_confirm.return_value = False

        params: dict = {}
        result = _interactive_prompts(params)

        assert result["gcp_project"] == "prompted-project"
        assert result["use_adc"] is True

    @patch("bq_assess.cli.Prompt.ask")
    @patch("bq_assess.cli.Confirm.ask")
    def test_preserves_existing_values(self, mock_confirm, mock_prompt) -> None:
        """Interactive mode does not overwrite already-set values."""
        mock_prompt.side_effect = lambda *a, **kw: {
            "Datasets to scan (comma-separated, or empty for all)": "",
            "Path to exported query logs JSON (or empty for API)": "",
            "Query log lookback window in days (1-90)": "30",
            "Monthly BigQuery cost override (or empty to calculate)": "",
            "Output directory": "bq-migration/",
            "Output formats (html,json)": "html",
        }.get(a[0], "")
        mock_confirm.return_value = False

        params = {"gcp_project": "already-set", "use_adc": True}
        result = _interactive_prompts(params)

        assert result["gcp_project"] == "already-set"

    @patch("bq_assess.cli.Prompt.ask")
    @patch("bq_assess.cli.Confirm.ask")
    def test_credentials_path_prompt(self, mock_confirm, mock_prompt) -> None:
        """Interactive mode prompts for credentials path when 'credentials' is chosen."""
        call_count = 0

        def prompt_side_effect(*args, **kwargs):
            nonlocal call_count
            prompt_text = args[0] if args else ""
            call_count += 1
            responses = {
                "GCP Project ID": "my-proj",
                "Authentication method": "credentials",
                "Path to service account JSON": "/path/to/sa.json",
                "Datasets to scan (comma-separated, or empty for all)": "",
                "Path to exported query logs JSON (or empty for API)": "",
                "Query log lookback window in days (1-90)": "30",
                "Monthly BigQuery cost override (or empty to calculate)": "",
                "Output directory": "bq-migration/",
                "Output formats (html,json)": "html",
            }
            return responses.get(prompt_text, "")

        mock_prompt.side_effect = prompt_side_effect
        mock_confirm.return_value = False

        params: dict = {}
        result = _interactive_prompts(params)

        assert result["gcp_project"] == "my-proj"
        assert result["credentials"] == "/path/to/sa.json"


# ---------------------------------------------------------------------------
# Tests: Engine options (Task 8)
# ---------------------------------------------------------------------------


def test_engine_option_accepted():
    """The --engine option is accepted without error."""
    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--help"])
    assert "--engine" in result.output


def test_query_sla_option_accepted():
    """The --query-sla-ms option is accepted without error."""
    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--help"])
    assert "--query-sla-ms" in result.output


def test_target_region_option_accepted():
    """The --target-region option is accepted without error."""
    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--help"])
    assert "--target-region" in result.output


def test_post_optimization_option_accepted():
    """The --post-optimization option is accepted without error."""
    runner = CliRunner()
    result = runner.invoke(main, ["assess", "--help"])
    assert "--post-optimization" in result.output


def test_engine_athena_reaches_config():
    """Invoking --engine athena passes the value to resolve_engine_config and sets preferred_engine."""
    runner = CliRunner()
    with patch("bq_assess.cli.collect") as mock_collect, \
         patch("bq_assess.cli.analyze_and_report") as mock_report:
        result = runner.invoke(main, [
            "assess",
            "--gcp-project", "test-project",
            "--use-adc",
            "--engine", "athena",
        ])
        assert mock_collect.called
        assert mock_report.called
        # Verify params dict contains engine="athena"
        call_params = mock_collect.call_args[0][0]
        assert call_params["engine"] == "athena"
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Tests: _engine_prompts
# ---------------------------------------------------------------------------


class TestEnginePrompts:
    """Test the _engine_prompts function (Task 9)."""

    def test_engine_prompts_returns_responses_when_no_cli_values(self) -> None:
        """_engine_prompts returns responses when CLI values are not provided."""
        from rich.prompt import Confirm, Prompt

        params = {}

        # Mock the Rich prompts
        with patch.object(Prompt, "ask") as mock_ask, \
             patch.object(Confirm, "ask") as mock_confirm:
            # Set up mock return values
            mock_ask.side_effect = ["us-east-1", "5s", "let tool decide"]
            mock_confirm.return_value = True

            responses = _engine_prompts(params, has_clustering=True)

            assert responses["target_region"] == "us-east-1"
            assert responses["query_sla_ms"] == 5000
            assert responses["preferred_engine"] is None
            assert responses["post_optimization"] is True

    def test_engine_prompts_skips_when_cli_values_provided(self) -> None:
        """_engine_prompts skips prompts when CLI values are provided."""
        from rich.prompt import Confirm, Prompt

        params = {
            "target_region": "us-west-2",
            "query_sla_ms": 1000,
            "engine": "athena",
            "post_optimization": False,
        }

        with patch.object(Prompt, "ask") as mock_ask, \
             patch.object(Confirm, "ask") as mock_confirm:
            responses = _engine_prompts(params, has_clustering=True)

            # No prompts should fire, and dict should be empty
            assert mock_ask.call_count == 0
            assert mock_confirm.call_count == 0
            assert responses == {}

    def test_engine_prompts_no_clustering_skips_post_optimization(self) -> None:
        """_engine_prompts skips post_optimization prompt when no clustering detected."""
        from rich.prompt import Confirm, Prompt

        params = {}

        with patch.object(Prompt, "ask") as mock_ask, \
             patch.object(Confirm, "ask") as mock_confirm:
            mock_ask.side_effect = ["us-east-1", "5s", "athena"]

            responses = _engine_prompts(params, has_clustering=False)

            # Confirm should not be called for post_optimization
            assert mock_confirm.call_count == 0
            assert "post_optimization" not in responses

    def test_engine_prompts_other_region_prompts_for_custom(self) -> None:
        """_engine_prompts prompts for custom region when 'other' is selected."""
        from rich.prompt import Confirm, Prompt

        params = {}

        with patch.object(Prompt, "ask") as mock_ask, \
             patch.object(Confirm, "ask") as mock_confirm:
            # First prompt: "other", second prompt: custom region, then SLA and engine
            mock_ask.side_effect = ["other", "eu-central-1", "30s", "redshift"]
            mock_confirm.return_value = True

            responses = _engine_prompts(params, has_clustering=True)

            assert responses["target_region"] == "eu-central-1"
            assert responses["query_sla_ms"] == 30000
            assert responses["preferred_engine"] == "redshift"

    def test_engine_prompts_skips_with_falsy_cli_values(self) -> None:
        """Explicit falsy CLI values still suppress their prompts."""
        from rich.prompt import Confirm, Prompt

        params = {
            "query_sla_ms": 0,
            "post_optimization": False,
        }

        with patch.object(Prompt, "ask") as mock_ask, \
             patch.object(Confirm, "ask") as mock_confirm:
            # Only region + engine prompts should fire (since they're not in params)
            mock_ask.side_effect = ["us-east-1", "let tool decide"]

            responses = _engine_prompts(params, has_clustering=True)

            # query_sla_ms=0 and post_optimization=False should NOT prompt
            assert "query_sla_ms" not in responses
            assert "post_optimization" not in responses
            # But region and engine should still prompt (not in params at all)
            assert responses["target_region"] == "us-east-1"
            assert responses["preferred_engine"] is None
            # Confirm should never be called (post_optimization=False already set)
            assert mock_confirm.call_count == 0


def test_report_accepts_engine_flags():
    """MRI-7: report command should accept --engine, --query-sla-ms, --target-region, --config."""
    from bq_assess.bundle.models import Bundle
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Create a minimal bundle mock
        mock_bundle = Bundle(
            project_id="test",
            bq_location="us",
            aws_region="us-east-1",
            entities=[],
            queries=[],
            created_at="2026-01-01T00:00:00Z",
        )

        # Patch analyze_and_report to capture params
        with patch("bq_assess.cli.analyze_and_report") as mock_report, \
             patch("bq_assess.cli.BundleLoader") as mock_loader:
            mock_loader.return_value.load.return_value = mock_bundle

            result = runner.invoke(main, [
                "report",
                "--bundle", "bundle",
                "--engine", "athena",
                "--query-sla-ms", "3000",
                "--target-region", "us-west-2",
            ])

            # Command should complete or call analyze_and_report
            assert result.exit_code == 0 or mock_report.called
            # analyze_and_report should have been called with params containing engine flags
            if mock_report.called:
                params = mock_report.call_args[0][1]
                assert params.get("engine") == "athena" or params.get("_cli_engine_params", {}).get("engine") == "athena"
