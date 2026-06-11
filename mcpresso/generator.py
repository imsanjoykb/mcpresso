"""MCPresso Generation Engine — NL-to-MCP Server Generation.

This module implements the core generation engine that transforms plain English
descriptions into complete, runnable MCP server Python files using the
Anthropic Claude API.

Design Decision (for paper):
    The generator uses a structured prompt engineering strategy combining:
    1. A system prompt encoding MCP SDK patterns, security best practices,
       and code quality standards.
    2. Optional few-shot grounding via registry seed servers.
    3. Output parsing via explicit XML-like delimiters to reliably extract
       code from the model response without fragile regex heuristics.
    This approach is documented as "retrieval-augmented code generation"
    in the paper's method section.
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

from mcpresso.models import (
    GenerationResult,
    RegistryMatchType,
    ResourceSpec,
    ToolSpec,
)

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 8192

# System prompt encoding MCP SDK patterns and quality standards.
# Kept as a module-level constant for testability and prompt versioning.
_SYSTEM_PROMPT = """\
You are an expert MCP (Model Context Protocol) server engineer. Your task is to
generate a complete, production-grade MCP server in Python using the official `mcp` SDK.

## Required Imports (use EXACTLY these — no substitutions)
```python
import asyncio
import logging
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
```

## Required Structure
Every server you generate MUST include:
1. The exact imports listed above
2. Server instantiation: `server = Server("server-name")`
3. Tool handlers decorated with `@server.list_tools()` and `@server.call_tool()`
4. Resource handlers (if needed): `@server.list_resources()` and `@server.read_resource()`
5. Async/await throughout — all handlers must be `async def`
6. A `main()` async function with this EXACT pattern:
```python
async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="your-server-name",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                )
            )
        )
if __name__ == "__main__":
    asyncio.run(main())
```

## Error Handling (use EXACTLY this pattern)
```python
from mcp.shared.exceptions import McpError
# McpError takes ONE argument: an ErrorData object — ALWAYS use this form:
raise McpError(types.ErrorData(code=types.INVALID_PARAMS,   message="bad input"))
raise McpError(types.ErrorData(code=types.METHOD_NOT_FOUND, message="unknown tool"))
raise McpError(types.ErrorData(code=types.INTERNAL_ERROR,   message="server error"))
# NEVER use: McpError(code, message)  — that form does NOT exist in this SDK version
# NEVER use: types.McpError, types.ErrorCode, mcp.McpError
```

## Quality Requirements
- All tool input parameters validated with proper error handling
- Environment variables for all API keys and secrets (never hardcode)
- Try/except around ALL external calls (HTTP, DB, filesystem)
- Timeout handling for network operations (use `httpx` with timeout parameter)
- Google-style docstrings on every function
- Type hints on every function signature
- Python `logging` module for observability
- No use of `eval()`, `exec()`, or `subprocess` without sanitization
- No bare `except:` clauses — always catch specific exceptions

## Tool Schema Requirements
Each tool MUST have:
- `name`: lowercase_with_underscores identifier
- `description`: clear, human-readable explanation (2+ sentences)
- `inputSchema`: valid JSON Schema with all parameters documented

## Output Format
Wrap your complete Python code between these exact delimiters:
<CODE_START>
# your complete server code here
<CODE_END>

Do not include any explanation outside the delimiters. The code must be immediately
runnable with `python server.py` after installing dependencies.
"""

_ADAPT_SYSTEM_PROMPT = """\
You are an expert MCP (Model Context Protocol) server engineer. You will be given
an existing MCP server and asked to adapt it for a new purpose.

Preserve the overall structure and quality patterns of the existing server.
Only modify the parts that are specific to the new functionality.
Keep all security patterns, error handling, and documentation standards.

## Output Format
Wrap your complete adapted Python code between these exact delimiters:
<CODE_START>
# your complete adapted server code here
<CODE_END>

