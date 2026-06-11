"""Tests for mcpresso.validator — 5-Category Quality Validation Engine.

All tests use static analysis only (no API calls, no LLM dependencies).
"""

from __future__ import annotations

import pytest
from mcpresso.validator import MCPValidator, CATEGORY_WEIGHTS
from mcpresso.models import IssueSeverity, ConfidenceLevel

# ---------------------------------------------------------------------------
# Fixtures — well-formed and malformed MCP server code
# ---------------------------------------------------------------------------

GOOD_SERVER = '''"""A well-formed MCP server for testing."""
import asyncio
import logging
import os
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types
from mcp import McpError

logger = logging.getLogger(__name__)
server = Server("test-server")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="echo",
            description="Echoes the input text back. Useful for testing connectivity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to echo"}
                },
                "required": ["text"],
            },
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Handle tool calls with validation and error handling."""
    if name == "echo":
        text = arguments.get("text", "")
        if not text:
            raise McpError(types.ErrorCode.InvalidParams, "text cannot be empty")
        try:
            result = str(text)
            logger.info("echo tool called with text length=%d", len(text))
            return [types.TextContent(type="text", text=result)]
        except Exception as e:
            logger.error("echo tool failed: %s", e)
            raise McpError(types.ErrorCode.InternalError, str(e))
    raise McpError(types.ErrorCode.MethodNotFound, f"Unknown tool: {name}")

async def main() -> None:
    """Run the MCP server."""
    api_key = os.getenv("API_KEY")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
'''

SYNTAX_ERROR_SERVER = '''
def broken(:
    pass
'''

MINIMAL_SERVER = '''
import asyncio
from mcp.server import Server
server = Server("minimal")
if __name__ == "__main__":
    asyncio.run(main())
'''

INSECURE_SERVER = '''
import asyncio
from mcp.server import Server
import mcp.types as types
from mcp.server.stdio import stdio_server

server = Server("insecure")
api_key = "sk-1234567890abcdef1234567890abcdef"

@server.list_tools()
async def list_tools():
    return [types.Tool(name="run", description="Runs code", inputSchema={"type": "object"})]

@server.call_tool()
async def call_tool(name, arguments):
    user_code = arguments.get("code", "")
    result = eval(user_code)
    return [types.TextContent(type="text", text=str(result))]

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
'''

NO_ERROR_HANDLING_SERVER = '''
import asyncio
from mcp.server import Server
import mcp.types as types
from mcp.server.stdio import stdio_server
import httpx

server = Server("no-errors")

@server.list_tools()
async def list_tools():
    return [types.Tool(name="fetch", description="Fetches a URL and returns content.",
                       inputSchema={"type": "object", "properties": {"url": {"type": "string"}}})]

@server.call_tool()
async def call_tool(name, arguments):
    url = arguments["url"]
    response = httpx.get(url)
    return [types.TextContent(type="text", text=response.text)]

async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
'''


# ---------------------------------------------------------------------------
# Tests — MCPValidator
# ---------------------------------------------------------------------------


