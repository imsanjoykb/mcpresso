"""MCPresso Data Models — All shared dataclasses and type definitions.

This module defines the complete data model hierarchy for MCPresso, providing
strongly-typed structures for every stage of the brew pipeline:
generation → validation → repair → scoring → registry → test generation.

Design Decision (for paper):
    All inter-module communication uses immutable dataclasses rather than dicts,
    enabling static type checking, IDE autocompletion, and clear API contracts.
    This is critical for the "separation of concerns" architectural pattern
    described in the paper's design section.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

class ReadinessTier(str, Enum):

    PRODUCTION_READY = "PRODUCTION_READY"
    STAGING_READY = "STAGING_READY"
    DEVELOPMENT_ONLY = "DEVELOPMENT_ONLY"
    NEEDS_REPAIR = "NEEDS_REPAIR"


class ConfidenceLevel(str, Enum):
    """Human-readable confidence level derived from overall validation score.

    Attributes:
        HIGH: Overall score ≥ 80.
        MEDIUM: Overall score 60–79.
        LOW: Overall score < 60.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IssueSeverity(str, Enum):

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    SUGGESTION = "SUGGESTION"


class RegistryMatchType(str, Enum):
    """How a registry lookup resolved for a new brew request."""

    ADAPT = "ADAPT"
    SEED = "SEED"
    FULL_GENERATION = "FULL_GENERATION"

@dataclass
class ToolSpec:
    """Specification of a single MCP tool extracted from generated code. """

    name: str
    description: str
    input_schema: dict[str, Any]
    return_type: str = "TextContent"
    is_async: bool = True


@dataclass
class ResourceSpec:
    """Specification of a single MCP resource extracted from generated code.

    Attributes:
        uri_pattern: URI template for the resource (e.g., 'github://issues/{id}').
        name: Human-readable resource name.
        description: Human-readable description of resource purpose.
        mime_type: MIME type of resource content (default: 'text/plain').
    """

    uri_pattern: str
    name: str
    description: str
    mime_type: str = "text/plain"

@dataclass
class GenerationResult:
    """Result of the NL-to-MCP generation step.

    This is the primary output of ``generator.py`` and feeds directly into
    the validation pipeline.    seed_server_id: Registry entry ID used as seed, if applicable.
    """

    source_code: str
    tool_definitions: list[ToolSpec]
    resource_definitions: list[ResourceSpec]
    generation_time_ms: float
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    description: str = ""
    registry_match_type: RegistryMatchType = RegistryMatchType.FULL_GENERATION
    seed_server_id: str | None = None

@dataclass
class Issue:
    """A single validation finding (critical issue, warning, or suggestion).

    Attributes:
        id: Unique identifier for cross-referencing in repair audit trail.
        category: Validation category name (e.g., 'structural_integrity').
        severity: Issue severity level.
        message: Human-readable description of the issue.
        line_number: Source code line number where issue was detected, if any.
        code_snippet: Relevant code snippet for context.
        fix_suggestion: Suggested remediation, if available.
    """

    id: str
    category: str
    severity: IssueSeverity
    message: str
    line_number: int | None = None
    code_snippet: str | None = None
    fix_suggestion: str | None = None


@dataclass
class CategoryResult:
    """Validation result for one of the five quality categories.

    Attributes:
        name: Category name (e.g., 'Structural Integrity').
        score: Category score from 0.0 to 100.0.
        weight: Relative weight in overall score calculation.
        issues: All issues found in this category.
        passed_checks: List of check names that passed.
        failed_checks: List of check names that failed.
    """

    name: str
    score: float
    weight: float
    issues: list[Issue]
    passed_checks: list[str]
    failed_checks: list[str]


@dataclass
class ValidationReport:
    """Comprehensive validation report covering all five quality categories.

    Design Decision (for paper):
        The five-category scoring model is inspired by software quality models
        (ISO/IEC 25010). Each category maps to a distinct axis of server quality:
        correctness (structural), interoperability (protocol), security posture,
        reliability, and maintainability (documentation).

    Attributes:
        overall_score: Weighted average across all five categories (0–100).
        confidence_level: HIGH/MEDIUM/LOW derived from score.
        execution_ready: True if overall_score >= 75.
        category_scores: Per-category validation results keyed by category name.
        critical_issues: All CRITICAL severity issues across categories.
        warnings: All WARNING severity issues across categories.
        suggestions: All SUGGESTION severity issues across categories.
        validation_time_ms: Wall-clock time for the validation run.
        source_code_hash: SHA-256 hash of validated source for audit trail.
    """

    overall_score: float
    confidence_level: ConfidenceLevel
    execution_ready: bool
    category_scores: dict[str, CategoryResult]
    critical_issues: list[Issue]
    warnings: list[Issue]
    suggestions: list[Issue]
    validation_time_ms: float
    source_code_hash: str = ""

