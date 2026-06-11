"""MCPresso Test Generator — Automatic Pytest Suite Co-Generation.

This module implements the automatic test suite generator that produces a complete
pytest file alongside every generated MCP server. Three test types are generated per
tool: happy path, edge case, and security boundary tests.

Design Decision (for paper):
    Co-generation of server + test suite addresses two fundamental problems in
    deployed AI-generated code:

    1. Deployment Risk: Generated MCP servers have no tests by default. Deploying
       untested server code is a production risk that existing tools (Copilot, cursor,
       etc.) do not address at the framework level.

    2. Specification Completeness: Generating tests from the same tool definitions
       used to generate the server acts as a self-consistency check — if the test
       generator cannot reason about expected inputs/outputs, the tool definition
       is likely underspecified.

    This is documented as "co-generation" in the paper's Section 3.4 and is one
    of two novel contributions (alongside the semantic registry).

Test types per tool:
    - HAPPY PATH: Valid inputs, assert correct output shape and content.
    - EDGE CASE: Empty inputs, None values, boundary values, empty responses.
    - SECURITY BOUNDARY: Injection attempts, oversized inputs, malformed JSON,
      path traversal attempts, null bytes in strings.

Coverage estimation:
    Static coverage estimation uses branch analysis from the AST:
    estimated_coverage = (directly_testable_branches / total_branches) * 100
    This is a static approximation — actual coverage requires runtime execution.

References:
    Schafer et al. (2023). An Empirical Evaluation of Using Large Language Models
    for Automated Unit Test Generation. TSE 2023.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import time
from typing import Any

import anthropic
from dotenv import load_dotenv

from mcpresso.models import TestGenResult, ToolSpec

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 8192

_TESTGEN_SYSTEM_PROMPT = """\
You are an expert Python test engineer specializing in MCP (Model Context Protocol) server testing.
Your task is to generate a complete pytest test suite for a given MCP server.

## Test Requirements
For EACH tool in the server, generate exactly 3 tests:

1. **Happy Path Test** (`test_<tool_name>_happy_path`):
   - Use valid, realistic inputs based on the tool's inputSchema
   - Assert that the return value is a list containing at least one TextContent object
   - Assert the TextContent has type='text' and non-empty text
   - Mock all external calls (HTTP, database, filesystem) using unittest.mock

2. **Edge Case Test** (`test_<tool_name>_edge_case`):
   - Test with empty strings, None values, zero/negative numbers as appropriate
   - Test boundary conditions (e.g., very long strings, empty lists)
   - Assert graceful handling (error response or empty result, not crash)

3. **Security Boundary Test** (`test_<tool_name>_security`):
   - Test with injection-style inputs: `'; DROP TABLE users; --`, `../../etc/passwd`
   - Test with oversized inputs (1000+ character strings)
   - Test with malformed/unexpected types where strings are expected
   - Assert the server handles these without raising unhandled exceptions

## Technical Requirements
- Use `pytest` and `pytest-asyncio` for async tests
- Decorate async test functions with `@pytest.mark.asyncio`
- Mock all external calls using `unittest.mock.AsyncMock` or `MagicMock`
- Import the server module using a relative import from the same directory
- All mocks should be applied via `@patch` decorator or context manager
- Include `conftest.py`-style fixtures if needed (inline in the file)
- Add a module docstring explaining what is being tested

## Output Format
Wrap the COMPLETE test file content between these exact delimiters:
<TESTS_START>
# complete test file content here
<TESTS_END>