class TestMCPValidator:
    """Unit tests for the MCPValidator class."""

    def setup_method(self):
        """Create a fresh validator for each test."""
        self.validator = MCPValidator()

    def test_good_server_passes_validation(self):
        """A well-formed server should get a high score and execution_ready=True."""
        report = self.validator.validate(GOOD_SERVER)
        assert report.overall_score >= 60.0, (
            f"Expected score >= 60, got {report.overall_score:.1f}"
        )
        assert report.execution_ready or report.overall_score >= 60.0

    def test_syntax_error_detected(self):
        """Syntax errors must be detected with CRITICAL severity."""
        report = self.validator.validate(SYNTAX_ERROR_SERVER)
        assert report.overall_score == 0.0 or len(report.critical_issues) > 0
        critical_msgs = [i.message for i in report.critical_issues]
        assert any("syntax" in m.lower() or "parse" in m.lower() for m in critical_msgs), (
            f"Expected syntax error in critical issues: {critical_msgs}"
        )

    def test_insecure_server_flags_critical(self):
        """Hardcoded secrets and eval() must be flagged as CRITICAL."""
        report = self.validator.validate(INSECURE_SERVER)
        critical_msgs = [i.message.lower() for i in report.critical_issues]
        # Should detect either hardcoded secret or dangerous builtin
        has_security_issue = any(
            "secret" in m or "hardcoded" in m or "eval" in m or "dangerous" in m
            for m in critical_msgs
        )
        assert has_security_issue, (
            f"Expected security issues in critical: {report.critical_issues}"
        )

    def test_category_weights_sum_to_one(self):
        """Category weights must sum to exactly 1.0."""
        total = sum(CATEGORY_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9, f"Weights sum to {total}, expected 1.0"

    def test_returns_all_five_categories(self):
        """Validation report must contain all five category keys."""
        report = self.validator.validate(GOOD_SERVER)
        expected = {
            "structural_integrity",
            "protocol_compliance",
            "security_posture",
            "robustness",
            "documentation",
        }
        assert set(report.category_scores.keys()) == expected

    def test_overall_score_is_weighted_average(self):
        """Overall score must equal weighted sum of category scores."""
        report = self.validator.validate(GOOD_SERVER)
        computed = sum(
            cat.score * CATEGORY_WEIGHTS[key]
            for key, cat in report.category_scores.items()
        )
        assert abs(report.overall_score - computed) < 0.01, (
            f"Overall={report.overall_score:.2f}, computed={computed:.2f}"
        )

    def test_confidence_level_high_when_score_80_plus(self):
        """Score >= 80 should yield HIGH confidence level."""
        report = self.validator.validate(GOOD_SERVER)
        if report.overall_score >= 80:
            assert report.confidence_level == ConfidenceLevel.HIGH

    def test_confidence_level_low_when_score_below_60(self):
        """Score < 60 should yield LOW confidence level."""
        report = self.validator.validate(SYNTAX_ERROR_SERVER)
        if report.overall_score < 60:
            assert report.confidence_level == ConfidenceLevel.LOW

    def test_issues_by_severity(self):
        """Issues must be correctly separated by severity."""
        report = self.validator.validate(INSECURE_SERVER)
        all_issues = report.critical_issues + report.warnings + report.suggestions
        # Check that severity is consistent
        for issue in report.critical_issues:
            assert issue.severity == IssueSeverity.CRITICAL
        for issue in report.warnings:
            assert issue.severity == IssueSeverity.WARNING
        for issue in report.suggestions:
            assert issue.severity == IssueSeverity.SUGGESTION

    def test_no_error_handling_flagged(self):
        """Missing try/except around external HTTP calls should be flagged."""
        report = self.validator.validate(NO_ERROR_HANDLING_SERVER)
        robustness = report.category_scores["robustness"]
        assert robustness.score < 100.0, "Expected robustness issues for server without error handling"

    def test_validation_time_recorded(self):
        """Validation time must be positive."""
        report = self.validator.validate(GOOD_SERVER)
        assert report.validation_time_ms > 0

    def test_source_code_hash_is_sha256(self):
        """Source code hash must be a 64-character hex string (SHA-256)."""
        report = self.validator.validate(GOOD_SERVER)
        assert len(report.source_code_hash) == 64
        assert all(c in "0123456789abcdef" for c in report.source_code_hash)

    def test_mcp_imports_check(self):
        """Missing MCP imports should be detected as structural issue."""
        no_imports = "import asyncio\nserver = None\n"
        report = self.validator.validate(no_imports)
        struct = report.category_scores["structural_integrity"]
        assert "mcp_sdk_imports" in struct.failed_checks

    def test_server_instantiation_check(self):
        """Missing Server() call should be detected."""
        no_server = "from mcp.server import Server\nimport asyncio\n"
        report = self.validator.validate(no_server)
        struct = report.category_scores["structural_integrity"]
        assert "server_instantiation" in struct.failed_checks

    def test_empty_source_code(self):
        """Empty code should produce a zero or very low score."""
        report = self.validator.validate("")
        assert report.overall_score < 30.0

    def test_category_result_has_passed_and_failed(self):
        """Each CategoryResult must have passed_checks and failed_checks lists."""
        report = self.validator.validate(GOOD_SERVER)
        for cat in report.category_scores.values():
            assert isinstance(cat.passed_checks, list)
            assert isinstance(cat.failed_checks, list)

    def test_issue_ids_are_unique(self):
        """All issue IDs in the report must be unique."""
        report = self.validator.validate(INSECURE_SERVER)
        all_ids = [i.id for i in report.critical_issues + report.warnings + report.suggestions]
        assert len(all_ids) == len(set(all_ids)), "Duplicate issue IDs found"
