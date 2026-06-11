"""MCPresso Confidence Scorer — Composite Quality Score & Execution Readiness.

This module computes a multi-dimensional confidence score that goes beyond the
static validation categories to incorporate dynamic signals: code complexity,
LLM self-consistency, and security posture bonus/penalty.

Design Decision (for paper):
    The four-component composite scorer is inspired by ensemble methods in
    machine learning, where multiple independent estimators vote on a final
    quality signal. The four components are intentionally orthogonal:

    Component 1 — Validation Score (40% weight):
        Direct output of the 5-category static analysis. Measures correctness
        and protocol compliance. Highest weight as it is the most reliable signal.

    Component 2 — Complexity Score (20% weight):
        Derived from code complexity metrics (cyclomatic complexity, LOC, tool
        count). Based on the insight that excessively complex or excessively
        trivial code is a quality risk. Uses radon for cyclomatic complexity.
        Targets a "Goldilocks zone" of complexity appropriate for MCP servers.

    Component 3 — LLM Self-Consistency (20% weight):
        Novel metric: generate the same description twice independently, then
        compute structural similarity (tool names, function signatures, imports).
        High agreement indicates the description has a clear, stable mapping
        to a server structure — a proxy for specification quality.

    Component 4 — Security Posture (20% weight):
        Extracts the security category score from validation but applies a
        non-linear bonus/penalty: servers with zero security critical issues
        receive a +10 bonus; servers with ≥ 2 security critical issues receive
        a -15 penalty. Models the asymmetric risk of security failures.

    This design is documented as "composite ensemble scoring" in Section 4.3
    of the paper.
"""

from __future__ import annotations

import ast
import logging
import re
from difflib import SequenceMatcher

from mcpresso.models import (
    ComplexityMetrics,
    ConfidenceScore,
    ReadinessTier,
    ValidationReport,
)

logger = logging.getLogger(__name__)

VALIDATION_WEIGHT = 0.40
COMPLEXITY_WEIGHT = 0.20
CONSISTENCY_WEIGHT = 0.20
SECURITY_WEIGHT = 0.20

PRODUCTION_READY_MIN = 90.0
STAGING_READY_MIN = 75.0
DEVELOPMENT_ONLY_MIN = 50.0

# Target cyclomatic complexity per function (too low = trivial; too high = risky)
CC_TARGET_MIN = 2.0
CC_TARGET_MAX = 8.0
CC_IDEAL = 4.0

# Target lines of code for a well-scoped MCP server
LOC_TARGET_MIN = 80
LOC_TARGET_MAX = 500
LOC_IDEAL = 200

# Target tool count
TOOL_COUNT_TARGET_MIN = 1
TOOL_COUNT_TARGET_MAX = 10
TOOL_COUNT_IDEAL = 3

