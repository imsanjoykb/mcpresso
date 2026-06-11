"""MCPresso Client Generator — Automatic MCP Client Script Synthesis.

Every time MCPresso brews a server, it can simultaneously generate a companion
client script that a user can run immediately to test every tool in the generated
server — zero manual configuration required.

Design Decision (for paper):
    Client generation is **deterministic** (no LLM call) — it infers realistic
    example argument values from each tool's JSON Schema ``inputSchema``.  This is
    intentional: client generation must be instant (< 50 ms) and reproducible so it
    never adds to the 60-second brew budget.  The approach mirrors the "co-generation"
    pattern described in the paper's novel contributions section.

Architecture:
    ToolSpec.input_schema  →  _infer_example_args()  →  per-tool call block
    All tool blocks        →  _render_client_script() →  final .py file string
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from mcpresso.models import ClientGenResult, ToolSpec

logger = logging.getLogger(__name__)

class MCPClientGenerator:
    """Generates a runnable MCP client script from a server's tool definitions.

    The generated client:
    - Connects to the server via stdio using the official MCP Python SDK.
    - Discovers and lists all tools on startup.
    - Calls every tool with inferred example arguments.
    - Pretty-prints results and handles errors gracefully.

    Example:
        >>> gen = MCPClientGenerator()
        >>> result = gen.generate(
        ...     tool_definitions=brew_result.generation_result.tool_definitions,
        ...     server_file="my_server.py",
        ...     server_name="calculator"
        ... )
        >>> Path("client_my_server.py").write_text(result.client_file)
    """

    def generate(
        self,
        tool_definitions: list[ToolSpec],
        server_file: str,
        server_name: str = "mcp_server",
    ) -> ClientGenResult:

        t0 = time.monotonic()
        logger.info(
            "Generating client script [server=%s, tools=%d]",
            server_file,
            len(tool_definitions),
        )

        example_args: dict[str, dict[str, Any]] = {}
        tool_call_blocks: list[str] = []

        for spec in tool_definitions:
            args = _infer_example_args(spec)
            example_args[spec.name] = args
            block = _render_tool_call_block(spec, args)
            tool_call_blocks.append(block)

        client_script = _render_client_script(
            server_file=server_file,
            server_name=server_name,
            tool_definitions=tool_definitions,
            tool_call_blocks=tool_call_blocks,
        )

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Client generation complete [%.1fms, %d tool calls]",
            elapsed_ms,
            len(tool_definitions),
        )

        return ClientGenResult(
            client_file=client_script,
            tool_call_count=len(tool_definitions),
            tools_covered=[s.name for s in tool_definitions],
            example_args=example_args,
            generation_time_ms=elapsed_ms,
            server_file=server_file,
        )


def _infer_example_args(spec: ToolSpec) -> dict[str, Any]:
    props: dict[str, Any] = spec.input_schema.get("properties", {})
    required: list[str] = spec.input_schema.get("required", [])

    args: dict[str, Any] = {}
    for param_name, schema in props.items():
        # Only include required params + first 3 optional ones to keep output clean
        if param_name not in required and len(args) >= 3:
            continue
        args[param_name] = _example_value(param_name, schema)

    return args


def _example_value(name: str, schema: dict[str, Any]) -> Any:
    # Enum → first option
    if "enum" in schema:
        return schema["enum"][0]

    # Default → based on name hints
    name_lower = name.lower()
    typ = schema.get("type", "string")

    if typ in ("number", "integer", "float"):
        # Pick contextual numbers
        if any(k in name_lower for k in ("timestamp", "epoch", "time")):
            return 1700000000
        if any(k in name_lower for k in ("port",)):
            return 8080
        if any(k in name_lower for k in ("limit", "count", "max", "size")):
            return 10
        if any(k in name_lower for k in ("page",)):
            return 1
        if typ == "integer":
            return 42
        return 9.5

    if typ == "boolean":
        return True

    if typ == "array":
        return []

    if typ == "object":
        return {}

    # String — pick contextual value
    if any(k in name_lower for k in ("url", "endpoint", "uri")):
        return "https://example.com"
    if any(k in name_lower for k in ("email",)):
        return "user@example.com"
    if any(k in name_lower for k in ("repo", "repository")):
        return "owner/repo"
    if any(k in name_lower for k in ("owner", "user", "author", "username")):
        return "octocat"
    if any(k in name_lower for k in ("label", "tag", "filter")):
        return "bug"
    if any(k in name_lower for k in ("state", "status")):
        return "open"
    if any(k in name_lower for k in ("query", "search", "q")):
        return "hello world"
    if any(k in name_lower for k in ("path", "file", "dir")):
        return "./data"
    if any(k in name_lower for k in ("message", "text", "content", "body")):
        return "Hello from MCPresso!"
    if any(k in name_lower for k in ("title",)):
        return "Example Item"
    if any(k in name_lower for k in ("description", "summary", "detail")):
        return "Created by MCPresso client"
    if any(k in name_lower for k in ("name",)):
        return "example-name"
    if any(k in name_lower for k in ("priority",)):
        return "medium"
    if any(k in name_lower for k in ("id",)):
        return "1"
    if any(k in name_lower for k in ("key", "token")):
        return "YOUR_KEY_HERE"
    if any(k in name_lower for k in ("format",)):
        return "json"
    if name_lower in ("a", "x", "first", "left"):
        return 12
    if name_lower in ("b", "y", "second", "right"):
        return 8

    return "example"

def _render_tool_call_block(spec: ToolSpec, args: dict[str, Any]) -> str:

    args_repr = json.dumps(args, indent=None) if args else "{}"
    # Multiline for readability when many args
    if len(args) > 2:
        inner = json.dumps(args, indent=12)
        args_repr = inner

    lines = [
        f'            # ── {spec.name} {"─" * max(0, 50 - len(spec.name))}',
        f'            print(f"\\nCALL → {spec.name}({_format_args_preview(args)})")',
        f'            try:',
        f'                result = await session.call_tool("{spec.name}", {args_repr})',
        f'                for item in result.content:',
        f'                    print(f"  ✓  {{item.text}}")',
        f'            except Exception as e:',
        f'                print(f"  ✗  Error: {{type(e).__name__}}: {{e}}")',
        f'',
    ]
    return "\n".join(lines)


def _format_args_preview(args: dict[str, Any]) -> str:
    """One-line preview of args for the print statement.

    Args:
        args: Example arguments dict.

    Returns:
        Compact string like ``a=12, b=8`` or empty string.
    """
    if not args:
        return ""
    return ", ".join(f"{k}={v!r}" for k, v in args.items())


def _render_client_script(
    server_file: str,
    server_name: str,
    tool_definitions: list[ToolSpec],
    tool_call_blocks: list[str],
) -> str:

    tool_list_comments = "\n".join(
        f"#   {i+1}. {s.name}" for i, s in enumerate(tool_definitions)
    )
    tool_count = len(tool_definitions)

    # Build the EXAMPLE_ARGS dict literal embedded in the generated script.
    # Keys are tool names; values are the inferred args dicts.
    # We also include entries derived from _infer_example_args for every spec.
    args_items = []
    for spec in tool_definitions:
        # Recompute args here so the dict is self-contained in the template.
        args = _infer_example_args(spec)
        args_repr = json.dumps(args) if args else "{}"
        args_items.append(f'    "{spec.name}": {args_repr},')
    args_dict_body = "\n".join(args_items) if args_items else "    # no pre-computed args"

    return f'''\
"""Auto-generated MCP client for {server_name}.
Generated by MCPresso — "Brew your MCP server in under 60 seconds"

