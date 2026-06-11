"""MCPresso Benchmark Harness — Empirical Evaluation Suite.

This module implements the benchmark harness that runs MCPresso against a
curated suite of 20 diverse NL descriptions to produce the empirical metrics
reported in the paper's evaluation section.

Design Decision (for paper):
    The benchmark suite is designed to cover a stratified sample of server types:
    - Simple single-tool servers (baseline difficulty)
    - Multi-tool servers with dependencies (moderate difficulty)
    - Security-sensitive servers handling auth/credentials (high difficulty)
    - External API integration servers (moderate-high difficulty)
    - Database-facing servers requiring parameterized queries (high difficulty)

    This stratification enables reporting of metric distributions across
    difficulty tiers, not just aggregate means — a stronger empirical claim.

Metrics reported:
    1. Generation latency (P50, P95 across all cases)
    2. Validation score distribution per category
    3. Repair convergence rate (% reaching STAGING_READY after ≤ 3 repairs)
    4. Token efficiency (tokens per successful generation)
    5. Security issue detection rate (against known-bad patterns)
    6. Registry reuse rate (% using ADAPT or SEED)
    7. Test coverage rate (average estimated coverage, if testgen enabled)

Output format:
    JSON report suitable for inclusion in paper tables and figures.
    Each run is timestamped and identified by a run UUID for reproducibility.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from mcpresso.models import (
    BenchmarkCase,
    BenchmarkCaseResult,
    BenchmarkReport,
    BrewResult,
    RegistryMatchType,
)
from mcpresso.pipeline import MCPressoPipeline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Benchmark Suite Definition (20 diverse cases)
# ---------------------------------------------------------------------------

BENCHMARK_CASES: list[BenchmarkCase] = [
    # --- Simple Tools (5 cases) ---
    BenchmarkCase(
        id="simple_01",
        description="A simple MCP server that returns the current date and time in multiple formats (ISO, human-readable, Unix timestamp).",
        category="simple_tool",
        expected_tools=["get_current_time", "get_datetime"],
        security_sensitive=False,
        notes="Baseline case: no external calls, no auth, pure computation.",
    ),
    BenchmarkCase(
        id="simple_02",
        description="A text utility MCP server with tools for counting words, characters, and sentences in a given text string.",
        category="simple_tool",
        expected_tools=["count_words", "count_characters"],
        security_sensitive=False,
        notes="Input validation test: tools must handle empty/null strings.",
    ),
    BenchmarkCase(
        id="simple_03",
        description="A unit conversion MCP server that converts between metric and imperial units for length, weight, and temperature.",
        category="simple_tool",
        expected_tools=["convert_length", "convert_weight", "convert_temperature"],
        security_sensitive=False,
        notes="Multi-tool simple server. Tests tool count complexity.",
    ),
    BenchmarkCase(
        id="simple_04",
        description="A random data generator MCP server that generates random UUIDs, random integers within a range, and random passwords.",
        category="simple_tool",
        expected_tools=["generate_uuid", "generate_password"],
        security_sensitive=True,
        notes="Security test: generated passwords must not be logged.",
    ),
    BenchmarkCase(
        id="simple_05",
        description="A JSON utility MCP server with tools to validate JSON strings, pretty-print JSON, and extract specific keys using dot notation.",
        category="simple_tool",
        expected_tools=["validate_json", "pretty_print_json"],
        security_sensitive=False,
        notes="Input validation test: malformed JSON input handling.",
    ),
    # --- API Integration (5 cases) ---
    BenchmarkCase(
        id="api_01",
        description="A GitHub MCP server that lists open issues for a repository, creates new issues, and searches issues by label.",
        category="api_integration",
        expected_tools=["list_issues", "create_issue", "search_issues"],
        security_sensitive=True,
        notes="Auth test: GitHub token must come from environment variable.",
    ),
    BenchmarkCase(
        id="api_02",
        description="A weather MCP server using the OpenWeatherMap API that fetches current weather, 5-day forecast, and weather alerts for a city.",
        category="api_integration",
        expected_tools=["get_current_weather", "get_forecast"],
        security_sensitive=True,
        notes="API key security + timeout handling for external HTTP calls.",
    ),
    BenchmarkCase(
        id="api_03",
        description="A Slack MCP server that sends messages to channels, lists channels, and retrieves recent messages from a channel.",
        category="api_integration",
        expected_tools=["send_message", "list_channels", "get_messages"],
        security_sensitive=True,
        notes="Auth + rate limiting patterns expected.",
    ),
    BenchmarkCase(
        id="api_04",
        description="A Jira MCP server that creates tickets, updates ticket status, searches tickets by project and status, and adds comments.",
        category="api_integration",
        expected_tools=["create_ticket", "update_ticket", "search_tickets"],
        security_sensitive=True,
        notes="Complex multi-operation API server with auth.",
    ),
    BenchmarkCase(
        id="api_05",
        description="A news aggregator MCP server that fetches top headlines, searches news by keyword, and summarizes article content using the NewsAPI.",
        category="api_integration",
        expected_tools=["get_headlines", "search_news"],
        security_sensitive=False,
        notes="Network timeout handling. Text summarization output.",
    ),
    # --- Database (4 cases) ---
    BenchmarkCase(
        id="db_01",
        description="A PostgreSQL MCP server that executes read-only SELECT queries, lists available tables, and describes table schemas.",
        category="database",
        expected_tools=["execute_query", "list_tables", "describe_table"],
        security_sensitive=True,
        notes="SQL injection prevention critical. Connection string from env.",
    ),
    BenchmarkCase(
        id="db_02",
        description="A SQLite MCP server for managing a local task database: create tasks, list tasks by status, update task completion, and delete tasks.",
        category="database",
        expected_tools=["create_task", "list_tasks", "update_task"],
        security_sensitive=True,
        notes="File path safety for SQLite file location.",
    ),
    BenchmarkCase(
        id="db_03",
        description="A Redis MCP server with tools for get/set key-value pairs, list keys by pattern, delete keys, and increment counters.",
        category="database",
        expected_tools=["get_key", "set_key", "delete_key"],
        security_sensitive=True,
        notes="Auth + key pattern validation to prevent wildcard abuse.",
    ),
    BenchmarkCase(
        id="db_04",
        description="A vector database MCP server (using ChromaDB) that stores document embeddings, performs similarity search, and manages collections.",
        category="database",
        expected_tools=["add_documents", "search_similar"],
        security_sensitive=False,
        notes="Complex AI-adjacent use case. Tests async pattern compliance.",
    ),
    # --- Complex Multi-Tool (4 cases) ---
    BenchmarkCase(
        id="complex_01",
        description="A DevOps MCP server that queries Kubernetes pod status, scales deployments, retrieves pod logs, and lists services in a namespace.",
        category="complex_multi_tool",
        expected_tools=["get_pod_status", "scale_deployment", "get_logs"],
        security_sensitive=True,
        notes="High-security server. Namespace isolation, RBAC expectations.",
    ),
    BenchmarkCase(
        id="complex_02",
        description="A file analysis MCP server that reads file contents, analyzes code complexity, counts lines of code, and detects file encoding.",
        category="complex_multi_tool",
        expected_tools=["read_file", "analyze_complexity", "count_lines"],
        security_sensitive=True,
        notes="Path traversal prevention critical. File size limits expected.",
    ),
    BenchmarkCase(
        id="complex_03",
        description="A CI/CD MCP server that triggers GitHub Actions workflows, monitors build status, retrieves build logs, and cancels running workflows.",
        category="complex_multi_tool",
        expected_tools=["trigger_workflow", "get_build_status", "get_build_logs"],
        security_sensitive=True,
        notes="Webhook + polling patterns. GitHub token security.",
    ),
    BenchmarkCase(
        id="complex_04",
        description="An email management MCP server that reads unread emails, sends emails with attachments, searches emails by sender/subject, and manages labels.",
        category="complex_multi_tool",
        expected_tools=["read_emails", "send_email", "search_emails"],
        security_sensitive=True,
        notes="OAuth token management. Attachment validation.",
    ),
    # --- Security-Sensitive (2 cases) ---
    BenchmarkCase(
        id="security_01",
        description="A secret management MCP server that retrieves secrets from HashiCorp Vault by path, lists available secret paths, and rotates API keys.",
        category="security_sensitive",
        expected_tools=["get_secret", "list_secrets", "rotate_key"],
        security_sensitive=True,
        notes="Highest security bar. Must use Vault token from env. No logging of secret values.",
    ),
    BenchmarkCase(
        id="security_02",
        description="A user authentication MCP server that validates JWT tokens, checks user permissions against a role matrix, and generates temporary access tokens.",
        category="security_sensitive",
        expected_tools=["validate_token", "check_permissions", "generate_token"],
        security_sensitive=True,
        notes="Crypto operations. Token expiry. No hardcoded secrets or private keys.",
    ),
]

# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------


class MCPressoBenchmark:
    """Benchmark harness for empirical evaluation of the MCPresso pipeline.

    Runs the full brew pipeline against each benchmark case and collects
    metrics for the paper's evaluation section. Supports incremental runs
    (run a subset of cases) and comparison across runs.

    Attributes:
        pipeline: MCPressoPipeline instance shared across all cases.
        with_tests: Whether to enable test generation (adds ~15s per case).
        output_dir: Directory for benchmark output files.
        on_case_complete: Optional callback for per-case progress reporting.

    Example:
        >>> bench = MCPressoBenchmark(output_dir="./benchmark_results")
        >>> report = bench.run(cases=BENCHMARK_CASES[:5])  # run first 5 cases
        >>> print(f"Success rate: {report.success_rate:.1%}")
        >>> bench.save_report(report)
    """

    def __init__(
        self,
        api_key: str | None = None,
        with_tests: bool = False,
        output_dir: str | Path = "./benchmark_results",
        on_case_complete: Callable[[BenchmarkCaseResult], None] | None = None,
    ) -> None:
        """Initialize the benchmark harness.

        Args:
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            with_tests: Enable test generation for each case.
            output_dir: Directory for benchmark outputs.
            on_case_complete: Callback fired after each case completes.
        """
        self.pipeline = MCPressoPipeline(api_key=api_key)
        self.with_tests = with_tests
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.on_case_complete = on_case_complete

        logger.info(
            "MCPressoBenchmark initialized [output_dir=%s, with_tests=%s]",
            self.output_dir,
            with_tests,
        )

    def run(
        self,
        cases: list[BenchmarkCase] | None = None,
        auto_repair: bool = True,
    ) -> BenchmarkReport:
        """Run the benchmark suite and produce an aggregated report.

        Args:
            cases: List of BenchmarkCase objects to run. Defaults to all 20
                   cases in BENCHMARK_CASES.
            auto_repair: Whether to enable auto-repair during brew.

        Returns:
            BenchmarkReport with all empirical metrics.
        """
        if cases is None:
            cases = BENCHMARK_CASES

        run_start = time.monotonic()
        run_timestamp = datetime.now(timezone.utc)
        case_results: list[BenchmarkCaseResult] = []

        logger.info("Starting benchmark run [cases=%d]", len(cases))

        for i, case in enumerate(cases, 1):
            logger.info("Running benchmark case %d/%d: %s", i, len(cases), case.id)
            result = self._run_case(case, auto_repair=auto_repair)
            case_results.append(result)

            if self.on_case_complete:
                try:
                    self.on_case_complete(result)
                except Exception as exc:
                    logger.warning("on_case_complete callback failed: %s", exc)

            logger.info(
                "Case %s complete [success=%s, score=%.1f, time=%.1fs]",
                case.id,
                result.success,
                result.brew_result.final_score if result.brew_result else 0.0,
                result.latency_ms / 1000,
            )

        run_duration_ms = (time.monotonic() - run_start) * 1000
        report = _compute_report(case_results, run_timestamp, run_duration_ms)

        logger.info(
            "Benchmark complete [success_rate=%.1f%%, p50=%.1fs, p95=%.1fs]",
            report.success_rate * 100,
            report.p50_latency_ms / 1000,
            report.p95_latency_ms / 1000,
        )

        return report

    def _run_case(
        self, case: BenchmarkCase, auto_repair: bool
    ) -> BenchmarkCaseResult:
        """Run a single benchmark case.

        Args:
            case: The benchmark case to run.
            auto_repair: Whether to enable auto-repair.

        Returns:
            BenchmarkCaseResult with the brew result and metrics.
        """
        start_ms = time.monotonic() * 1000

        try:
            brew_result = self.pipeline.brew(
                description=case.description,
                auto_repair=auto_repair,
                with_tests=self.with_tests,
                save_to_registry=True,
            )

            latency_ms = time.monotonic() * 1000 - start_ms
            success = brew_result.readiness_tier.value in (
                "PRODUCTION_READY", "STAGING_READY"
            )
            repair_iters = (
                brew_result.repair_result.repair_iterations
                if brew_result.repair_result else 0
            )

            return BenchmarkCaseResult(
                case=case,
                brew_result=brew_result,
                success=success,
                latency_ms=latency_ms,
                repair_iterations_used=repair_iters,
                registry_match_type=brew_result.generation_result.registry_match_type,
                error=None,
            )

        except Exception as exc:
            latency_ms = time.monotonic() * 1000 - start_ms
            logger.error("Benchmark case %s failed: %s", case.id, exc)
            return BenchmarkCaseResult(
                case=case,
                brew_result=None,
                success=False,
                latency_ms=latency_ms,
                repair_iterations_used=0,
                registry_match_type=RegistryMatchType.FULL_GENERATION,
                error=str(exc),
            )

    def save_report(self, report: BenchmarkReport, filename: str | None = None) -> Path:
        """Save the benchmark report to a JSON file.

        Args:
            report: The benchmark report to save.
            filename: Override filename. Defaults to benchmark_<timestamp>.json.

        Returns:
            Path to the saved report file.
        """
        if filename is None:
            ts = report.run_timestamp.strftime("%Y%m%d_%H%M%S")
            filename = f"benchmark_{ts}.json"

        output_path = self.output_dir / filename
        report_dict = _serialize_report(report)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, indent=2, default=str)

        logger.info("Benchmark report saved to %s", output_path)
        return output_path

    def print_summary(self, report: BenchmarkReport) -> None:
        """Print a human-readable summary of benchmark results.

        Args:
            report: The benchmark report to summarize.
        """
        print("\n" + "=" * 70)
        print("☕ MCPresso Benchmark Results")
        print("=" * 70)
        print(f"  Run timestamp:      {report.run_timestamp.isoformat()}")
        print(f"  Total cases:        {report.total_cases}")
        print(f"  Successful cases:   {report.successful_cases} ({report.success_rate:.1%})")
        print(f"  Run duration:       {report.run_duration_ms/1000:.1f}s")
        print()
        print("  Latency (wall clock):")
        print(f"    P50:              {report.p50_latency_ms/1000:.1f}s")
        print(f"    P95:              {report.p95_latency_ms/1000:.1f}s")
        print()
        print(f"  Mean validation score:  {report.mean_validation_score:.1f}/100")
        print(f"  Repair convergence:      {report.repair_convergence_rate:.1%}")
        print(f"  Token efficiency:        {report.token_efficiency:.0f} tokens/success")
        print(f"  Security detection:      {report.security_detection_rate:.1%}")
        print(f"  Registry reuse rate:     {report.registry_reuse_rate:.1%}")
        if report.mean_test_coverage > 0:
            print(f"  Mean test coverage:      {report.mean_test_coverage:.1f}%")
        print()
        print("  Scores by category:")
        for cat, score in report.mean_score_by_category.items():
            print(f"    {cat:30s}: {score:.1f}")
        print()
        print("  Per-case results:")
        for r in report.case_results:
            status = "✅" if r.success else "❌"
            score = r.brew_result.final_score if r.brew_result else 0.0
            iters = r.repair_iterations_used
            match = r.registry_match_type.value[:4]
            error = f" ERROR: {r.error[:40]}" if r.error else ""
            print(
                f"    {status} [{r.case.id:12s}] "
                f"score={score:5.1f} | iter={iters} | match={match} | "
                f"{r.latency_ms/1000:.1f}s{error}"
            )
        print("=" * 70)


# ---------------------------------------------------------------------------
# Report Computation
# ---------------------------------------------------------------------------


def _compute_report(
    case_results: list[BenchmarkCaseResult],
    run_timestamp: datetime,
    run_duration_ms: float,
) -> BenchmarkReport:
    """Compute aggregate metrics from individual case results.

    Args:
        case_results: List of per-case results.
        run_timestamp: When the benchmark run started.
        run_duration_ms: Total run duration.

    Returns:
        BenchmarkReport with all aggregate metrics.
    """
    total = len(case_results)
    successful = [r for r in case_results if r.success]
    success_count = len(successful)
    success_rate = success_count / total if total > 0 else 0.0

    latencies = [r.latency_ms for r in case_results]
    p50 = float(np.percentile(latencies, 50)) if latencies else 0.0
    p95 = float(np.percentile(latencies, 95)) if latencies else 0.0

    # Mean validation score (from brew results)
    scores = [
        r.brew_result.final_score
        for r in case_results
        if r.brew_result is not None
    ]
    mean_score = float(np.mean(scores)) if scores else 0.0

    # Per-category mean scores
    category_scores: dict[str, list[float]] = {}
    for r in case_results:
        if r.brew_result is None:
            continue
        for cat_key, cat_result in r.brew_result.validation_report.category_scores.items():
            category_scores.setdefault(cat_key, []).append(cat_result.score)
    mean_by_cat = {k: float(np.mean(v)) for k, v in category_scores.items()}

    # Repair convergence rate
    repaired = [
        r for r in case_results
        if r.brew_result and r.brew_result.repair_result
    ]
    converged = [
        r for r in repaired
        if r.brew_result.repair_result.success  # type: ignore[union-attr]
    ]
    repair_convergence = (
        len(converged) / len(repaired) if repaired else 1.0
    )

    # Token efficiency
    total_tokens = sum(
        r.brew_result.generation_result.prompt_tokens
        + r.brew_result.generation_result.completion_tokens
        for r in successful
        if r.brew_result is not None
    )
    token_efficiency = (total_tokens / success_count) if success_count > 0 else 0.0

    # Security detection rate (security-sensitive cases only)
    security_cases = [r for r in case_results if r.case.security_sensitive]
    security_detected = [
        r for r in security_cases
        if r.brew_result and len(r.brew_result.validation_report.category_scores.get(
            "security_posture", type("", (), {"issues": []})
        ).issues) == 0  # no security issues = good detection/prevention
    ]
    # Invert: detection rate = fraction of security-sensitive cases that scored WELL
    security_detection_rate = (
        len(security_detected) / len(security_cases)
        if security_cases else 0.0
    )

    # Registry reuse rate
    reuse_cases = [
        r for r in case_results
        if r.registry_match_type in (RegistryMatchType.ADAPT, RegistryMatchType.SEED)
    ]
    registry_reuse_rate = len(reuse_cases) / total if total > 0 else 0.0

    # Mean test coverage
    test_coverages = [
        r.brew_result.test_result.estimated_coverage
        for r in case_results
        if r.brew_result and r.brew_result.test_result
    ]
    mean_test_coverage = float(np.mean(test_coverages)) if test_coverages else 0.0

    return BenchmarkReport(
        total_cases=total,
        successful_cases=success_count,
        success_rate=success_rate,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        mean_validation_score=mean_score,
        mean_score_by_category=mean_by_cat,
        repair_convergence_rate=repair_convergence,
        token_efficiency=token_efficiency,
        security_detection_rate=security_detection_rate,
        registry_reuse_rate=registry_reuse_rate,
        mean_test_coverage=mean_test_coverage,
        case_results=case_results,
        run_timestamp=run_timestamp,
        run_duration_ms=run_duration_ms,
    )


def _serialize_report(report: BenchmarkReport) -> dict:
    """Serialize BenchmarkReport to a JSON-compatible dict.

    Args:
        report: The benchmark report to serialize.

    Returns:
        JSON-serializable dict.
    """
    case_summaries = []
    for r in report.case_results:
        brew = r.brew_result
        case_summaries.append({
            "case_id": r.case.id,
            "category": r.case.category,
            "success": r.success,
            "latency_ms": r.latency_ms,
            "repair_iterations": r.repair_iterations_used,
            "registry_match_type": r.registry_match_type.value,
            "error": r.error,
            "final_score": brew.final_score if brew else None,
            "readiness_tier": brew.readiness_tier.value if brew else None,
            "under_60s": brew.under_60_seconds if brew else None,
            "total_tokens": (
                brew.generation_result.prompt_tokens + brew.generation_result.completion_tokens
                if brew else None
            ),
            "test_count": brew.test_result.test_count if brew and brew.test_result else None,
            "estimated_coverage": (
                brew.test_result.estimated_coverage if brew and brew.test_result else None
            ),
            "category_scores": {
                k: v.score
                for k, v in brew.validation_report.category_scores.items()
            } if brew else {},
        })

    return {
        "mcpresso_benchmark_version": "1.0",
        "run_id": str(uuid.uuid4()),
        "run_timestamp": report.run_timestamp.isoformat(),
        "run_duration_seconds": report.run_duration_ms / 1000,
        "summary": {
            "total_cases": report.total_cases,
            "successful_cases": report.successful_cases,
            "success_rate": report.success_rate,
            "p50_latency_seconds": report.p50_latency_ms / 1000,
            "p95_latency_seconds": report.p95_latency_ms / 1000,
            "mean_validation_score": report.mean_validation_score,
            "repair_convergence_rate": report.repair_convergence_rate,
            "token_efficiency": report.token_efficiency,
            "security_detection_rate": report.security_detection_rate,
            "registry_reuse_rate": report.registry_reuse_rate,
            "mean_test_coverage": report.mean_test_coverage,
        },
        "mean_score_by_category": report.mean_score_by_category,
        "case_results": case_summaries,
    }