class MCPScorer:
    """Composite confidence scorer for generated MCP servers.

    Combines validation results, complexity analysis, LLM self-consistency,
    and security posture into a single composite score that determines
    execution readiness tier.

    Example:
        >>> scorer = MCPScorer()
        >>> score = scorer.compute_score(
        ...     source_code=server_code,
        ...     validation_report=report,
        ...     alternative_code=alt_code,  # second generation for consistency
        ... )
        >>> print(f"Overall: {score.overall_score:.1f} → {score.readiness_tier.value}")
    """

    def compute_score(
        self,
        source_code: str,
        validation_report: ValidationReport,
        alternative_code: str | None = None,
    ) -> ConfidenceScore:
        """Compute the composite confidence score for a generated MCP server.

        Args:
            source_code: Primary generated Python source code.
            validation_report: Completed validation report for the source code.
            alternative_code: Second independent generation of the same server
                              description (used for LLM self-consistency scoring).
                              If None, consistency component uses a default score.

        Returns:
            ConfidenceScore with per-component scores, readiness tier, and
            detailed complexity metrics.
        """
        logger.info(
            "Computing confidence score [validation_score=%.1f]",
            validation_report.overall_score,
        )

        # Component 1: Validation score (direct, normalized to 0-100)
        validation_component = validation_report.overall_score

        # Component 2: Complexity analysis
        complexity_metrics = _compute_complexity_metrics(source_code)
        complexity_component = _score_complexity(complexity_metrics)
        logger.debug("Complexity component: %.1f", complexity_component)

        # Component 3: LLM self-consistency
        if alternative_code is not None:
            consistency_similarity = _compute_structural_similarity(
                source_code, alternative_code
            )
        else:
            # No alternative: assign neutral score (0.70 similarity assumed)
            consistency_similarity = 0.70
        consistency_component = consistency_similarity * 100.0
        logger.debug(
            "Consistency component: %.1f (similarity=%.3f)",
            consistency_component,
            consistency_similarity,
        )

        # Component 4: Security posture (non-linear bonus/penalty)
        security_cat = validation_report.category_scores.get("security_posture")
        base_security_score = security_cat.score if security_cat else 50.0
        security_component = _score_security_posture(
            base_security_score,
            validation_report.critical_issues,
        )
        logger.debug("Security component: %.1f", security_component)

        # Weighted composite
        overall_score = (
            validation_component * VALIDATION_WEIGHT
            + complexity_component * COMPLEXITY_WEIGHT
            + consistency_component * CONSISTENCY_WEIGHT
            + security_component * SECURITY_WEIGHT
        )
        overall_score = max(0.0, min(100.0, overall_score))

        zero_critical = len(validation_report.critical_issues) == 0
        readiness_tier = _assign_readiness_tier(overall_score, zero_critical)

        logger.info(
            "Confidence score computed [overall=%.1f, tier=%s, zero_critical=%s]",
            overall_score,
            readiness_tier.value,
            zero_critical,
        )

        return ConfidenceScore(
            overall_score=overall_score,
            readiness_tier=readiness_tier,
            validation_component=validation_component,
            complexity_component=complexity_component,
            consistency_component=consistency_component,
            security_component=security_component,
            complexity_metrics=complexity_metrics,
            consistency_similarity=consistency_similarity,
            zero_critical_issues=zero_critical,
        )

def _compute_complexity_metrics(source_code: str) -> ComplexityMetrics:
    # Lines of code (non-blank, non-comment)
    loc = sum(
        1 for line in source_code.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    # Try radon for cyclomatic complexity
    avg_cc = _compute_cyclomatic_complexity(source_code)

    # AST-based metrics
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return ComplexityMetrics(
            cyclomatic_complexity=avg_cc,
            lines_of_code=loc,
            tool_count=0,
            resource_count=0,
            function_count=0,
            avg_function_length=0.0,
        )

    # Count functions and their lengths
    function_lengths: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            function_lengths.append(end - node.lineno + 1)

    function_count = len(function_lengths)
    avg_function_length = (
        sum(function_lengths) / function_count if function_count > 0 else 0.0
    )

    # Count tools via decorator pattern
    tool_count = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _has_decorator_name(node, "list_tools")
    )
    # Also count via types.Tool() calls
    tool_calls = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "Tool")
            or (isinstance(node.func, ast.Name) and node.func.id == "Tool")
        )
    )
    tool_count = max(tool_count, tool_calls)

    resource_count = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _has_decorator_name(node, "list_resources")
    )

    return ComplexityMetrics(
        cyclomatic_complexity=avg_cc,
        lines_of_code=loc,
        tool_count=tool_count,
        resource_count=resource_count,
        function_count=function_count,
        avg_function_length=avg_function_length,
    )


def _compute_cyclomatic_complexity(source_code: str) -> float:
    try:
        from radon.complexity import cc_visit

        results = cc_visit(source_code)
        if results:
            return sum(r.complexity for r in results) / len(results)
        return 1.0
    except (ImportError, Exception) as exc:
        logger.debug("radon not available; using AST fallback for CC: %s", exc)
        return _estimate_cc_from_ast(source_code)


def _estimate_cc_from_ast(source_code: str) -> float:
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return 1.0

    branch_nodes = (
        ast.If, ast.For, ast.While, ast.Try, ast.With,
        ast.AsyncFor, ast.AsyncWith, ast.ExceptHandler,
    )
    branch_count = sum(1 for node in ast.walk(tree) if isinstance(node, branch_nodes))
    # Also count boolean operators
    bool_count = sum(
        len(node.values) - 1
        for node in ast.walk(tree)
        if isinstance(node, ast.BoolOp)
    )

    func_count = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )

    total = branch_count + bool_count + func_count
    return (total / func_count) if func_count > 0 else 1.0