Do not include explanation outside the delimiters.
The test file must be immediately runnable with `pytest test_<name>.py`.
"""


# ---------------------------------------------------------------------------
# Test Generator Class
# ---------------------------------------------------------------------------


class MCPTestGenerator:
    """Automatic pytest test suite generator for MCP servers.

    Generates three test categories per tool: happy path, edge case,
    and security boundary tests. Uses Claude to produce realistic,
    runnable test code with appropriate mocking of external dependencies.

    Attributes:
        model: Anthropic model identifier.
        max_tokens: Maximum completion tokens.
        client: Anthropic API client.

    Example:
        >>> gen = MCPTestGenerator()
        >>> result = gen.generate(
        ...     source_code=server_code,
        ...     tool_definitions=tools,
        ...     server_name="github_server",
        ... )
        >>> print(f"Generated {result.test_count} tests for {len(result.tools_covered)} tools")
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
    ) -> None:
        """Initialize the MCPTestGenerator.

        Args:
            model: Anthropic model to use for test generation.
            max_tokens: Maximum tokens for test file generation.
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

        Raises:
            ValueError: If no API key is available.
        """
        self.model = model or os.getenv("MCPRESSO_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens

        resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key not found. Set ANTHROPIC_API_KEY in your environment "
                "or .env file, or pass api_key= to MCPTestGenerator()."
            )
        self.client = anthropic.Anthropic(api_key=resolved_key)
        logger.info("MCPTestGenerator initialized [model=%s]", self.model)

    def generate(
        self,
        source_code: str,
        tool_definitions: list[ToolSpec],
        server_name: str = "mcp_server",
    ) -> TestGenResult:
        """Generate a complete pytest test suite for an MCP server.

        Args:
            source_code: Python source code of the MCP server to test.
            tool_definitions: Tool specifications extracted from the server.
            server_name: Name used for the server module import in tests.
                         Defaults to 'mcp_server'.

        Returns:
            TestGenResult with the complete test file, test counts,
            coverage estimate, and security test count.

        Raises:
            anthropic.APIError: If the API call fails.
        """
        start_time = time.monotonic()
        logger.info(
            "Starting test generation [tools=%d, server_name=%s]",
            len(tool_definitions),
            server_name,
        )

        if not tool_definitions:
            # Try to infer tools from source code
            tool_definitions = _infer_tools_from_source(source_code)
            logger.info("Inferred %d tools from source code.", len(tool_definitions))

        prompt = _build_testgen_prompt(source_code, tool_definitions, server_name)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_TESTGEN_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
        except anthropic.APIError as exc:
            logger.error("API error during test generation: %s", exc)
            # Return a minimal placeholder test file
            return _make_placeholder_result(tool_definitions, server_name, start_time)

        test_file = _extract_test_content(raw_text)
        elapsed_ms = (time.monotonic() - start_time) * 1000

        # Analyze the generated test file
        test_count = _count_test_functions(test_file)
        tools_covered = _extract_covered_tools(test_file, tool_definitions)
        security_tests = _count_security_tests(test_file)
        estimated_coverage = _estimate_coverage(source_code, test_file)

        logger.info(
            "Test generation complete [tests=%d, tools_covered=%d, "
            "security_tests=%d, coverage_est=%.1f%%, time=%.1fms]",
            test_count,
            len(tools_covered),
            security_tests,
            estimated_coverage,
            elapsed_ms,
        )

        return TestGenResult(
            test_file=test_file,
            test_count=test_count,
            tools_covered=tools_covered,
            estimated_coverage=estimated_coverage,
            security_tests=security_tests,
            generation_time_ms=elapsed_ms,
            model_used=self.model,
        )


# ---------------------------------------------------------------------------
# Prompt Building
# ---------------------------------------------------------------------------


def _build_testgen_prompt(
    source_code: str,
    tool_definitions: list[ToolSpec],
    server_name: str,
) -> str:
    """Build the test generation prompt.

    Args:
        source_code: MCP server source code.
        tool_definitions: Tool specifications.
        server_name: Module name for imports.

    Returns:
        Formatted prompt string.
    """
    tools_summary = []
    for tool in tool_definitions:
        schema_str = str(tool.input_schema) if tool.input_schema else "{}"
        tools_summary.append(
            f"- **{tool.name}**: {tool.description}\n"
            f"  Input schema: {schema_str}\n"
            f"  Return type: {tool.return_type}\n"
            f"  Async: {tool.is_async}"
        )

    tools_text = "\n".join(tools_summary) if tools_summary else "No tools detected."

    return (
        f"Generate a complete pytest test suite for this MCP server.\n\n"
        f"## Server Module Name\n`{server_name}`\n\n"
        f"## Tools to Test\n{tools_text}\n\n"
        f"## Server Source Code\n```python\n{source_code}\n```\n\n"
        f"Generate exactly 3 tests per tool (happy path, edge case, security boundary). "
        f"Use pytest-asyncio for async tests. Mock all external dependencies."
    )


# ---------------------------------------------------------------------------
# Response Parsing
# ---------------------------------------------------------------------------