@dataclass
class FixRecord:
    """Audit record for a single automated fix applied during repair.

    Attributes:
        issue_id: References the Issue.id that triggered this fix.
        description: Human-readable explanation of what was changed and why.
        before_snippet: Code snippet before the fix.
        after_snippet: Code snippet after the fix.
        category: Validation category the fix addressed.
    """

    issue_id: str
    description: str
    before_snippet: str
    after_snippet: str
    category: str


@dataclass
class RepairResult:
    """Result of the automated repair process.

    Design Decision (for paper):
        The repair engine implements iterative refinement — a form of
        agentic self-correction where the LLM evaluates its own output,
        identifies deficiencies, and generates improved versions.
        Capped at 3 iterations to bound API cost in production use.
 """

    repaired_code: str
    fixes_applied: list[FixRecord]
    repair_iterations: int
    initial_score: float
    final_score: float
    final_report: ValidationReport
    success: bool
    remaining_issues: list[Issue]

@dataclass
class ComplexityMetrics:
    """Code complexity metrics used as input to the confidence scorer."""

    cyclomatic_complexity: float
    lines_of_code: int
    tool_count: int
    resource_count: int
    function_count: int
    avg_function_length: float


@dataclass
class ConfidenceScore:
    """Composite confidence score combining multiple quality signals.

    Design Decision (for paper):
        The four-component composite scorer mirrors ensemble methods in ML:
        validation quality (40%), complexity/scope (20%), LLM self-consistency
        (20%), and security posture (20%). The LLM self-consistency component
        is novel — generating twice and comparing structural similarity adds an
        empirical reliability signal beyond static analysis.

    Attributes:
        overall_score: Weighted composite score (0–100).
        readiness_tier: Assigned execution readiness tier.
        validation_component: Score from validation (40% weight).
        complexity_component: Score from complexity analysis (20% weight).
        consistency_component: Score from LLM self-consistency (20% weight).
        security_component: Score from security posture (20% weight).
        complexity_metrics: Detailed complexity breakdown.
        consistency_similarity: Raw cosine similarity between dual generations.
        zero_critical_issues: Whether validation found zero critical issues.
    """

    overall_score: float
    readiness_tier: ReadinessTier
    validation_component: float
    complexity_component: float
    consistency_component: float
    security_component: float
    complexity_metrics: ComplexityMetrics
    consistency_similarity: float
    zero_critical_issues: bool

@dataclass
class RegistryEntry:
    """A registry record for a successfully brewed MCP server.

    Design Decision (for paper):
        The registry implements "semantic memory" for the generation system —
        akin to episodic memory in cognitive architectures. By storing
        embeddings of descriptions alongside source code, MCPresso can
        perform nearest-neighbor retrieval to ground new generations in
        previously validated solutions. This is the key contribution enabling
        the empirical reuse-rate metric.

    Attributes:
        id: UUID v4 identifier for this registry entry.
        description: Original NL prompt that produced this server.
        embedding: Dense vector representation (sentence-transformers).
        source_code: Final validated/repaired source code.
        validation_score: Overall validation score at time of registration.
        readiness_tier: Execution readiness tier at time of registration.
        tags: Auto-extracted semantic tags (e.g., ["github", "api", "search"]).
        created_at: UTC timestamp of registry entry creation.
        brew_time_ms: Total wall-clock time for the brew pipeline.
        tool_names: Names of tools defined in this server.
        repair_iterations: How many repair passes were needed.
    """

    id: str
    description: str
    embedding: list[float]
    source_code: str
    validation_score: float
    readiness_tier: str
    tags: list[str]
    created_at: datetime
    brew_time_ms: float
    tool_names: list[str] = field(default_factory=list)
    repair_iterations: int = 0


@dataclass
class RegistrySearchResult:
    """Result of a semantic registry search.

    Attributes:
        entry: The matching registry entry.
        similarity: Cosine similarity score (0.0–1.0).
        match_type: How this match will be used (ADAPT/SEED/FULL_GENERATION).
    """

    entry: RegistryEntry
    similarity: float
    match_type: RegistryMatchType

@dataclass
class TestGenResult:
    """Result of the automatic test suite generation.

    Design Decision (for paper):
        Co-generation of servers and test suites addresses the "untested AI
        code" deployment risk. By generating tests in the same pass as the
        server (using the same tool definitions), we can compute estimated
        branch coverage statically — a metric novel to the MCP ecosystem.

    Attributes:
        test_file: Complete pytest file content ready to run.
        test_count: Total number of test functions generated.
        tools_covered: List of tool names with generated tests.
        estimated_coverage: Static coverage estimate based on branch analysis.
        security_tests: Number of security boundary tests generated.
        generation_time_ms: Wall-clock time for test generation.
        model_used: Anthropic model used for test generation.
    """

    test_file: str
    test_count: int
    tools_covered: list[str]
    estimated_coverage: float
    security_tests: int
    generation_time_ms: float
    model_used: str

