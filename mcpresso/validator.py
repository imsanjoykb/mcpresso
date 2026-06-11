"""MCPresso Validation Engine — 5-Category Quality Validation.

This module implements the comprehensive quality validation engine that scores
generated MCP servers across five orthogonal quality dimensions. Each category
produces a 0–100 score, and the weighted average determines execution readiness.

Design Decision (for paper):
    The five-category model is structurally inspired by ISO/IEC 25010 software
    quality characteristics, adapted for the specific constraints of MCP servers:
    - Category 1 (Structural Integrity) → Functional Correctness
    - Category 2 (Protocol Compliance) → Interoperability
    - Category 3 (Security Posture) → Security
    - Category 4 (Robustness) → Reliability
    - Category 5 (Documentation) → Maintainability

    All checks use static analysis (AST, regex) rather than dynamic execution,
    making validation safe and deterministic — critical for automated pipelines.

Category weights:
    structural_integrity:   25%
    protocol_compliance:    25%
    security_posture:       20%
    robustness:             20%
    documentation:          10%
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import NamedTuple

from mcpresso.models import (
    CategoryResult,
    ConfidenceLevel,
    Issue,
    IssueSeverity,
    ValidationReport,
)

logger = logging.getLogger(__name__)

CATEGORY_WEIGHTS: dict[str, float] = {
    "structural_integrity": 0.25,
    "protocol_compliance": 0.25,
    "security_posture": 0.20,
    "robustness": 0.20,
    "documentation": 0.10,
}

# Threshold for execution readiness
EXECUTION_READY_THRESHOLD = 75.0

_HARDCODED_SECRET_PATTERNS = [
    # API keys, tokens, passwords embedded in string literals
    re.compile(
        r'(?:api[_-]?key|token|password|secret|passwd|auth)\s*=\s*["\'][^"\'${}]{8,}["\']',
        re.IGNORECASE,
    ),
    # AWS-style keys
    re.compile(r'AKIA[0-9A-Z]{16}'),
    # Generic long hex/base64 that looks like a key
    re.compile(r'["\'][0-9a-fA-F]{32,}["\']'),
]

_DANGEROUS_BUILTINS = {"eval", "exec", "compile", "__import__"}

_PATH_TRAVERSAL_PATTERNS = [
    re.compile(r'\.\./'),
    re.compile(r'os\.path\.join\([^)]*\.\.[^)]*\)'),
]

_MCP_RETURN_TYPES = {
    "TextContent", "ImageContent", "EmbeddedResource",
    "types.TextContent", "types.ImageContent", "types.EmbeddedResource",
}

_MCP_ERROR_CODES = {
    "ErrorCode.InvalidRequest", "ErrorCode.MethodNotFound",
    "ErrorCode.InvalidParams", "ErrorCode.InternalError",
    "McpError",
}

class _CheckResult(NamedTuple):
    """Internal result of a single validation check.

    Attributes:
        passed: Whether the check passed (True) or failed (False).
        name: Human-readable check name.
        issue: Issue to append if the check failed, or None.
    """

    passed: bool
    name: str
    issue: Issue | None = None


def _issue(
    category: str,
    severity: IssueSeverity,
    message: str,
    line_number: int | None = None,
    snippet: str | None = None,
    fix: str | None = None,
) -> Issue:
    return Issue(
        id=str(uuid.uuid4())[:8],
        category=category,
        severity=severity,
        message=message,
        line_number=line_number,
        code_snippet=snippet,
        fix_suggestion=fix,
    )

class MCPValidator:

    def validate(self, source_code: str) -> ValidationReport:
        start_time = time.monotonic()
        source_hash = hashlib.sha256(source_code.encode()).hexdigest()

        # Parse AST once, reuse across all categories
        try:
            tree: ast.Module | None = ast.parse(source_code)
            parse_error: SyntaxError | None = None
        except SyntaxError as exc:
            tree = None
            parse_error = exc

        lines = source_code.splitlines()

        logger.info("Starting validation [source_hash=%s, lines=%d]", source_hash[:8], len(lines))

        # Run all five categories
        cat1 = self._check_structural_integrity(source_code, tree, parse_error, lines)
        cat2 = self._check_protocol_compliance(source_code, tree, lines)
        cat3 = self._check_security_posture(source_code, tree, lines)
        cat4 = self._check_robustness(source_code, tree, lines)
        cat5 = self._check_documentation(source_code, tree, lines)

        category_scores = {
            "structural_integrity": cat1,
            "protocol_compliance": cat2,
            "security_posture": cat3,
            "robustness": cat4,
            "documentation": cat5,
        }

        # Weighted overall score
        overall_score = sum(
            cat.score * CATEGORY_WEIGHTS[key]
            for key, cat in category_scores.items()
        )

        # Collect all issues by severity
        all_issues = [
            issue
            for cat in category_scores.values()
            for issue in cat.issues
        ]
        critical_issues = [i for i in all_issues if i.severity == IssueSeverity.CRITICAL]
        warnings = [i for i in all_issues if i.severity == IssueSeverity.WARNING]
        suggestions = [i for i in all_issues if i.severity == IssueSeverity.SUGGESTION]

        # Confidence level
        if overall_score >= 80:
            confidence = ConfidenceLevel.HIGH
        elif overall_score >= 60:
            confidence = ConfidenceLevel.MEDIUM
        else:
            confidence = ConfidenceLevel.LOW

        execution_ready = overall_score >= EXECUTION_READY_THRESHOLD
        elapsed_ms = (time.monotonic() - start_time) * 1000

        logger.info(
            "Validation complete [score=%.1f, confidence=%s, critical=%d, time=%.1fms]",
            overall_score,
            confidence.value,
            len(critical_issues),
            elapsed_ms,
        )

        return ValidationReport(
            overall_score=overall_score,
            confidence_level=confidence,
            execution_ready=execution_ready,
            category_scores=category_scores,
            critical_issues=critical_issues,
            warnings=warnings,
            suggestions=suggestions,
            validation_time_ms=elapsed_ms,
            source_code_hash=source_hash,
        )
    
    def _check_structural_integrity(
        self,
        source_code: str,
        tree: ast.Module | None,
        parse_error: SyntaxError | None,
        lines: list[str],
    ) -> CategoryResult:
        cat = "structural_integrity"
        checks: list[_CheckResult] = []

        # Check 1: Valid Python syntax
        if tree is None:
            checks.append(_CheckResult(
                passed=False,
                name="valid_python_syntax",
                issue=_issue(
                    cat, IssueSeverity.CRITICAL,
                    f"Python syntax error: {parse_error}",
                    line_number=getattr(parse_error, "lineno", None),
                    fix="Fix the syntax error preventing AST parsing.",
                ),
            ))
            # Cannot continue without valid AST
            return _build_category_result(
                "Structural Integrity", cat, checks,
                CATEGORY_WEIGHTS[cat],
            )
        else:
            checks.append(_CheckResult(passed=True, name="valid_python_syntax"))

        # Check 2: MCP SDK imports
        has_server_import = _has_import(tree, ["mcp.server", "mcp"])
        checks.append(_CheckResult(
            passed=has_server_import,
            name="mcp_sdk_imports",
            issue=None if has_server_import else _issue(
                cat, IssueSeverity.CRITICAL,
                "MCP SDK imports not found. Expected: 'from mcp.server import Server' "
                "and 'import mcp.types as types'",
                fix="Add: from mcp.server import Server\nimport mcp.types as types",
            ),
        ))

        # Check 3: Server instantiation
        has_server = _has_server_instantiation(source_code, tree)
        checks.append(_CheckResult(
            passed=has_server,
            name="server_instantiation",
            issue=None if has_server else _issue(
                cat, IssueSeverity.CRITICAL,
                "No MCP Server instantiation found. Expected: server = Server('name')",
                fix="Add: server = Server('my-server-name')",
            ),
        ))

        # Check 4: list_tools handler
        has_list_tools = _has_decorator(tree, "list_tools")
        checks.append(_CheckResult(
            passed=has_list_tools,
            name="list_tools_handler",
            issue=None if has_list_tools else _issue(
                cat, IssueSeverity.CRITICAL,
                "No @server.list_tools() handler found.",
                fix="Add an async function decorated with @server.list_tools()",
            ),
        ))

        # Check 5: call_tool handler
        has_call_tool = _has_decorator(tree, "call_tool")
        checks.append(_CheckResult(
            passed=has_call_tool,
            name="call_tool_handler",
            issue=None if has_call_tool else _issue(
                cat, IssueSeverity.CRITICAL,
                "No @server.call_tool() handler found.",
                fix="Add an async function decorated with @server.call_tool()",
            ),
        ))

        # Check 6: __main__ entrypoint
        has_main_guard = '__name__ == "__main__"' in source_code or \
                         "__name__ == '__main__'" in source_code
        checks.append(_CheckResult(
            passed=has_main_guard,
            name="main_entrypoint",
            issue=None if has_main_guard else _issue(
                cat, IssueSeverity.WARNING,
                'No if __name__ == "__main__" entrypoint found.',
                fix='Add: if __name__ == "__main__": asyncio.run(main())',
            ),
        ))

        # Check 7: main() function with asyncio
        has_asyncio = "asyncio.run" in source_code or "asyncio.run(" in source_code
        checks.append(_CheckResult(
            passed=has_asyncio,
            name="asyncio_entrypoint",
            issue=None if has_asyncio else _issue(
                cat, IssueSeverity.WARNING,
                "No asyncio.run() call found. Server may not start properly.",
                fix="Add: asyncio.run(main())",
            ),
        ))

        return _build_category_result(
            "Structural Integrity", cat, checks, CATEGORY_WEIGHTS[cat]
        )

    def _check_protocol_compliance(
        self,
        source_code: str,
        tree: ast.Module | None,
        lines: list[str],
    ) -> CategoryResult:
        cat = "protocol_compliance"
        checks: list[_CheckResult] = []

        if tree is None:
            return _build_category_result(
                "Protocol Compliance", cat, [], CATEGORY_WEIGHTS[cat]
            )

        # Check 1: Tool definitions have name, description, inputSchema
        has_input_schema = "inputSchema" in source_code or "input_schema" in source_code
        checks.append(_CheckResult(
            passed=has_input_schema,
            name="tool_input_schema",
            issue=None if has_input_schema else _issue(
                cat, IssueSeverity.CRITICAL,
                "No inputSchema found in tool definitions. All tools must define an inputSchema.",
                fix="Add inputSchema=types.ToolInputSchema(type='object', properties={...}) "
                    "to each types.Tool definition.",
            ),
        ))

        # Check 2: Tool descriptions present
        has_descriptions = bool(re.search(
            r'description\s*=\s*["\'](.{10,})["\']', source_code
        ))
        checks.append(_CheckResult(
            passed=has_descriptions,
            name="tool_descriptions",
            issue=None if has_descriptions else _issue(
                cat, IssueSeverity.WARNING,
                "Tool descriptions appear missing or too short (< 10 chars).",
                fix="Add meaningful description= parameters to all types.Tool objects.",
            ),
        ))

        # Check 3: Async/await patterns
        async_count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        )
        checks.append(_CheckResult(
            passed=async_count >= 2,
            name="async_await_patterns",
            issue=None if async_count >= 2 else _issue(
                cat, IssueSeverity.CRITICAL,
                f"Only {async_count} async function(s) found. MCP handlers must be async.",
                fix="Ensure all tool/resource handlers are declared as 'async def'.",
            ),
        ))

        # Check 4: MCP return types used
        has_return_types = any(rt in source_code for rt in _MCP_RETURN_TYPES)
        checks.append(_CheckResult(
            passed=has_return_types,
            name="mcp_return_types",
            issue=None if has_return_types else _issue(
                cat, IssueSeverity.WARNING,
                "No MCP return types (TextContent, ImageContent) found in handlers.",
                fix="Return types.TextContent(type='text', text=...) from tool handlers.",
            ),
        ))

        # Check 5: McpError for error handling
        has_mcp_error = "McpError" in source_code
        checks.append(_CheckResult(
            passed=has_mcp_error,
            name="mcp_error_handling",
            issue=None if has_mcp_error else _issue(
                cat, IssueSeverity.WARNING,
                "McpError not found. Protocol errors should use McpError with error codes.",
                fix="Import McpError from mcp and raise McpError(ErrorCode.InvalidParams, ...) "
                    "for invalid inputs.",
            ),
        ))

        # Check 6: stdio_server transport
        has_stdio = "stdio_server" in source_code or "stdio" in source_code
        checks.append(_CheckResult(
            passed=has_stdio,
            name="stdio_transport",
            issue=None if has_stdio else _issue(
                cat, IssueSeverity.CRITICAL,
                "No stdio transport found. MCP servers must use stdio_server for transport.",
                fix="Add: from mcp.server.stdio import stdio_server\n"
                    "async with stdio_server() as (read, write): ...",
            ),
        ))

        # Check 7: await used in async functions
        await_nodes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Await)
        ]
        checks.append(_CheckResult(
            passed=len(await_nodes) > 0,
            name="await_usage",
            issue=None if await_nodes else _issue(
                cat, IssueSeverity.WARNING,
                "No 'await' expressions found in async functions.",
                fix="Use await for async operations (httpx, asyncio, etc.).",
            ),
        ))

        return _build_category_result(
            "Protocol Compliance", cat, checks, CATEGORY_WEIGHTS[cat]
        )

    def _check_security_posture(
        self,
        source_code: str,
        tree: ast.Module | None,
        lines: list[str],
    ) -> CategoryResult:
        cat = "security_posture"
        checks: list[_CheckResult] = []

        # Check 1: No hardcoded secrets
        secret_findings: list[tuple[int, str]] = []
        for i, line in enumerate(lines, 1):
            for pattern in _HARDCODED_SECRET_PATTERNS:
                if pattern.search(line):
                    secret_findings.append((i, line.strip()))
                    break

        if secret_findings:
            for line_no, snippet in secret_findings[:3]:  # cap at 3 findings
                checks.append(_CheckResult(
                    passed=False,
                    name="no_hardcoded_secrets",
                    issue=_issue(
                        cat, IssueSeverity.CRITICAL,
                        f"Potential hardcoded secret on line {line_no}.",
                        line_number=line_no,
                        snippet=snippet,
                        fix="Move all secrets to environment variables: "
                            "os.getenv('API_KEY') or os.environ['API_KEY']",
                    ),
                ))
        else:
            checks.append(_CheckResult(passed=True, name="no_hardcoded_secrets"))

        # Check 2: No dangerous builtins
        if tree is not None:
            dangerous_calls: list[tuple[int, str]] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = None
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if name in _DANGEROUS_BUILTINS:
                        dangerous_calls.append(
                            (getattr(node, "lineno", 0), name)
                        )
            has_dangerous = bool(dangerous_calls)
            if has_dangerous:
                for line_no, name in dangerous_calls[:2]:
                    checks.append(_CheckResult(
                        passed=False,
                        name="no_dangerous_builtins",
                        issue=_issue(
                            cat, IssueSeverity.CRITICAL,
                            f"Dangerous builtin '{name}()' call found on line {line_no}.",
                            line_number=line_no,
                            fix=f"Remove or sanitize all uses of {name}(). "
                                "Never execute untrusted input.",
                        ),
                    ))
            else:
                checks.append(_CheckResult(passed=True, name="no_dangerous_builtins"))

        # Check 3: Environment variables for credentials
        uses_env = "os.getenv" in source_code or "os.environ" in source_code
        checks.append(_CheckResult(
            passed=uses_env,
            name="env_var_credentials",
            issue=None if uses_env else _issue(
                cat, IssueSeverity.WARNING,
                "No environment variable access found. API keys and secrets should use "
                "os.getenv() or os.environ.",
                fix="Use os.getenv('MY_API_KEY') for all credentials.",
            ),
        ))

        # Check 4: No path traversal patterns
        traversal_found = False
        for pattern in _PATH_TRAVERSAL_PATTERNS:
            if pattern.search(source_code):
                traversal_found = True
                break
        checks.append(_CheckResult(
            passed=not traversal_found,
            name="no_path_traversal",
            issue=None if not traversal_found else _issue(
                cat, IssueSeverity.CRITICAL,
                "Potential path traversal vulnerability detected (../ in path construction).",
                fix="Validate and sanitize all file paths. Use os.path.abspath() and check "
                    "that the result starts with an allowed base directory.",
            ),
        ))

        # Check 5: Subprocess safety
        has_subprocess = "subprocess" in source_code
        if has_subprocess:
            # Check if shell=True is used (dangerous)
            shell_true = bool(re.search(r'subprocess\.[^(]+\([^)]*shell\s*=\s*True', source_code))
            checks.append(_CheckResult(
                passed=not shell_true,
                name="subprocess_safety",
                issue=None if not shell_true else _issue(
                    cat, IssueSeverity.CRITICAL,
                    "subprocess called with shell=True — command injection risk.",
                    fix="Use shell=False and pass command as a list: "
                        "subprocess.run(['cmd', 'arg1'], shell=False)",
                ),
            ))
            if not shell_true:
                checks.append(_CheckResult(passed=True, name="subprocess_safety"))
        else:
            checks.append(_CheckResult(passed=True, name="no_subprocess"))

        # Check 6: Input validation in tool handlers
        has_validation = (
            "if not" in source_code
            or "isinstance(" in source_code
            or "raise McpError" in source_code
            or "ValueError" in source_code
            or "len(" in source_code
        )
        checks.append(_CheckResult(
            passed=has_validation,
            name="input_validation",
            issue=None if has_validation else _issue(
                cat, IssueSeverity.WARNING,
                "No input validation detected in tool handlers. All tool parameters "
                "should be validated before use.",
                fix="Add input validation: check types, lengths, and formats before "
                    "using tool arguments.",
            ),
        ))

        return _build_category_result(
            "Security Posture", cat, checks, CATEGORY_WEIGHTS[cat]
        )

    def _check_robustness(
        self,
        source_code: str,
        tree: ast.Module | None,
        lines: list[str],
    ) -> CategoryResult:
        cat = "robustness"
        checks: list[_CheckResult] = []

        if tree is None:
            return _build_category_result(
                "Robustness & Reliability", cat, [], CATEGORY_WEIGHTS[cat]
            )

        # Check 1: Try/except blocks present
        try_count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Try))
        checks.append(_CheckResult(
            passed=try_count >= 1,
            name="try_except_blocks",
            issue=None if try_count >= 1 else _issue(
                cat, IssueSeverity.CRITICAL,
                "No try/except blocks found. External calls must be wrapped in try/except.",
                fix="Wrap all HTTP, database, and filesystem calls in try/except blocks.",
            ),
        ))

        # Check 2: No bare except clauses
        bare_excepts: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                bare_excepts.append(getattr(node, "lineno", 0))

        checks.append(_CheckResult(
            passed=len(bare_excepts) == 0,
            name="no_bare_except",
            issue=None if not bare_excepts else _issue(
                cat, IssueSeverity.WARNING,
                f"Bare 'except:' clause(s) found at line(s): {bare_excepts[:3]}. "
                "Always catch specific exception types.",
                line_number=bare_excepts[0] if bare_excepts else None,
                fix="Replace 'except:' with 'except Exception as e:' or more specific types.",
            ),
        ))

        # Check 3: Timeout handling
        # Look for timeout= parameter in httpx/aiohttp calls
        has_timeout = bool(re.search(r'timeout\s*=', source_code))
        # Also accept asyncio.wait_for or httpx.AsyncClient(timeout=...)
        has_timeout = has_timeout or "wait_for" in source_code
        checks.append(_CheckResult(
            passed=has_timeout,
            name="timeout_handling",
            issue=None if has_timeout else _issue(
                cat, IssueSeverity.WARNING,
                "No timeout handling found for network operations.",
                fix="Set timeout parameters: httpx.get(url, timeout=30.0) or "
                    "asyncio.wait_for(coro, timeout=30.0)",
            ),
        ))

        # Check 4: Context managers for resources
        has_context_managers = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.With, ast.AsyncWith))
        )
        checks.append(_CheckResult(
            passed=has_context_managers >= 1,
            name="context_managers",
            issue=None if has_context_managers >= 1 else _issue(
                cat, IssueSeverity.SUGGESTION,
                "No context managers (with/async with) found. Use context managers "
                "for resource cleanup.",
                fix="Use 'async with httpx.AsyncClient() as client:' for HTTP clients.",
            ),
        ))

        # Check 5: HTTP/network libraries present (robustness signal)
        has_http = any(lib in source_code for lib in ["httpx", "aiohttp", "requests"])
        has_db = any(lib in source_code for lib in ["asyncpg", "sqlite3", "sqlalchemy"])
        has_external_io = has_http or has_db or "open(" in source_code

        if has_external_io and try_count == 0:
            checks.append(_CheckResult(
                passed=False,
                name="external_io_error_handling",
                issue=_issue(
                    cat, IssueSeverity.CRITICAL,
                    "External I/O operations found but no error handling present.",
                    fix="Wrap all external I/O in try/except with specific exception types.",
                ),
            ))
        else:
            checks.append(_CheckResult(passed=True, name="external_io_error_handling"))

        # Check 6: Logging on errors
        has_logging = "logging" in source_code and (
            "logger.error" in source_code
            or "logger.exception" in source_code
            or "logging.error" in source_code
        )
        checks.append(_CheckResult(
            passed=has_logging,
            name="error_logging",
            issue=None if has_logging else _issue(
                cat, IssueSeverity.SUGGESTION,
                "No error-level logging found. Errors should be logged for observability.",
                fix="Add logger.error('Message: %s', str(e)) in except blocks.",
            ),
        ))

        return _build_category_result(
            "Robustness & Reliability", cat, checks, CATEGORY_WEIGHTS[cat]
        )

    def _check_documentation(
        self,
        source_code: str,
        tree: ast.Module | None,
        lines: list[str],
    ) -> CategoryResult:
        cat = "documentation"
        checks: list[_CheckResult] = []

        if tree is None:
            return _build_category_result(
                "Documentation & Observability", cat, [], CATEGORY_WEIGHTS[cat]
            )

        # Check 1: Module docstring
        has_module_doc = (
            isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ) if tree.body else False
        checks.append(_CheckResult(
            passed=has_module_doc,
            name="module_docstring",
            issue=None if has_module_doc else _issue(
                cat, IssueSeverity.SUGGESTION,
                "No module-level docstring found.",
                fix='Add a module docstring at the top: """MCP server for ..."""',
            ),
        ))

        # Check 2: Docstrings on async functions
        async_funcs = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        ]
        undocumented = [
            f.name for f in async_funcs
            if ast.get_docstring(f) is None
        ]
        checks.append(_CheckResult(
            passed=len(undocumented) == 0,
            name="function_docstrings",
            issue=None if not undocumented else _issue(
                cat, IssueSeverity.WARNING,
                f"Async functions without docstrings: {undocumented[:5]}.",
                fix="Add Google-style docstrings to all async functions.",
            ),
        ))

        # Check 3: Type hints on function signatures
        funcs_without_hints: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Check return annotation
                has_return_hint = node.returns is not None
                # Check arg annotations (skip self/cls)
                args = [
                    a for a in node.args.args
                    if a.arg not in ("self", "cls")
                ]
                args_with_hints = [a for a in args if a.annotation is not None]
                if not has_return_hint and len(args) > 0 and len(args_with_hints) == 0:
                    funcs_without_hints.append(node.name)

        checks.append(_CheckResult(
            passed=len(funcs_without_hints) == 0,
            name="type_hints",
            issue=None if not funcs_without_hints else _issue(
                cat, IssueSeverity.WARNING,
                f"Functions without type hints: {funcs_without_hints[:5]}.",
                fix="Add type hints to all function parameters and return values.",
            ),
        ))

        # Check 4: Logging module used
        has_logging_import = "import logging" in source_code
        has_logging_usage = "logger" in source_code or "logging." in source_code
        checks.append(_CheckResult(
            passed=has_logging_import and has_logging_usage,
            name="logging_present",
            issue=None if (has_logging_import and has_logging_usage) else _issue(
                cat, IssueSeverity.WARNING,
                "Python logging module not found. All MCP servers should use logging "
                "for observability.",
                fix="Add: import logging; logger = logging.getLogger(__name__)",
            ),
        ))

        # Check 5: Tool descriptions completeness (length heuristic)
        desc_matches = re.findall(r'description\s*=\s*["\'](.+?)["\']', source_code)
        short_descs = [d for d in desc_matches if len(d) < 20]
        checks.append(_CheckResult(
            passed=len(short_descs) == 0,
            name="description_completeness",
            issue=None if not short_descs else _issue(
                cat, IssueSeverity.SUGGESTION,
                f"{len(short_descs)} tool/resource description(s) appear too short (< 20 chars).",
                fix="Provide complete, human-readable descriptions of at least 2 sentences.",
            ),
        ))

        return _build_category_result(
            "Documentation & Observability", cat, checks, CATEGORY_WEIGHTS[cat]
        )

def _build_category_result(
    display_name: str,
    key: str,
    checks: list[_CheckResult],
    weight: float,
) -> CategoryResult:
    if not checks:
        return CategoryResult(
            name=display_name,
            score=0.0,
            weight=weight,
            issues=[],
            passed_checks=[],
            failed_checks=["no_checks_run"],
        )

    passed = [c for c in checks if c.passed]
    failed = [c for c in checks if not c.passed]
    issues = [c.issue for c in failed if c.issue is not None]

    # Base score: fraction of checks passed
    base_score = len(passed) / len(checks) * 100.0

    # Apply critical issue penalty (-5 per critical issue, capped at -30)
    critical_count = sum(1 for i in issues if i.severity == IssueSeverity.CRITICAL)
    penalty = min(critical_count * 5, 30)
    score = max(0.0, base_score - penalty)

    return CategoryResult(
        name=display_name,
        score=score,
        weight=weight,
        issues=issues,
        passed_checks=[c.name for c in passed],
        failed_checks=[c.name for c in failed],
    )

def _has_import(tree: ast.Module, module_prefixes: list[str]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in module_prefixes):
                    return True
        if isinstance(node, ast.ImportFrom):
            if node.module and any(
                node.module.startswith(p) for p in module_prefixes
            ):
                return True
    return False


def _has_server_instantiation(source_code: str, tree: ast.Module) -> bool:
    # Quick regex check first
    if re.search(r'\bServer\s*\(', source_code):
        return True

    # AST check for Server() calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "Server":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "Server":
                return True
    return False

def _has_decorator(tree: ast.Module, decorator_name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    func = dec.func
                    if isinstance(func, ast.Attribute) and func.attr == decorator_name:
                        return True
                    if isinstance(func, ast.Name) and func.id == decorator_name:
                        return True
                if isinstance(dec, ast.Attribute) and dec.attr == decorator_name:
                    return True
                if isinstance(dec, ast.Name) and dec.id == decorator_name:
                    return True
    return False
