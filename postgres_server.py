import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
import asyncpg
from asyncpg.pool import Pool
import json

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server = Server("postgresql-database")

# Global connection pool
_pool: Optional[Pool] = None

async def get_pool() -> Pool:
    """Get or create the PostgreSQL connection pool.
    
    Returns:
        Pool: The PostgreSQL connection pool instance.
        
    Raises:
        McpError: If database connection fails or pool creation fails.
    """
    global _pool
    if _pool is None:
        try:
            database_url = os.getenv("DATABASE_URL")
            if not database_url:
                # Build connection string from individual components
                host = os.getenv("POSTGRES_HOST", "localhost")
                port = int(os.getenv("POSTGRES_PORT", "5432"))
                database = os.getenv("POSTGRES_DB", "postgres")
                user = os.getenv("POSTGRES_USER", "postgres")
                password = os.getenv("POSTGRES_PASSWORD", "")
                
                if not password:
                    raise McpError(types.ErrorData(
                        code=types.INTERNAL_ERROR,
                        message="Database password not provided via POSTGRES_PASSWORD or DATABASE_URL"
                    ))
                
                database_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"
            
            _pool = await asyncpg.create_pool(
                database_url,
                min_size=1,
                max_size=10,
                command_timeout=30
            )
            logger.info("PostgreSQL connection pool created successfully")
            
        except Exception as e:
            logger.error(f"Failed to create database pool: {e}")
            raise McpError(types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"Failed to connect to database: {str(e)}"
            ))
    
    return _pool

@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List all available PostgreSQL database tools.
    
    Returns:
        List[types.Tool]: List of available database operation tools.
    """
    return [
        types.Tool(
            name="execute_query",
            description="Execute a PostgreSQL query and return results. Supports SELECT, INSERT, UPDATE, DELETE, and other SQL statements. Use with caution for data modification queries.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute"
                    },
                    "parameters": {
                        "type": "array",
                        "items": {"type": ["string", "number", "boolean", "null"]},
                        "description": "Optional query parameters for parameterized queries",
                        "default": []
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="list_tables",
            description="List all tables in the PostgreSQL database. Returns table names, schemas, and basic metadata information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schema": {
                        "type": "string",
                        "description": "Optional schema name to filter tables (defaults to 'public')",
                        "default": "public"
                    }
                },
                "required": []
            }
        ),
        types.Tool(
            name="describe_table",
            description="Get detailed schema information for a specific table including column names, types, constraints, and indexes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to describe"
                    },
                    "schema": {
                        "type": "string",
                        "description": "Schema name (defaults to 'public')",
                        "default": "public"
                    }
                },
                "required": ["table_name"]
            }
        ),
        types.Tool(
            name="get_database_info",
            description="Get general information about the PostgreSQL database including version, current database name, and connection details.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool execution requests.
    
    Args:
        name: The name of the tool to execute.
        arguments: Dictionary of arguments for the tool.
        
    Returns:
        List[types.TextContent]: The tool execution results.
        
    Raises:
        McpError: If tool is not found or execution fails.
    """
    try:
        pool = await get_pool()
        
        if name == "execute_query":
            return await _execute_query(pool, arguments)
        elif name == "list_tables":
            return await _list_tables(pool, arguments)
        elif name == "describe_table":
            return await _describe_table(pool, arguments)
        elif name == "get_database_info":
            return await _get_database_info(pool, arguments)
        else:
            raise McpError(types.ErrorData(
                code=types.METHOD_NOT_FOUND,
                message=f"Unknown tool: {name}"
            ))
            
    except McpError:
        raise
    except Exception as e:
        logger.error(f"Error executing tool {name}: {e}")
        raise McpError(types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"Tool execution failed: {str(e)}"
        ))