Do not include any explanation outside the delimiters.
"""


# ---------------------------------------------------------------------------
# Generator Class
# ---------------------------------------------------------------------------


class MCPGenerator:
    """NL-to-MCP generation engine backed by the Anthropic Claude API.

    This class is responsible for the "grind" phase of the brew pipeline —
    transforming a natural language description into structured Python source code
    implementing a compliant MCP server.

    Attributes:
        model: Anthropic model identifier to use for generation.
        max_tokens: Maximum tokens to generate in the completion.
        client: Anthropic API client instance.

    Example:
        >>> generator = MCPGenerator()
        >>> result = await generator.generate(
        ...     "A server that fetches GitHub issues and summarizes them"
        ... )
        >>> print(f"Generated {len(result.tool_definitions)} tools")
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
    ) -> None:
        """Initialize the MCPGenerator.

        Args:
            model: Anthropic model to use. Defaults to claude-sonnet-4-20250514.
            max_tokens: Maximum tokens for generation. Defaults to 8192.
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

        Raises:
            ValueError: If no API key is found in args or environment.
        """
        self.model = model or os.getenv("MCPRESSO_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key not found. Set ANTHROPIC_API_KEY in your environment "
                "or .env file, or pass api_key= to MCPGenerator()."
            )
        self.client = anthropic.Anthropic(api_key=resolved_key)
        logger.info("MCPGenerator initialized with model=%s", self.model)

    async def generate(
        self,
        description: str,
        seed_server_code: str | None = None,
        seed_server_id: str | None = None,
        match_type: RegistryMatchType = RegistryMatchType.FULL_GENERATION,
    ) -> GenerationResult:
        """Generate a complete MCP server from a natural language description.

        This is the primary entry point for server generation. Depending on the
        ``match_type``, the generation will either adapt an existing server,
        use one as a few-shot seed, or start from scratch.

        Args:
            description: Plain English description of the server's purpose.
            seed_server_code: Source code of a similar server from registry.
            seed_server_id: Registry ID of the seed server, for audit trail.
            match_type: Registry resolution type (ADAPT/SEED/FULL_GENERATION).

        Returns:
            GenerationResult containing source code, tool/resource specs,
            timing info, and token usage metrics.

        Raises:
            anthropic.APIError: If the Anthropic API call fails.
            ValueError: If the model response cannot be parsed.
        """
        logger.info(
            "Starting generation [match_type=%s, description_len=%d]",
            match_type.value,
            len(description),
        )
        start_time = time.monotonic()

        if match_type == RegistryMatchType.ADAPT and seed_server_code:
            source_code, usage = await self._adapt_server(description, seed_server_code)
        elif match_type == RegistryMatchType.SEED and seed_server_code:
            source_code, usage = await self._generate_with_seed(description, seed_server_code)
        else:
            source_code, usage = await self._generate_fresh(description)

        elapsed_ms = (time.monotonic() - start_time) * 1000
        logger.info(
            "Generation complete in %.1fms [prompt_tokens=%d, completion_tokens=%d]",
            elapsed_ms,
            usage.input_tokens,
            usage.output_tokens,
        )

        tool_definitions = _extract_tool_specs(source_code)
        resource_definitions = _extract_resource_specs(source_code)

        return GenerationResult(
            source_code=source_code,
            tool_definitions=tool_definitions,
            resource_definitions=resource_definitions,
            generation_time_ms=elapsed_ms,
            model_used=self.model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            description=description,
            registry_match_type=match_type,
            seed_server_id=seed_server_id,
        )

    async def _generate_fresh(self, description: str) -> tuple[str, Any]:
        """Generate a server from scratch without any registry seed.

        Args:
            description: Plain English description of the server purpose.

        Returns:
            Tuple of (source_code, usage_object).
        """
        user_prompt = (
            f"Generate a complete MCP server for the following purpose:\n\n{description}\n\n"
            "Remember to follow all quality requirements in the system prompt."
        )
        return await self._call_api(_SYSTEM_PROMPT, user_prompt)

    async def _generate_with_seed(
        self, description: str, seed_code: str
    ) -> tuple[str, Any]:
        """Generate a server using an existing server as a reference example.

        This implements few-shot grounding — passing the seed as a reference
        example to improve consistency with validated patterns.

        Args:
            description: Plain English description of the new server.
            seed_code: Reference server code from the registry.

        Returns:
            Tuple of (source_code, usage_object).
        """
        user_prompt = (
            f"Here is an example of a high-quality MCP server for reference:\n\n"
            f"```python\n{seed_code}\n```\n\n"
            f"Using this as a reference for style and structure, generate a NEW and DIFFERENT "
            f"MCP server for the following purpose:\n\n{description}\n\n"
            "Do NOT copy the reference server. Use it only to guide the code quality, "
            "structure, and patterns."
        )
        return await self._call_api(_SYSTEM_PROMPT, user_prompt)

    async def _adapt_server(
        self, description: str, existing_code: str
    ) -> tuple[str, Any]:
        """Adapt an existing server to a new but similar purpose.

        When registry similarity > 0.85, full regeneration is wasteful.
        This method asks the model to minimally modify an existing server
        to match the new description, preserving all validated patterns.

        Args:
            description: New purpose description.
            existing_code: Existing server code from the registry.

        Returns:
            Tuple of (source_code, usage_object).
        """
        user_prompt = (
            f"Here is an existing MCP server:\n\n"
            f"```python\n{existing_code}\n```\n\n"
            f"Adapt this server for the following new purpose, making only the minimal "
            f"necessary changes while preserving all quality patterns:\n\n{description}"
        )
        return await self._call_api(_ADAPT_SYSTEM_PROMPT, user_prompt)

    async def _call_api(self, system_prompt: str, user_prompt: str) -> tuple[str, Any]:
        """Make an Anthropic API call and extract the code block from the response.

        Args:
            system_prompt: System-level instructions for the model.
            user_prompt: User message with the generation request.

        Returns:
            Tuple of (extracted_source_code, usage_object).

        Raises:
            ValueError: If no code block delimiters are found in the response.
        """
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw_text = response.content[0].text
        source_code = _extract_code_block(raw_text)
        return source_code, response.usage


