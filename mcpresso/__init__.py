"""MCPresso — Brew your MCP server in under 60 seconds.

MCPresso is a production-grade Python package that transforms natural language
descriptions into fully validated, security-audited MCP (Model Context Protocol)
servers in under 60 seconds.

Branding concept: "Brewed in under 60 seconds." ☕

Example:
    >>> from mcpresso import MCPressoPipeline
    >>> result = MCPressoPipeline().brew(
    ...     description="A server that fetches GitHub issues and summarizes them",
    ...     auto_repair=True,
    ...     output_path="./github_server.py"
    ... )
    >>> print(f"Score: {result.final_score:.1f} | Tier: {result.readiness_tier}")
"""

from mcpresso.pipeline import MCPressoPipeline
from mcpresso.models import (
    BrewResult,
    GenerationResult,
    ValidationReport,
    RepairResult,
    ConfidenceScore,
    TestGenResult,
    RegistryEntry,
    ReadinessTier,
    ConfidenceLevel,
)

__version__ = "0.1.0"
__author__ = "MCPresso Team"
__email__ = "mcpresso@example.com"
__tagline__ = "Brew your MCP server in under 60 seconds"

__all__ = [
    "MCPressoPipeline",
    "BrewResult",
    "GenerationResult",
    "ValidationReport",
    "RepairResult",
    "ConfidenceScore",
    "TestGenResult",
    "RegistryEntry",
    "ReadinessTier",
    "ConfidenceLevel",
    "__version__",
    "__tagline__",
]
