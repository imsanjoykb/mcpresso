import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Sequence

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create server instance
server = Server("time-converter")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """
    List available tools for time operations.
    
    Returns:
        List of available tools including current time getter and timestamp converter.
    """
    return [
        types.Tool(
            name="get_current_utc_time",
            description="Returns the current UTC time as an ISO 8601 formatted string. "
                       "This tool provides the precise current timestamp in Coordinated Universal Time.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="convert_unix_timestamp",
            description="Converts a Unix timestamp (seconds since epoch) to a human-readable date string. "
                       "The output includes both ISO format and a more readable format with timezone information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timestamp": {
                        "type": "number",
                        "description": "Unix timestamp in seconds since January 1, 1970 UTC"
                    },
                    "format": {
                        "type": "string",
                        "enum": ["iso", "readable", "both"],
                        "default": "both",
                        "description": "Output format: 'iso' for ISO 8601, 'readable' for human-friendly, 'both' for both formats"
                    }
                },
                "required": ["timestamp"]
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    """
    Handle tool calls for time operations.
    
    Args:
        name: The name of the tool to call
        arguments: Dictionary of arguments for the tool
        
    Returns:
        List of text content with the tool results
        
    Raises:
        McpError: If tool name is unknown or arguments are invalid
    """
    try:
        if name == "get_current_utc_time":
            return await _get_current_utc_time()
        elif name == "convert_unix_timestamp":
            return await _convert_unix_timestamp(arguments or {})
        else:
            raise McpError(types.ErrorData(code=types.METHOD_NOT_FOUND, message=f"Unknown tool: {name}"))
    except McpError:
        # Re-raise MCP errors as-is
        raise
    except Exception as e:
        logger.error(f"Error in tool {name}: {str(e)}")
        raise McpError(types.ErrorData(code=types.INTERNAL_ERROR, message=f"Internal error executing tool {name}: {str(e)}"))


async def _get_current_utc_time() -> list[types.TextContent]:
    """
    Get the current UTC time as an ISO string.
    
    Returns:
        List containing the current UTC time in ISO 8601 format
    """
    try:
        current_time = datetime.now(timezone.utc)
        iso_time = current_time.isoformat()
        
        logger.info(f"Generated current UTC time: {iso_time}")
        
        return [
            types.TextContent(
                type="text",
                text=f"Current UTC time: {iso_time}"
            )
        ]
    except Exception as e:
        logger.error(f"Error getting current UTC time: {str(e)}")
        raise McpError(types.ErrorData(code=types.INTERNAL_ERROR, message=f"Failed to get current UTC time: {str(e)}"))


async def _convert_unix_timestamp(arguments: dict[str, Any]) -> list[types.TextContent]:
    """
    Convert a Unix timestamp to human-readable date.
    
    Args:
        arguments: Dictionary containing 'timestamp' and optional 'format'
        
    Returns:
        List containing the converted timestamp in requested format(s)
        
    Raises:
        McpError: If timestamp is invalid or conversion fails
    """
    # Validate required arguments
    if "timestamp" not in arguments:
        raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Missing required parameter: timestamp"))
    
    timestamp = arguments["timestamp"]
    output_format = arguments.get("format", "both")
    
    # Validate timestamp type
    if not isinstance(timestamp, (int, float)):
        raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message=f"Invalid timestamp type. Expected number, got {type(timestamp).__name__}"))
    
    # Validate format parameter
    if output_format not in ["iso", "readable", "both"]:
        raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Invalid format parameter. Must be one of: iso, readable, both"))
    
    try:
        # Convert timestamp to datetime
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        
        # Generate requested format(s)
        results = []
        
        if output_format in ["iso", "both"]:
            iso_format = dt.isoformat()
            results.append(f"ISO format: {iso_format}")
        
        if output_format in ["readable", "both"]:
            readable_format = dt.strftime("%A, %B %d, %Y at %H:%M:%S UTC")
            results.append(f"Readable format: {readable_format}")
        
        result_text = "\n".join(results)
        
        logger.info(f"Converted timestamp {timestamp} to: {result_text}")
        
        return [
            types.TextContent(
                type="text",
                text=f"Converted Unix timestamp {timestamp}:\n{result_text}"
            )
        ]
        
    except (ValueError, OSError) as e:
        logger.error(f"Error converting timestamp {timestamp}: {str(e)}")
        raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message=f"Invalid timestamp value: {timestamp}. Error: {str(e)}"))
    except Exception as e:
        logger.error(f"Unexpected error converting timestamp {timestamp}: {str(e)}")
        raise McpError(types.ErrorData(code=types.INTERNAL_ERROR, message=f"Failed to convert timestamp: {str(e)}"))


async def main() -> None:
    """
    Main function to run the MCP server.
    """
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Starting time-converter MCP server")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="time-converter",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                )
            )
        )
        logger.info("Time-converter MCP server stopped")


if __name__ == "__main__":
    asyncio.run(main())