# ---------------------------------------------------------------------------
# Parsing Helpers
# ---------------------------------------------------------------------------


def _extract_code_block(response_text: str) -> str:
    """Extract Python source code from a model response with delimiters.

    Attempts multiple extraction strategies in order:
    1. Explicit <CODE_START> / <CODE_END> delimiters (preferred).
    2. Markdown python code fences (```python ... ```).
    3. Any markdown code fence (``` ... ```).
    4. The entire response text as fallback.

    Args:
        response_text: Raw text response from the Anthropic API.

    Returns:
        Cleaned Python source code string.

    Raises:
        ValueError: If no recognizable code block is found and the text
            does not appear to be valid Python.
    """
    # Strategy 1: explicit delimiters
    match = re.search(r"<CODE_START>\s*(.*?)\s*<CODE_END>", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Strategy 2: markdown python fence
    match = re.search(r"```python\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Strategy 3: any markdown fence
    match = re.search(r"```\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Strategy 4: treat full response as code (last resort)
    stripped = response_text.strip()
    try:
        ast.parse(stripped)
        logger.warning("No code delimiters found; treating full response as code.")
        return stripped
    except SyntaxError as exc:
        raise ValueError(
            f"Could not extract valid Python code from model response. "
            f"Response preview: {response_text[:200]!r}"
        ) from exc


def _extract_tool_specs(source_code: str) -> list[ToolSpec]:
    """Extract MCP tool specifications from generated Python source code using AST.

    Parses the source code's abstract syntax tree to find tool definitions
    indicated by ``@server.list_tools()`` and ``@server.call_tool()`` decorators,
    then reconstructs ToolSpec objects from the function signatures and docstrings.

    Args:
        source_code: Python source code of a generated MCP server.

    Returns:
        List of ToolSpec objects for each discovered tool handler.
    """
    specs: list[ToolSpec] = []
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        logger.warning("Could not parse source code for tool extraction (SyntaxError).")
        return specs

    # Extract tool names from list_tools handler return values
    tool_names = _find_tool_names_from_ast(tree, source_code)

    # Extract async function definitions that look like call_tool handlers
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            # Check if decorated with @server.call_tool() or similar
            has_call_tool = any(
                _decorator_matches(dec, ["call_tool"])
                for dec in node.decorator_list
            )
            if has_call_tool:
                docstring = ast.get_docstring(node) or "No description available."
                # Try to infer the tool name from first argument default or body
                spec = ToolSpec(
                    name=node.name,
                    description=docstring.split("\n")[0],
                    input_schema={"type": "object", "properties": {}},
                    return_type="list[types.TextContent]",
                    is_async=True,
                )
                if spec not in specs:
                    specs.append(spec)

    # If we found tool names from list_tools but no call_tool handlers,
    # synthesize specs with names only
    if not specs and tool_names:
        for name in tool_names:
            specs.append(
                ToolSpec(
                    name=name,
                    description=f"Tool: {name}",
                    input_schema={"type": "object", "properties": {}},
                )
            )

    logger.debug("Extracted %d tool specs from source code.", len(specs))
    return specs


def _extract_resource_specs(source_code: str) -> list[ResourceSpec]:
    """Extract MCP resource specifications from generated Python source code.

    Args:
        source_code: Python source code of a generated MCP server.

    Returns:
        List of ResourceSpec objects for each discovered resource handler.
    """
    specs: list[ResourceSpec] = []
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return specs

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            has_resource = any(
                _decorator_matches(dec, ["list_resources", "read_resource"])
                for dec in node.decorator_list
            )
            if has_resource:
                docstring = ast.get_docstring(node) or "Resource handler."
                spec = ResourceSpec(
                    uri_pattern=f"resource://{node.name}",
                    name=node.name,
                    description=docstring.split("\n")[0],
                )
                specs.append(spec)

    logger.debug("Extracted %d resource specs from source code.", len(specs))
    return specs


def _decorator_matches(decorator: ast.expr, names: list[str]) -> bool:
    """Check if an AST decorator node matches any of the given names.

    Args:
        decorator: AST expression node representing a decorator.
        names: List of function/method names to match against.

    Returns:
        True if the decorator matches any name in the list.
    """
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if isinstance(func, ast.Attribute):
            return func.attr in names
        if isinstance(func, ast.Name):
            return func.id in names
    if isinstance(decorator, ast.Attribute):
        return decorator.attr in names
    return False


def _find_tool_names_from_ast(tree: ast.Module, source_code: str) -> list[str]:
    """Extract tool names from the list_tools handler in the AST.

    Looks for functions decorated with @server.list_tools() and extracts
    the ``name`` fields from ``types.Tool(name=...)`` constructor calls.

    Args:
        tree: Parsed AST module.
        source_code: Original source code (for fallback regex).

    Returns:
        List of tool name strings found in the list_tools handler.
    """
    names: list[str] = []

    # AST approach: find Tool(name=...) calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_tool_constructor = (
                (isinstance(func, ast.Attribute) and func.attr == "Tool")
                or (isinstance(func, ast.Name) and func.id == "Tool")
            )
            if is_tool_constructor:
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        names.append(str(kw.value.value))

    # Regex fallback for string-based Tool instantiation
    if not names:
        for m in re.finditer(r'name\s*=\s*["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']', source_code):
            candidate = m.group(1)
            if candidate not in names:
                names.append(candidate)

    return names