This client connects to {server_file!r} via stdio.  It discovers all tools
at runtime via ``session.list_tools()`` and calls each one, using
pre-computed example arguments where available (else infers from schema).

Hint tools from generation phase ({tool_count}):
{tool_list_comments}

Usage:
    python {_client_filename(server_file)}

Requirements:
    pip install mcp
"""

import asyncio
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER_FILE = "{server_file}"

# Pre-computed example arguments (keyed by tool name).
# For tools not listed here, arguments are inferred at runtime from the schema.
EXAMPLE_ARGS: dict[str, dict[str, Any]] = {{
{args_dict_body}
}}


def _infer_arg(param_name: str, schema: dict) -> Any:
    """Infer a single example argument value from a JSON Schema property.

    Used at runtime for tools not in EXAMPLE_ARGS.
    """
    if "enum" in schema:
        return schema["enum"][0]
    name = param_name.lower()
    typ = schema.get("type", "string")
    if typ in ("number", "float"):
        return 1700000000 if "timestamp" in name else 9.5
    if typ == "integer":
        return 1 if "page" in name else (10 if "limit" in name else 42)
    if typ == "boolean":
        return True
    if typ == "array":
        return []
    if typ == "object":
        return {{}}
    # String heuristics
    if "url" in name or "uri" in name:
        return "https://example.com"
    if "email" in name:
        return "user@example.com"
    if "repo" in name:
        return "owner/repo"
    if "query" in name or "search" in name:
        return "hello world"
    if "message" in name or "text" in name or "body" in name:
        return "Hello from MCPresso!"
    if "title" in name or "name" in name:
        return "Example Item"
    if "description" in name:
        return "Created by MCPresso client"
    if "id" in name:
        return "1"
    return "example"


async def main() -> None:
    """Connect to the MCP server and call all available tools."""
    params = StdioServerParameters(command="python", args=[SERVER_FILE])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── Tool discovery ───────────────────────────────────────────────
            tools_resp = await session.list_tools()
            print("=" * 60)
            print(f"  Server: {server_name}  ({{SERVER_FILE}})")
            print(f"  Tools available: {{len(tools_resp.tools)}}")
            print("=" * 60)
            for t in tools_resp.tools:
                print(f"  * {{t.name:20s}} {{t.description[:45]}}")
            print()

            # ── Tool calls (dynamic — iterates over ALL discovered tools) ─────
            for tool in tools_resp.tools:
                # Use pre-computed args if available; else infer from schema
                if tool.name in EXAMPLE_ARGS:
                    args = EXAMPLE_ARGS[tool.name]
                else:
                    props = (tool.inputSchema or {{}}).get("properties", {{}})
                    required = (tool.inputSchema or {{}}).get("required", [])
                    args = {{}}
                    for pname, pschema in props.items():
                        if pname in required or len(args) < 3:
                            args[pname] = _infer_arg(pname, pschema)

                args_preview = ", ".join(f"{{k}}={{v!r}}" for k, v in args.items())
                print(f"\\nCALL -> {{tool.name}}({{args_preview}})")
                try:
                    result = await session.call_tool(tool.name, args)
                    for item in result.content:
                        print(f"  [OK]  {{item.text}}")
                except Exception as e:
                    print(f"  [ERR] {{type(e).__name__}}: {{e}}")

            print()
            print("=" * 60)
            print("  All tool calls completed.")
            print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
'''


def _client_filename(server_file: str) -> str:
    import os
    base = os.path.basename(server_file)
    stem, ext = os.path.splitext(base)
    return f"client_{stem}{ext}"
