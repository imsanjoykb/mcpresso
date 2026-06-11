from __future__ import annotations
import pytest
from mcpresso.scorer import (
    MCPScorer,
    _compute_complexity_metrics,
    _compute_structural_similarity,
    _assign_readiness_tier,
    _gaussian_score,
    _score_security_posture,
)
from mcpresso.models import (
    ReadinessTier,
    ValidationReport,
    CategoryResult,
    ConfidenceLevel,
    Issue,
    IssueSeverity,
)
from mcpresso.validator import MCPValidator

GOOD_SERVER = '''"""A well-formed MCP server."""
import asyncio, logging, os
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types
from mcp import McpError

logger = logging.getLogger(__name__)
server = Server("test-server")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """List tools."""
    return [types.Tool(name="ping", description="Ping the server to check it is alive.",
                       inputSchema={"type": "object", "properties": {}})]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Call a tool."""
    try:
        if name == "ping":
            return [types.TextContent(type="text", text="pong")]
        raise McpError(types.ErrorCode.MethodNotFound, f"Unknown: {name}")
    except McpError:
        raise
    except Exception as e:
        logger.error("Tool error: %s", e)
        raise McpError(types.ErrorCode.InternalError, str(e))

async def main() -> None:
    """Run the server."""
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
'''

ALT_SERVER = '''"""Alternative server for consistency testing."""
import asyncio, logging
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

logger = logging.getLogger(__name__)
server = Server("test-server-alt")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """List available tools."""
    return [types.Tool(name="ping", description="Check server liveness.",
                       inputSchema={"type": "object"})]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Execute a tool call."""
    if name == "ping":
        return [types.TextContent(type="text", text="pong")]
    raise Exception(f"Unknown tool: {name}")

async def main() -> None:
    """Entry point."""
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
'''


def _make_mock_report(
    overall: float = 80.0,
    critical_count: int = 0,
    security_score: float = 85.0,
) -> ValidationReport:
    """Create a minimal ValidationReport for testing."""
    cat = CategoryResult(
        name="security_posture",
        score=security_score,
        weight=0.20,
        issues=[],
        passed_checks=["no_hardcoded_secrets"],
        failed_checks=[],
    )
    dummy_cat = CategoryResult(
        name="dummy",
        score=overall,
        weight=0.80,
        issues=[],
        passed_checks=[],
        failed_checks=[],
    )
    critical_issues = [
        Issue(
            id=f"test{i:02d}",
            category="security_posture",
            severity=IssueSeverity.CRITICAL,
            message=f"Critical issue {i}",
        )
        for i in range(critical_count)
    ]
    return ValidationReport(
        overall_score=overall,
        confidence_level=ConfidenceLevel.HIGH if overall >= 80 else ConfidenceLevel.MEDIUM,
        execution_ready=overall >= 75,
        category_scores={"security_posture": cat, "dummy": dummy_cat},
        critical_issues=critical_issues,
        warnings=[],
        suggestions=[],
        validation_time_ms=10.0,
        source_code_hash="a" * 64,
    )