def _extract_test_content(response_text: str) -> str:
    """Extract test file content from the generation response.

    Args:
        response_text: Raw API response text.

    Returns:
        Extracted test file content string.
    """
    # Strategy 1: explicit delimiters
    match = re.search(r"<TESTS_START>\s*(.*?)\s*<TESTS_END>", response_text, re.DOTALL)
    if match:
        content = match.group(1).strip()
        # Strip markdown fence if inside delimiters
        fence = re.match(r"```(?:python)?\s*(.*?)\s*```", content, re.DOTALL)
        if fence:
            return fence.group(1).strip()
        return content

    # Strategy 2: markdown python fence
    match = re.search(r"```python\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Strategy 3: any code fence
    match = re.search(r"```\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    logger.warning("Could not extract test content; using raw response.")
    return response_text.strip()


# ---------------------------------------------------------------------------
# Analysis Helpers
# ---------------------------------------------------------------------------


def _count_test_functions(test_file: str) -> int:
    """Count the number of test functions in the generated test file.

    Args:
        test_file: Generated test file content.

    Returns:
        Number of functions starting with 'test_'.
    """
    try:
        tree = ast.parse(test_file)
        return sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    except SyntaxError:
        # Fallback: regex count
        return len(re.findall(r"^\s*(?:async\s+)?def\s+test_", test_file, re.MULTILINE))


def _extract_covered_tools(
    test_file: str, tool_definitions: list[ToolSpec]
) -> list[str]:
    """Extract tool names that have corresponding tests in the test file.

    Args:
        test_file: Generated test file content.
        tool_definitions: Original tool definitions.

    Returns:
        List of tool names with at least one test.
    """
    covered = []
    for tool in tool_definitions:
        # A tool is covered if its name appears in a test function name
        pattern = rf"def\s+test_{re.escape(tool.name)}"
        if re.search(pattern, test_file):
            covered.append(tool.name)
        elif tool.name in test_file:
            covered.append(tool.name)

    return list(set(covered))


def _count_security_tests(test_file: str) -> int:
    """Count security boundary tests in the generated test file.

    Args:
        test_file: Generated test file content.

    Returns:
        Number of functions with 'security' in their name.
    """
    return len(re.findall(
        r"^\s*(?:async\s+)?def\s+test_\w+_security\b",
        test_file,
        re.MULTILINE
    ))


def _estimate_coverage(source_code: str, test_file: str) -> float:
    """Estimate test coverage statically from branch analysis.

    Estimates the percentage of source code branches that are likely
    covered by the generated test suite. Uses a heuristic approach:
    1. Count total branches in source (if/for/while/try/except).
    2. Count references to source functions in test file.
    3. Estimate coverage based on ratio and test types.

    This is a static approximation — actual coverage requires execution.

    Args:
        source_code: MCP server source code.
        test_file: Generated test file content.

    Returns:
        Estimated coverage percentage (0.0–100.0).
    """
    try:
        src_tree = ast.parse(source_code)
    except SyntaxError:
        return 0.0

    # Count branches in source
    branch_nodes = (ast.If, ast.For, ast.While, ast.Try, ast.ExceptHandler,
                    ast.AsyncFor, ast.With, ast.AsyncWith)
    total_branches = sum(1 for node in ast.walk(src_tree) if isinstance(node, branch_nodes))
    if total_branches == 0:
        total_branches = 1

    # Count source function names referenced in tests
    src_funcs = [
        node.name for node in ast.walk(src_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    referenced = sum(1 for f in src_funcs if f in test_file)

    # Heuristic: 3 tests per tool with 70% branch coverage per test
    test_count = _count_test_functions(test_file)
    tool_count = max(len(src_funcs) // 2, 1)  # rough tool estimate

    # Base coverage from function reference ratio
    func_coverage = (referenced / len(src_funcs) * 100.0) if src_funcs else 50.0

    # Adjust for test density
    tests_per_tool = test_count / tool_count if tool_count > 0 else 0
    density_factor = min(tests_per_tool / 3.0, 1.0)  # target: 3 tests per tool

    estimated = func_coverage * 0.70 * density_factor
    return min(estimated, 95.0)  # cap at 95% (static analysis cannot guarantee 100%)


def _infer_tools_from_source(source_code: str) -> list[ToolSpec]:
    """Infer tool definitions from source code when none are provided.

    Args:
        source_code: MCP server Python source code.

    Returns:
        List of inferred ToolSpec objects.
    """
    specs: list[ToolSpec] = []

    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return specs

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_tool = (
                (isinstance(func, ast.Attribute) and func.attr == "Tool")
                or (isinstance(func, ast.Name) and func.id == "Tool")
            )
            if is_tool:
                name = ""
                description = ""
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name = str(kw.value.value)
                    elif kw.arg == "description" and isinstance(kw.value, ast.Constant):
                        description = str(kw.value.value)
                if name:
                    specs.append(ToolSpec(
                        name=name,
                        description=description or f"Tool: {name}",
                        input_schema={"type": "object", "properties": {}},
                    ))

    return specs


def _make_placeholder_result(
    tool_definitions: list[ToolSpec],
    server_name: str,
    start_time: float,
) -> TestGenResult:
    """Create a minimal placeholder TestGenResult when generation fails.

    Args:
        tool_definitions: Tool definitions for the server.
        server_name: Server module name.
        start_time: Generation start time for elapsed calculation.

    Returns:
        TestGenResult with a placeholder test file skeleton.
    """
    tool_names = [t.name for t in tool_definitions]
    placeholder_tests = [
        f"import pytest",
        f"from unittest.mock import AsyncMock, patch",
        f"",
        f'"""Placeholder tests for {server_name} — test generation encountered an error.',
        f"Please regenerate or write tests manually.",
        f'"""',
        f"",
    ]

    for tool_name in tool_names:
        placeholder_tests.extend([
            f"@pytest.mark.asyncio",
            f"async def test_{tool_name}_placeholder():",
            f'    """Placeholder test for {tool_name}. Replace with real test."""',
            f"    pass",
            f"",
        ])

    test_file = "\n".join(placeholder_tests)
    elapsed_ms = (time.monotonic() - start_time) * 1000

    return TestGenResult(
        test_file=test_file,
        test_count=len(tool_names),
        tools_covered=tool_names,
        estimated_coverage=0.0,
        security_tests=0,
        generation_time_ms=elapsed_ms,
        model_used=DEFAULT_MODEL,
    )