async def _execute_query(pool: Pool, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Execute a PostgreSQL query with optional parameters.
    
    Args:
        pool: The database connection pool.
        arguments: Dictionary containing 'query' and optional 'parameters'.
        
    Returns:
        List[types.TextContent]: Query results formatted as text.
        
    Raises:
        McpError: If query execution fails or parameters are invalid.
    """
    if "query" not in arguments:
        raise McpError(types.ErrorData(
            code=types.INVALID_PARAMS,
            message="Missing required parameter: query"
        ))
    
    query = arguments["query"].strip()
    parameters = arguments.get("parameters", [])
    
    if not query:
        raise McpError(types.ErrorData(
            code=types.INVALID_PARAMS,
            message="Query cannot be empty"
        ))
    
    try:
        async with pool.acquire() as conn:
            if parameters:
                result = await conn.fetch(query, *parameters)
            else:
                result = await conn.fetch(query)
            
            if result:
                # Convert rows to list of dictionaries
                rows = [dict(row) for row in result]
                result_text = json.dumps(rows, indent=2, default=str)
                return [types.TextContent(
                    type="text",
                    text=f"Query executed successfully. Returned {len(rows)} row(s):\n\n{result_text}"
                )]
            else:
                # For queries that don't return rows (INSERT, UPDATE, DELETE)
                return [types.TextContent(
                    type="text",
                    text="Query executed successfully. No rows returned."
                )]
                
    except asyncpg.PostgresError as e:
        logger.error(f"PostgreSQL error: {e}")
        raise McpError(types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"Database error: {str(e)}"
        ))
    except Exception as e:
        logger.error(f"Unexpected error executing query: {e}")
        raise McpError(types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"Query execution failed: {str(e)}"
        ))

async def _list_tables(pool: Pool, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """List all tables in the specified schema.
    
    Args:
        pool: The database connection pool.
        arguments: Dictionary containing optional 'schema' parameter.
        
    Returns:
        List[types.TextContent]: List of tables formatted as text.
        
    Raises:
        McpError: If table listing fails.
    """
    schema = arguments.get("schema", "public")
    
    query = """
        SELECT 
            schemaname,
            tablename,
            tableowner,
            hasindexes,
            hasrules,
            hastriggers
        FROM pg_tables 
        WHERE schemaname = $1
        ORDER BY tablename;
    """
    
    try:
        async with pool.acquire() as conn:
            result = await conn.fetch(query, schema)
            
            if result:
                tables = [dict(row) for row in result]
                result_text = json.dumps(tables, indent=2, default=str)
                return [types.TextContent(
                    type="text",
                    text=f"Found {len(tables)} table(s) in schema '{schema}':\n\n{result_text}"
                )]
            else:
                return [types.TextContent(
                    type="text",
                    text=f"No tables found in schema '{schema}'"
                )]
                
    except asyncpg.PostgresError as e:
        logger.error(f"PostgreSQL error listing tables: {e}")
        raise McpError(types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"Failed to list tables: {str(e)}"
        ))

async def _describe_table(pool: Pool, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Get detailed schema information for a specific table.
    
    Args:
        pool: The database connection pool.
        arguments: Dictionary containing 'table_name' and optional 'schema'.
        
    Returns:
        List[types.TextContent]: Table schema information formatted as text.
        
    Raises:
        McpError: If table description fails or table doesn't exist.
    """
    if "table_name" not in arguments:
        raise McpError(types.ErrorData(
            code=types.INVALID_PARAMS,
            message="Missing required parameter: table_name"
        ))
    
    table_name = arguments["table_name"]
    schema = arguments.get("schema", "public")
    
    # Query for column information
    column_query = """
        SELECT 
            column_name,
            data_type,
            is_nullable,
            column_default,
            character_maximum_length,
            numeric_precision,
            numeric_scale
        FROM information_schema.columns 
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position;
    """
    
    # Query for constraint information
    constraint_query = """
        SELECT 
            tc.constraint_name,
            tc.constraint_type,
            kcu.column_name,
            ccu.table_name AS foreign_table_name,
            ccu.column_name AS foreign_column_name
        FROM information_schema.table_constraints AS tc 
        JOIN information_schema.key_column_usage AS kcu
            ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
        LEFT JOIN information_schema.constraint_column_usage AS ccu
            ON ccu.constraint_name = tc.constraint_name
            AND ccu.table_schema = tc.table_schema
        WHERE tc.table_schema = $1 AND tc.table_name = $2;
    """
    
    try:
        async with pool.acquire() as conn:
            # Get column information
            columns = await conn.fetch(column_query, schema, table_name)
            
            if not columns:
                raise McpError(types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Table '{schema}.{table_name}' not found"
                ))
            
            # Get constraint information
            constraints = await conn.fetch(constraint_query, schema, table_name)
            
            # Format results
            table_info = {
                "schema": schema,
                "table_name": table_name,
                "columns": [dict(col) for col in columns],
                "constraints": [dict(cons) for cons in constraints]
            }
            
            result_text = json.dumps(table_info, indent=2, default=str)
            return [types.TextContent(
                type="text",
                text=f"Schema information for table '{schema}.{table_name}':\n\n{result_text}"
            )]
            
    except McpError:
        raise
    except asyncpg.PostgresError as e:
        logger.error(f"PostgreSQL error describing table: {e}")
        raise McpError(types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"Failed to describe table: {str(e)}"
        ))

async def _get_database_info(pool: Pool, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Get general information about the PostgreSQL database.
    
    Args:
        pool: The database connection pool.
        arguments: Dictionary (unused for this operation).
        
    Returns:
        List[types.TextContent]: Database information formatted as text.
        
    Raises:
        McpError: If database information retrieval fails.
    """
    queries = {
        "version": "SELECT version();",
        "current_database": "SELECT current_database();",
        "current_user": "SELECT current_user;",
        "current_schema": "SELECT current_schema();",
        "database_size": "SELECT pg_size_pretty(pg_database_size(current_database()));",
        "connection_count": "SELECT count(*) FROM pg_stat_activity;"
    }
    
    try:
        async with pool.acquire() as conn:
            db_info = {}
            
            for key, query in queries.items():
                try:
                    result = await conn.fetchval(query)
                    db_info[key] = result
                except Exception as e:
                    logger.warning(f"Could not get {key}: {e}")
                    db_info[key] = f"Error: {str(e)}"
            
            result_text = json.dumps(db_info, indent=2, default=str)
            return [types.TextContent(
                type="text",
                text=f"PostgreSQL Database Information:\n\n{result_text}"
            )]
            
    except asyncpg.PostgresError as e:
        logger.error(f"PostgreSQL error getting database info: {e}")
        raise McpError(types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=f"Failed to get database information: {str(e)}"
        ))

async def main() -> None:
    """Main entry point for the PostgreSQL MCP server."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="postgresql-database",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    )
                )
            )
    finally:
        # Clean up the connection pool
        global _pool
        if _pool:
            await _pool.close()
            logger.info("Database connection pool closed")

if __name__ == "__main__":
    asyncio.run(main())