class TestMCPScorer:
    """Unit tests for the MCPScorer class."""

    def setup_method(self):
        self.scorer = MCPScorer()

    def test_returns_confidence_score(self):
        """compute_score must return a ConfidenceScore object."""
        from mcpresso.models import ConfidenceScore
        validator = MCPValidator()
        report = validator.validate(GOOD_SERVER)
        result = self.scorer.compute_score(GOOD_SERVER, report)
        assert isinstance(result, ConfidenceScore)

    def test_overall_score_in_range(self):
        """Overall score must be in [0, 100]."""
        validator = MCPValidator()
        report = validator.validate(GOOD_SERVER)
        result = self.scorer.compute_score(GOOD_SERVER, report)
        assert 0.0 <= result.overall_score <= 100.0

    def test_readiness_tier_is_valid_enum(self):
        """Readiness tier must be a valid ReadinessTier value."""
        validator = MCPValidator()
        report = validator.validate(GOOD_SERVER)
        result = self.scorer.compute_score(GOOD_SERVER, report)
        assert result.readiness_tier in list(ReadinessTier)

    def test_consistency_check_with_alternative(self):
        """Providing alternative code should produce a consistency score."""
        validator = MCPValidator()
        report = validator.validate(GOOD_SERVER)
        result = self.scorer.compute_score(GOOD_SERVER, report, alternative_code=ALT_SERVER)
        # Same server structure → similarity should be reasonably high
        assert result.consistency_similarity > 0.0
        assert result.consistency_component > 0.0

    def test_no_alternative_uses_default_consistency(self):
        """No alternative code → consistency_similarity should be 0.70."""
        validator = MCPValidator()
        report = validator.validate(GOOD_SERVER)
        result = self.scorer.compute_score(GOOD_SERVER, report, alternative_code=None)
        assert result.consistency_similarity == 0.70

    def test_zero_critical_issues_flag(self):
        """zero_critical_issues must reflect validation report."""
        report_clean = _make_mock_report(critical_count=0)
        report_dirty = _make_mock_report(critical_count=2)
        result_clean = self.scorer.compute_score(GOOD_SERVER, report_clean)
        result_dirty = self.scorer.compute_score(GOOD_SERVER, report_dirty)
        assert result_clean.zero_critical_issues is True
        assert result_dirty.zero_critical_issues is False

    def test_production_ready_requires_high_score_and_zero_critical(self):
        """PRODUCTION_READY tier requires score >= 90 AND zero critical issues."""
        tier = _assign_readiness_tier(92.0, zero_critical_issues=True)
        assert tier == ReadinessTier.PRODUCTION_READY

        tier_with_critical = _assign_readiness_tier(92.0, zero_critical_issues=False)
        assert tier_with_critical != ReadinessTier.PRODUCTION_READY

    def test_staging_ready_range(self):
        """STAGING_READY should be assigned for score 75–89 + zero critical."""
        tier = _assign_readiness_tier(80.0, zero_critical_issues=True)
        assert tier == ReadinessTier.STAGING_READY

    def test_development_only_range(self):
        """DEVELOPMENT_ONLY should be assigned for score 50–74."""
        tier = _assign_readiness_tier(60.0, zero_critical_issues=True)
        assert tier == ReadinessTier.DEVELOPMENT_ONLY

    def test_needs_repair_low_score(self):
        """NEEDS_REPAIR for score < 50."""
        tier = _assign_readiness_tier(30.0, zero_critical_issues=True)
        assert tier == ReadinessTier.NEEDS_REPAIR

    def test_complexity_metrics_computed(self):
        """Complexity metrics should be computed for valid Python code."""
        metrics = _compute_complexity_metrics(GOOD_SERVER)
        assert metrics.lines_of_code > 0
        assert metrics.function_count > 0
        assert metrics.cyclomatic_complexity >= 1.0

    def test_complexity_metrics_zero_for_empty(self):
        """Empty code should produce zero LOC."""
        metrics = _compute_complexity_metrics("")
        assert metrics.lines_of_code == 0

    def test_gaussian_score_peak_at_ideal(self):
        """Gaussian score must be 100 at the ideal value."""
        score = _gaussian_score(4.0, ideal=4.0, sigma=2.0)
        assert abs(score - 100.0) < 0.001

    def test_gaussian_score_decreases_with_distance(self):
        """Gaussian score must decrease as value moves away from ideal."""
        score_near = _gaussian_score(5.0, ideal=4.0, sigma=2.0)
        score_far = _gaussian_score(10.0, ideal=4.0, sigma=2.0)
        assert score_near > score_far

    def test_security_bonus_for_clean_security(self):
        """Clean security posture should receive a bonus."""
        score = _score_security_posture(85.0, all_critical_issues=[])
        assert score > 85.0  # bonus applied

    def test_security_penalty_for_multiple_critical(self):
        """Multiple security critical issues should penalize the score."""
        issues = [
            Issue(id="x", category="security_posture",
                  severity=IssueSeverity.CRITICAL, message="test"),
            Issue(id="y", category="security_posture",
                  severity=IssueSeverity.CRITICAL, message="test"),
        ]
        score = _score_security_posture(70.0, all_critical_issues=issues)
        assert score < 70.0


class TestStructuralSimilarity:
    """Tests for the LLM self-consistency / structural similarity computation."""

    def test_identical_code_has_high_similarity(self):
        """Same code compared to itself should have similarity close to 1."""
        sim = _compute_structural_similarity(GOOD_SERVER, GOOD_SERVER)
        assert sim > 0.90

    def test_completely_different_code_has_low_similarity(self):
        """Structurally unrelated code should have lower similarity."""
        code_a = "import os\ndef func_a(): pass\n"
        code_b = "import sys\nclass Foo:\n    def bar(self): return 42\n"
        sim = _compute_structural_similarity(code_a, code_b)
        assert 0.0 <= sim <= 1.0

    def test_similar_servers_have_moderate_similarity(self):
        """Two MCP servers with same structure but different names should be similar."""
        sim = _compute_structural_similarity(GOOD_SERVER, ALT_SERVER)
        assert sim > 0.1  # should have some similarity

    def test_empty_codes_are_identical(self):
        """Two empty strings should have maximum similarity."""
        sim = _compute_structural_similarity("", "")
        assert sim == 1.0

    def test_one_empty_has_zero_similarity(self):
        """One empty, one non-empty → zero similarity."""
        sim = _compute_structural_similarity(GOOD_SERVER, "")
        assert sim == 0.0