def _score_complexity(metrics: ComplexityMetrics) -> float:
    scores: list[float] = []

    # Cyclomatic complexity score
    scores.append(_gaussian_score(metrics.cyclomatic_complexity, CC_IDEAL, sigma=3.0))

    # Lines of code score
    scores.append(_gaussian_score(float(metrics.lines_of_code), float(LOC_IDEAL), sigma=150.0))

    # Tool count score
    scores.append(_gaussian_score(float(metrics.tool_count), float(TOOL_COUNT_IDEAL), sigma=3.0))

    # Bonus: if tool count > 0 (server has actual tools)
    if metrics.tool_count == 0:
        scores.append(0.0)  # penalize servers with no tools
    else:
        scores.append(100.0)

    return (sum(scores) / len(scores)) * 100.0 / 100.0


def _gaussian_score(value: float, ideal: float, sigma: float) -> float:
    import math

    return 100.0 * math.exp(-0.5 * ((value - ideal) / sigma) ** 2)

def _compute_structural_similarity(code_a: str, code_b: str) -> float:
    features_a = _extract_structural_features(code_a)
    features_b = _extract_structural_features(code_b)

    if not features_a and not features_b:
        return 1.0  # Both empty: trivially similar
    if not features_a or not features_b:
        return 0.0  # One empty: completely different

    # Jaccard similarity on feature sets
    set_a = set(features_a)
    set_b = set(features_b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    jaccard = intersection / union if union > 0 else 0.0

    # Sequence similarity on sorted feature lists (order-aware)
    sorted_a = " ".join(sorted(features_a))
    sorted_b = " ".join(sorted(features_b))
    seq_sim = SequenceMatcher(None, sorted_a, sorted_b).ratio()

    # Weighted combination: Jaccard 60%, sequence similarity 40%
    return 0.60 * jaccard + 0.40 * seq_sim


def _extract_structural_features(source_code: str) -> list[str]:
    features: list[str] = []

    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        # Fall back to regex-based extraction
        return _extract_features_regex(source_code)

    # Imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                features.append(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                features.append(f"from:{node.module}")
            for alias in node.names:
                features.append(f"import_name:{alias.name}")

    # Function/class names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            features.append(f"func:{node.name}")
        elif isinstance(node, ast.ClassDef):
            features.append(f"class:{node.name}")

    # Tool names from Tool(name=...) calls
    for m in re.finditer(r'name\s*=\s*["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']', source_code):
        features.append(f"tool:{m.group(1)}")

    return features


def _extract_features_regex(source_code: str) -> list[str]:
    features: list[str] = []

    for m in re.finditer(r'^\s*(?:async\s+)?def\s+(\w+)', source_code, re.MULTILINE):
        features.append(f"func:{m.group(1)}")
    for m in re.finditer(r'^\s*import\s+(\S+)', source_code, re.MULTILINE):
        features.append(f"import:{m.group(1)}")
    for m in re.finditer(r'^\s*from\s+(\S+)\s+import', source_code, re.MULTILINE):
        features.append(f"from:{m.group(1)}")

    return features

def _score_security_posture(
    base_security_score: float,
    all_critical_issues: list,
) -> float:

    security_critical = [
        i for i in all_critical_issues
        if i.category == "security_posture"
    ]

    score = base_security_score

    if len(security_critical) == 0 and base_security_score >= 80:
        # Bonus for clean security posture
        score = min(100.0, score + 10.0)
    elif len(security_critical) >= 2:
        # Penalty for multiple security critical issues
        score = max(0.0, score - 15.0)
    elif len(security_critical) == 1:
        # Moderate penalty for single security issue
        score = max(0.0, score - 5.0)

    return score

def _assign_readiness_tier(
    overall_score: float,
    zero_critical_issues: bool,
) -> ReadinessTier:
    if overall_score >= PRODUCTION_READY_MIN and zero_critical_issues:
        return ReadinessTier.PRODUCTION_READY
    elif overall_score >= STAGING_READY_MIN and zero_critical_issues:
        return ReadinessTier.STAGING_READY
    elif overall_score >= DEVELOPMENT_ONLY_MIN:
        return ReadinessTier.DEVELOPMENT_ONLY
    else:
        return ReadinessTier.NEEDS_REPAIR

def _has_decorator_name(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    name: str,
) -> bool:

    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            func = dec.func
            if isinstance(func, ast.Attribute) and func.attr == name:
                return True
            if isinstance(func, ast.Name) and func.id == name:
                return True
        elif isinstance(dec, ast.Attribute) and dec.attr == name:
            return True
        elif isinstance(dec, ast.Name) and dec.id == name:
            return True
    return False
