"""A well-formed MCP echo server — MCPresso demo."""
import asyncio
import logging
import os
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types
from mcp import McpError

logger = logging.getLogger(__name__)
server = Server("demo-server")


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
                    "text": {"type": "string", "description": "Text to echo back"}
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="reverse",
            description="Reverses the input text and returns it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to reverse"}
                },
                "required": ["text"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Handle tool calls with validation and error handling."""
    if name == "echo":
        text = arguments.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise McpError(types.ErrorCode.InvalidParams, "text must be a non-empty string")
        try:
            logger.info("echo tool called with text length=%d", len(text))
            return [types.TextContent(type="text", text=text)]
        except Exception as exc:
            logger.error("echo tool error: %s", exc)
            raise McpError(types.ErrorCode.InternalError, str(exc))

    elif name == "reverse":
        text = arguments.get("text", "")
        if not isinstance(text, str):
            raise McpError(types.ErrorCode.InvalidParams, "text must be a string")
        try:
            logger.info("reverse tool called")
            result = text[::-1]
            return [types.TextContent(type="text", text=result)]
        except Exception as exc:
            logger.error("reverse tool error: %s", exc)
            raise McpError(types.ErrorCode.InternalError, str(exc))

    raise McpError(types.ErrorCode.MethodNotFound, f"Unknown tool: {name}")


async def main() -> None:
    """Run the MCP server using stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