@dataclass
class BrewResult:
    """Complete result of the end-to-end MCPresso brew pipeline.

    This is the top-level output returned from ``MCPressoPipeline.brew()``
    and contains a full audit trail of every pipeline stage.

    Attributes:
        description: Original NL description provided by the user.
        output_path: File path where the server was written, if any.
        source_code: Final server source code (after repair if applicable).
        generation_result: Full generation stage output.
        validation_report: Validation report (pre-repair).
        repair_result: Repair stage output, if auto_repair was enabled.
        confidence_score: Final composite confidence score.
        test_result: Test suite generation result, if --with-tests was used.
        final_score: Final overall quality score (0–100).
        readiness_tier: Final execution readiness classification.
        total_time_ms: Total wall-clock time for the entire pipeline.
        under_60_seconds: Whether the pipeline completed in under 60 seconds.
        brew_id: Unique identifier for this brew run (UUID v4).
        created_at: UTC timestamp of brew completion.
        registry_entry_id: ID of registry entry if this brew was saved.
    """

    description: str
    output_path: str | None
    source_code: str
    generation_result: GenerationResult
    validation_report: ValidationReport
    repair_result: RepairResult | None
    confidence_score: ConfidenceScore
    test_result: TestGenResult | None
    final_score: float
    readiness_tier: ReadinessTier
    total_time_ms: float
    under_60_seconds: bool
    brew_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.utcnow)
    registry_entry_id: str | None = None

@dataclass
class BenchmarkCase:
    """A single test case in the benchmark suite.

    Attributes:
        id: Unique case identifier.
        description: NL description to brew.
        category: Category label (e.g., 'simple_tool', 'complex_multi_tool').
        expected_tools: Tool names expected to appear in the generated server.
        security_sensitive: Whether this case tests security-sensitive patterns.
        notes: Additional notes for paper documentation.
    """

    id: str
    description: str
    category: str
    expected_tools: list[str]
    security_sensitive: bool = False
    notes: str = ""


@dataclass
class BenchmarkCaseResult:
    """Result for a single benchmark case run.

    Attributes:
        case: The benchmark case definition.
        brew_result: Full brew pipeline result.
        success: Whether the brew reached STAGING_READY or better.
        latency_ms: Total brew time for this case.
        repair_iterations_used: How many repair passes were needed.
        registry_match_type: How the registry was used.
        error: Any unhandled exception message, if the brew failed.
    """

    case: BenchmarkCase
    brew_result: BrewResult | None
    success: bool
    latency_ms: float
    repair_iterations_used: int
    registry_match_type: RegistryMatchType
    error: str | None = None


@dataclass
class BenchmarkReport:
    """Aggregated benchmark report across all test cases.

    These metrics form the empirical basis for the paper's evaluation section.

    Attributes:
        total_cases: Total number of benchmark cases run.
        successful_cases: Cases that reached STAGING_READY or better.
        success_rate: successful_cases / total_cases.
        p50_latency_ms: Median generation latency.
        p95_latency_ms: 95th percentile generation latency.
        mean_validation_score: Average validation score across cases.
        mean_score_by_category: Per-category mean scores.
        repair_convergence_rate: % of servers reaching STAGING_READY after ≤ 3 repairs.
        token_efficiency: Mean tokens per successful generation.
        security_detection_rate: % of security issues correctly flagged.
        registry_reuse_rate: % of brews using ADAPT or SEED.
        mean_test_coverage: Average estimated test coverage (if testgen enabled).
        case_results: Per-case detailed results.
        run_timestamp: UTC timestamp of benchmark run.
        run_duration_ms: Total time to complete all benchmark cases.
    """

    total_cases: int
    successful_cases: int
    success_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    mean_validation_score: float
    mean_score_by_category: dict[str, float]
    repair_convergence_rate: float
    token_efficiency: float
    security_detection_rate: float
    registry_reuse_rate: float
    mean_test_coverage: float
    case_results: list[BenchmarkCaseResult]
    run_timestamp: datetime
    run_duration_ms: float

@dataclass
class ClientGenResult:
    """Result of the automatic client script generation step.

    Produced by ``clientgen.MCPClientGenerator.generate()`` — a deterministic,
    zero-LLM-cost companion to every brewed server.

    Attributes:
        client_file: Complete Python source code of the generated client script.
        tool_call_count: Number of tool calls included in the client.
        tools_covered: Tool names for which call blocks were generated.
        example_args: Mapping of tool name → inferred example arguments dict.
        generation_time_ms: Wall-clock time for client synthesis (typically < 10ms).
        server_file: Server filename referenced inside the client script.
    """

    client_file: str
    tool_call_count: int
    tools_covered: list[str]
    example_args: dict[str, Any]
    generation_time_ms: float
    server_file: str
