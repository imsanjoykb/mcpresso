"""MCPresso CLI — The Barista Interface.

This module implements the ``mcpresso`` command-line interface using Typer and Rich.
The brewing metaphor is maintained throughout all commands, error messages, and
progress indicators.

Commands:
    mcpresso brew <description>     — Full pipeline: generate + validate + (repair)
    mcpresso validate <file>        — Validate an existing MCP server file
    mcpresso repair <file>          — Auto-repair an existing MCP server file
    mcpresso taste <file>           — Alias for validate (coffee theme)
    mcpresso testgen <file>         — Generate tests for an existing server
    mcpresso registry list          — List all registry entries
    mcpresso registry search <q>   — Semantic search the registry
    mcpresso registry export <out> — Export registry to JSON
    mcpresso registry stats         — Show registry statistics
    mcpresso version                — Show version information

Design Decision (for paper):
    The CLI is the primary user-facing interface and is designed to minimize the
    cognitive overhead of server generation. The coffee brewing metaphor provides
    intuitive progress cues without requiring users to understand the underlying
    pipeline stages. Each stage maps to a brewing step that most users understand:
    grinding (generation), checking the brew (validation), fixing the blend (repair).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from mcpresso import __version__, __tagline__

app = typer.Typer(
    name="mcpresso",
    help=f"☕ MCPresso — {__tagline__}",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=True,
)

registry_app = typer.Typer(
    name="registry",
    help="📚 Manage the MCPresso server template registry.",
    no_args_is_help=True,
)
app.add_typer(registry_app, name="registry")

console = Console()
error_console = Console(stderr=True, style="bold red")

logging.basicConfig(
    level=os.getenv("MCPRESSO_LOG_LEVEL", "WARNING"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

_verbose_option = typer.Option(False, "--verbose", "-v", help="Enable verbose output.")
_output_option = typer.Option(None, "--output", "-o", help="Output file path.")
_api_key_option = typer.Option(None, "--api-key", help="Anthropic API key (overrides env var).")

@app.command()
def brew(
    description: str = typer.Argument(..., help="Plain English description of your MCP server."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output file path (e.g., server.py)."),
    repair: bool = typer.Option(True, "--repair/--no-repair", help="Auto-repair critical issues."),
    with_tests: bool = typer.Option(False, "--with-tests", help="Generate a pytest test suite alongside the server."),
    with_client: bool = typer.Option(False, "--with-client", help="Generate a companion client script (client_<name>.py) for immediate testing."),
    verbose: bool = _verbose_option,
    api_key: Optional[str] = _api_key_option,
) -> None:
    """☕ Brew a production-ready MCP server from a plain English description.

    Examples:

        mcpresso brew "A server that fetches GitHub issues and summarizes them"

        mcpresso brew "A PostgreSQL query server with connection pooling" --output pg_server.py

        mcpresso brew "A calculator server" --with-client --output calc_server.py
    """
    if verbose:
        logging.getLogger("mcpresso").setLevel(logging.DEBUG)

    _print_banner()

    progress_task: Optional[TaskID] = None
    brew_result = None

    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=30),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:

        task = progress.add_task("☕ Starting the brew...", total=8)

        def on_progress(event: str, message: str) -> None:
            """Progress callback for the pipeline."""
            progress.update(task, description=message, advance=1)

        try:
            from mcpresso.pipeline import MCPressoPipeline

            pipeline = MCPressoPipeline(
                api_key=api_key,
                on_progress=on_progress,
            )

            brew_result = pipeline.brew(
                description=description,
                auto_repair=repair,
                output_path=output,
                with_tests=with_tests,
                with_client=with_client,
            )

        except ValueError as exc:
            error_console.print(f"\n❌ Configuration error: {exc}")
            raise typer.Exit(code=1) from exc
        except KeyboardInterrupt:
            console.print("\n⚠️  Brew interrupted by user.")
            raise typer.Exit(code=130)
        except Exception as exc:
            error_console.print(f"\n❌ Brew failed: {exc}")
            if verbose:
                import traceback
                console.print_exception()
            raise typer.Exit(code=1) from exc

        progress.update(task, description="✅ Brew complete!", advance=0, completed=7)

    if brew_result:
        _print_brew_scorecard(brew_result, output, with_tests, with_client, verbose)

@app.command()
def validate(
    file: str = typer.Argument(..., help="Path to the MCP server Python file to validate."),
    report: str = typer.Option("rich", "--report", "-r",
                               help="Report format: 'rich' (default) or 'json'."),
    verbose: bool = _verbose_option,
) -> None:
    """🔍 Validate an existing MCP server file against all 5 quality categories.

    Examples:

        mcpresso validate server.py

        mcpresso validate server.py --report json > report.json
    """
    if not Path(file).exists():
        error_console.print(f"❌ File not found: {file}")
        raise typer.Exit(code=1)

    with console.status(f"[bold cyan]🔍 Checking the brew... validating {file}"):
        source_code = Path(file).read_text(encoding="utf-8")
        from mcpresso.validator import MCPValidator
        validator = MCPValidator()
        result = validator.validate(source_code)

    if report == "json":
        _print_validation_json(result)
    else:
        _print_validation_report(result, file)

    # Exit with non-zero if not execution ready
    if not result.execution_ready:
        raise typer.Exit(code=2)

@app.command()
def taste(
    file: str = typer.Argument(..., help="Path to the MCP server Python file to taste-test."),
    report: str = typer.Option("rich", "--report", "-r", help="Report format: 'rich' or 'json'."),
    verbose: bool = _verbose_option,
) -> None:
    """☕ Taste-test your MCP server (alias for validate, keeping the coffee theme).

    Examples:

        mcpresso taste server.py
    """
    validate(file=file, report=report, verbose=verbose)

@app.command()
def repair(
    file: str = typer.Argument(..., help="Path to the MCP server Python file to repair."),
    output: Optional[str] = typer.Option(None, "--output", "-o",
                                          help="Output path. Defaults to overwriting the input file."),
    verbose: bool = _verbose_option,
    api_key: Optional[str] = _api_key_option,
) -> None:
    """🔧 Auto-repair critical issues in an existing MCP server file.

    Examples:

        mcpresso repair broken_server.py

        mcpresso repair broken_server.py --output fixed_server.py
    """
    if not Path(file).exists():
        error_console.print(f"❌ File not found: {file}")
        raise typer.Exit(code=1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("🔧 Fixing the blend...", total=None)

        try:
            from mcpresso.pipeline import MCPressoPipeline
            pipeline = MCPressoPipeline(api_key=api_key)
            result = pipeline.repair_file(file_path=file, output_path=output)
            progress.update(task, description="✅ Repair complete!")
        except Exception as exc:
            error_console.print(f"\n❌ Repair failed: {exc}")
            if verbose:
                console.print_exception()
            raise typer.Exit(code=1) from exc

    write_path = output or file
    console.print(f"\n[green]✅ Repaired server written to:[/green] {write_path}")
    console.print(f"   Score: [bold]{result.initial_score:.1f}[/bold] → [bold green]{result.final_score:.1f}[/bold green]")
    console.print(f"   Iterations: {result.repair_iterations}")
    console.print(f"   Fixes applied: {len(result.fixes_applied)}")

    if verbose and result.fixes_applied:
        console.print("\n[bold]Fixes Applied:[/bold]")
        for i, fix in enumerate(result.fixes_applied, 1):
            console.print(f"  {i}. [{fix.category}] {fix.description}")

@app.command()
def testgen(
    file: str = typer.Argument(..., help="Path to the MCP server Python file."),
    output: Optional[str] = typer.Option(None, "--output", "-o",
                                          help="Output path for test file. "
                                               "Defaults to test_<filename>.py in same directory."),
    verbose: bool = _verbose_option,
    api_key: Optional[str] = _api_key_option,
) -> None:
    """🧪 Generate a pytest test suite for an existing MCP server.

    Examples:

        mcpresso testgen server.py

        mcpresso testgen server.py --output tests/test_server.py
    """
    file_path = Path(file)
    if not file_path.exists():
        error_console.print(f"❌ File not found: {file}")
        raise typer.Exit(code=1)

    default_output = file_path.parent / f"test_{file_path.stem}.py"
    output_path = output or str(default_output)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("🧪 Brewing the test suite...", total=None)

        try:
            source_code = file_path.read_text(encoding="utf-8")
            from mcpresso.testgen import MCPTestGenerator, _infer_tools_from_source
            generator = MCPTestGenerator(api_key=api_key)
            tools = _infer_tools_from_source(source_code)
            result = generator.generate(
                source_code=source_code,
                tool_definitions=tools,
                server_name=file_path.stem,
            )
            Path(output_path).write_text(result.test_file, encoding="utf-8")
            progress.update(task, description="✅ Tests generated!")
        except Exception as exc:
            error_console.print(f"\n❌ Test generation failed: {exc}")
            if verbose:
                console.print_exception()
            raise typer.Exit(code=1) from exc

    console.print(f"\n[green]✅ Test suite written to:[/green] {output_path}")
    console.print(f"   Tests generated: [bold]{result.test_count}[/bold]")
    console.print(f"   Tools covered: [bold]{len(result.tools_covered)}[/bold]")
    console.print(f"   Security tests: [bold]{result.security_tests}[/bold]")
    console.print(f"   Est. coverage: [bold]{result.estimated_coverage:.1f}%[/bold]")

@app.command()
def version() -> None:
    """☕ Show MCPresso version and system information."""
    _print_banner()
    console.print(f"  [bold]Version:[/bold]  {__version__}")
    console.print(f"  [bold]Tagline:[/bold]  {__tagline__}")
    console.print(f"  [bold]Python:[/bold]   {sys.version.split()[0]}")

@registry_app.command("list")
def registry_list(
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum entries to show."),
    verbose: bool = _verbose_option,
) -> None:
    """📋 List all servers in the local registry."""
    from mcpresso.registry import MCPRegistry
    registry = MCPRegistry()
    entries = registry.list_all()

    if not entries:
        console.print("[yellow]Registry is empty. Brew some servers first![/yellow]")
        console.print("  mcpresso brew \"your server description\"")
        return

    table = Table(
        title=f"☕ MCPresso Registry ({len(entries)} entries)",
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
    )
    table.add_column("ID", style="dim", width=10)
    table.add_column("Description", width=45)
    table.add_column("Score", justify="right", width=8)
    table.add_column("Tier", width=18)
    table.add_column("Tags", width=20)
    table.add_column("Brewed", width=12)

    for entry in entries[:limit]:
        tier_style = {
            "PRODUCTION_READY": "green",
            "STAGING_READY": "yellow",
            "DEVELOPMENT_ONLY": "orange3",
            "NEEDS_REPAIR": "red",
        }.get(entry.readiness_tier, "white")

        table.add_row(
            entry.id[:8],
            entry.description[:43] + ("…" if len(entry.description) > 43 else ""),
            f"{entry.validation_score:.1f}",
            f"[{tier_style}]{entry.readiness_tier}[/{tier_style}]",
            ", ".join(entry.tags[:3]),
            entry.created_at.strftime("%Y-%m-%d"),
        )

    console.print(table)
    if len(entries) > limit:
        console.print(f"  [dim]Showing {limit} of {len(entries)} entries. Use --limit to show more.[/dim]")


@registry_app.command("search")
def registry_search(
    query: str = typer.Argument(..., help="Search query for semantic similarity matching."),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Number of results to show."),
) -> None:
    """🔍 Semantically search the registry for similar servers."""
    from mcpresso.registry import MCPRegistry, _embed, _cosine_similarity
    import numpy as np

    with console.status("[bold cyan]🔍 Searching the registry..."):
        registry = MCPRegistry()
        entries = registry.list_all()

        if not entries:
            console.print("[yellow]Registry is empty. Brew some servers first![/yellow]")
            return

        query_emb = _embed(query)
        results = []
        for entry in entries:
            if entry.embedding:
                sim = _cosine_similarity(
                    np.array(query_emb, dtype=np.float32),
                    np.array(entry.embedding, dtype=np.float32),
                )
                results.append((entry, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        results = results[:top_k]

    console.print(f"\n[bold cyan]Search results for:[/bold cyan] '{query}'\n")

    for rank, (entry, sim) in enumerate(results, 1):
        match_emoji = "🎯" if sim >= 0.85 else ("🌱" if sim >= 0.60 else "❓")
        console.print(
            f"  {rank}. {match_emoji} [{entry.id[:8]}] "
            f"similarity={sim:.3f} | score={entry.validation_score:.1f}"
        )
        console.print(f"     {entry.description[:80]}")
        console.print(f"     Tags: {', '.join(entry.tags)}\n")


@registry_app.command("export")
def registry_export(
    output: str = typer.Argument(..., help="Output JSON file path."),
) -> None:
    """📦 Export the entire registry to a JSON file."""
    from mcpresso.registry import MCPRegistry
    registry = MCPRegistry()
    registry.export(output)
    entries = registry.list_all()
    console.print(f"[green]✅ Exported {len(entries)} entries to {output}[/green]")


@registry_app.command("stats")
def registry_stats() -> None:
    """📊 Show registry statistics."""
    from mcpresso.registry import MCPRegistry
    registry = MCPRegistry()
    stats = registry.stats()

    if stats.get("entry_count", 0) == 0:
        console.print("[yellow]Registry is empty.[/yellow]")
        return

    panel_content = (
        f"[bold]Total Entries:[/bold]     {stats['entry_count']}\n"
        f"[bold]Mean Score:[/bold]        {stats.get('mean_score', 0):.1f}\n"
        f"[bold]Score Std Dev:[/bold]     {stats.get('std_score', 0):.1f}\n"
        f"[bold]Min Score:[/bold]         {stats.get('min_score', 0):.1f}\n"
        f"[bold]Max Score:[/bold]         {stats.get('max_score', 0):.1f}\n"
        f"[bold]Mean Brew Time:[/bold]    {stats.get('mean_brew_time_ms', 0)/1000:.1f}s\n"
    )

    tier_dist = stats.get("tier_distribution", {})
    if tier_dist:
        panel_content += "\n[bold]Tier Distribution:[/bold]\n"
        for tier, count in tier_dist.items():
            panel_content += f"  {tier}: {count}\n"

    console.print(Panel(panel_content, title="☕ Registry Statistics", border_style="cyan"))

def _print_banner() -> None:
    """Print the MCPresso ASCII art banner."""
    banner = r"""
[bold]
[dim] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ [/dim]

        [cyan]███╗   ███╗[/cyan][blue] ██████╗[/blue][magenta]██████╗ [/magenta][cyan]██████╗ [/cyan][blue]███████╗[/blue][magenta]███████╗[/magenta][cyan]███████╗[/cyan][blue] ██████╗ [/blue]
        [cyan]████╗ ████║[/cyan][blue]██╔════╝[/blue][magenta]██╔══██╗[/magenta][cyan]██╔══██╗[/cyan][blue]██╔════╝[/blue][magenta]██╔════╝[/magenta][cyan]██╔════╝[/cyan][blue]██╔═══██╗[/blue]
        [cyan]██╔████╔██║[/cyan][blue]██║     [/blue][magenta]██████╔╝[/magenta][cyan]██████╔╝[/cyan][blue]█████╗  [/blue][magenta]███████╗[/magenta][cyan]███████╗[/cyan][blue]██║   ██║[/blue]
        [cyan]██║╚██╔╝██║[/cyan][blue]██║     [/blue][magenta]██╔═══╝ [/magenta][cyan]██╔══██╗[/cyan][blue]██╔══╝  [/blue][magenta]╚════██║[/magenta][cyan]╚════██║[/cyan][blue]██║   ██║[/blue]
        [cyan]██║ ╚═╝ ██║[/cyan][blue]╚██████╗[/blue][magenta]██║     [/magenta][cyan]██║  ██║[/cyan][blue]███████╗[/blue][magenta]███████║[/magenta][cyan]███████║[/cyan][blue]╚██████╔╝[/blue]
        [cyan]╚═╝     ╚═╝[/cyan][blue] ╚═════╝[/blue][magenta]╚═╝     [/magenta][cyan]╚═╝  ╚═╝[/cyan][blue]╚══════╝[/blue][magenta]╚══════╝[/magenta][cyan]╚══════╝[/cyan][blue] ╚═════╝ [/blue]

[dim] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ [/dim]

                    [bold magenta]✦[/bold magenta]
         [bold white]Brew your MCP server in under 60 seconds[/bold white]
                    [bold magenta]✦[/bold magenta]

"""
    console.print(banner)


def _print_brew_scorecard(
    result: "BrewResult",
    output_path: Optional[str],
    with_tests: bool,
    with_client: bool,
    verbose: bool,
) -> None:
    """Print a rich score card for a completed brew result.

    Args:
        result: Completed BrewResult from the pipeline.
        output_path: Output file path, if any.
        with_tests: Whether tests were generated.
        with_client: Whether a client script was generated.
        verbose: Whether to show detailed output.
    """
    tier_icons = {
        "PRODUCTION_READY": ("🏆", "green"),
        "STAGING_READY": ("✅", "yellow"),
        "DEVELOPMENT_ONLY": ("⚠️", "orange3"),
        "NEEDS_REPAIR": ("❌", "red"),
    }
    tier_name = result.readiness_tier.value
    tier_icon, tier_color = tier_icons.get(tier_name, ("❓", "white"))

    time_color = "green" if result.under_60_seconds else "yellow"
    time_icon = "✓" if result.under_60_seconds else "⚠"

    score_bar = _make_score_bar(result.final_score)

    # Main scorecard panel
    content = (
        f"[bold]Score:[/bold]         {score_bar} [bold]{result.final_score:.1f}[/bold]/100\n"
        f"[bold]Readiness:[/bold]     [{tier_color}]{tier_icon} {tier_name}[/{tier_color}]\n"
        f"[bold]Brew Time:[/bold]     [{time_color}]{time_icon} {result.total_time_ms/1000:.2f}s[/{time_color}]"
        f"{'  (under 60s ✓)' if result.under_60_seconds else '  ⚠ exceeded 60s target'}\n"
        f"[bold]Tools Generated:[/bold] {len(result.generation_result.tool_definitions)}\n"
        f"[bold]Registry Match:[/bold] {result.generation_result.registry_match_type.value}\n"
    )

    if result.repair_result:
        r = result.repair_result
        content += (
            f"[bold]Repair:[/bold]        {r.repair_iterations} iteration(s) | "
            f"{r.initial_score:.1f} → {r.final_score:.1f}\n"
        )

    if result.test_result:
        t = result.test_result
        content += (
            f"[bold]Tests:[/bold]         {t.test_count} tests | "
            f"{t.security_tests} security | "
            f"~{t.estimated_coverage:.0f}% coverage\n"
        )

    if output_path:
        content += f"\n[bold]Output:[/bold]        [cyan]{output_path}[/cyan]"
        if result.test_result:
            test_file = Path(output_path).parent / f"test_{Path(output_path).stem}.py"
            content += f"\n[bold]Tests:[/bold]         [cyan]{test_file}[/cyan]"
        if with_client:
            client_file = Path(output_path).parent / f"client_{Path(output_path).stem}.py"
            content += f"\n[bold]Client:[/bold]        [green]{client_file}[/green]  ← run this to test!"

    if result.registry_entry_id:
        content += f"\n[bold]Registry ID:[/bold]   [dim]{result.registry_entry_id[:8]}[/dim]"

    console.print(Panel(
        content,
        title=f"☕ Brew Complete — {result.brew_id[:8]}",
        border_style=tier_color,
        padding=(1, 2),
    ))

    # Category scores table
    if verbose:
        _print_category_scores(result)

    # Issues summary
    if result.validation_report.critical_issues:
        _print_issues_summary(result)


def _make_score_bar(score: float, width: int = 20) -> str:
    """Create a visual score bar using Unicode block characters.

    Args:
        score: Score value 0–100.
        width: Width of the bar in characters.

    Returns:
        Rich markup string with colored score bar.
    """
    filled = int(score / 100 * width)
    empty = width - filled
    color = "green" if score >= 75 else ("yellow" if score >= 50 else "red")
    bar = f"[{color}]{'█' * filled}[/{color}]{'░' * empty}"
    return bar


def _print_category_scores(result: "BrewResult") -> None:
    """Print per-category validation scores as a table.

    Args:
        result: Completed BrewResult.
    """
    table = Table(
        title="Validation Category Scores",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("Category", width=30)
    table.add_column("Score", justify="right", width=8)
    table.add_column("Weight", justify="right", width=8)
    table.add_column("Bar", width=22)
    table.add_column("Issues", justify="right", width=8)

    for key, cat in result.validation_report.category_scores.items():
        color = "green" if cat.score >= 75 else ("yellow" if cat.score >= 50 else "red")
        issue_count = len(cat.issues)
        table.add_row(
            cat.name,
            f"{cat.score:.1f}",
            f"{cat.weight:.0%}",
            _make_score_bar(cat.score, width=15),
            f"[red]{issue_count}[/red]" if issue_count > 0 else "[green]0[/green]",
        )

    console.print(table)


def _print_issues_summary(result: "BrewResult") -> None:
    """Print a summary of critical issues and warnings.

    Args:
        result: Completed BrewResult.
    """
    report = result.validation_report
    if not report.critical_issues and not report.warnings:
        return

    console.print("\n[bold]Issues Found:[/bold]")

    for issue in report.critical_issues[:5]:
        console.print(f"  [red]🔴 CRITICAL[/red] [{issue.category}] {issue.message}")
        if issue.fix_suggestion:
            console.print(f"     [dim]Fix: {issue.fix_suggestion[:80]}[/dim]")

    for issue in report.warnings[:3]:
        console.print(f"  [yellow]🟡 WARNING[/yellow] [{issue.category}] {issue.message}")

    remaining = len(report.critical_issues) - 5
    if remaining > 0:
        console.print(f"  [dim]... and {remaining} more critical issues[/dim]")


def _print_validation_report(result: "ValidationReport", file_path: str) -> None:
    """Print a rich validation report for the 'validate' command.

    Args:
        result: ValidationReport from the validator.
        file_path: Path of the validated file (for display).
    """
    status = "[green]✅ EXECUTION READY[/green]" if result.execution_ready \
             else "[red]❌ NOT READY[/red]"

    console.print(Panel(
        f"[bold]File:[/bold]       {file_path}\n"
        f"[bold]Status:[/bold]     {status}\n"
        f"[bold]Score:[/bold]      {_make_score_bar(result.overall_score)} "
        f"[bold]{result.overall_score:.1f}[/bold]/100\n"
        f"[bold]Confidence:[/bold] {result.confidence_level.value}\n"
        f"[bold]Critical:[/bold]   {len(result.critical_issues)} issues\n"
        f"[bold]Warnings:[/bold]   {len(result.warnings)} issues\n"
        f"[bold]Validated in:[/bold] {result.validation_time_ms:.1f}ms",
        title="🔍 Validation Report",
        border_style="cyan" if result.execution_ready else "red",
    ))

    # Category breakdown
    table = Table(show_header=True, header_style="bold", border_style="dim")
    table.add_column("Category", width=32)
    table.add_column("Score", justify="right", width=8)
    table.add_column("Passed", justify="right", width=8)
    table.add_column("Failed", justify="right", width=8)

    for key, cat in result.category_scores.items():
        color = "green" if cat.score >= 75 else ("yellow" if cat.score >= 50 else "red")
        table.add_row(
            cat.name,
            f"[{color}]{cat.score:.1f}[/{color}]",
            f"[green]{len(cat.passed_checks)}[/green]",
            f"[red]{len(cat.failed_checks)}[/red]" if cat.failed_checks else "0",
        )
    console.print(table)

    # Issues
    if result.critical_issues:
        console.print("\n[bold red]Critical Issues:[/bold red]")
        for issue in result.critical_issues:
            console.print(f"  🔴 [{issue.category}] {issue.message}")
            if issue.fix_suggestion:
                console.print(f"     [dim]→ {issue.fix_suggestion[:100]}[/dim]")

    if result.warnings:
        console.print("\n[bold yellow]Warnings:[/bold yellow]")
        for issue in result.warnings[:5]:
            console.print(f"  🟡 [{issue.category}] {issue.message}")


def _print_validation_json(result: "ValidationReport") -> None:
    """Print validation report as machine-readable JSON.

    Args:
        result: ValidationReport from the validator.
    """
    import dataclasses
    from datetime import datetime

    def default_serializer(obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        if hasattr(obj, "value"):
            return obj.value
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    output = {
        "overall_score": result.overall_score,
        "confidence_level": result.confidence_level.value,
        "execution_ready": result.execution_ready,
        "validation_time_ms": result.validation_time_ms,
        "source_code_hash": result.source_code_hash,
        "critical_issue_count": len(result.critical_issues),
        "warning_count": len(result.warnings),
        "suggestion_count": len(result.suggestions),
        "category_scores": {
            key: {
                "name": cat.name,
                "score": cat.score,
                "weight": cat.weight,
                "passed_checks": cat.passed_checks,
                "failed_checks": cat.failed_checks,
                "issue_count": len(cat.issues),
            }
            for key, cat in result.category_scores.items()
        },
        "critical_issues": [
            {
                "id": i.id,
                "category": i.category,
                "severity": i.severity.value,
                "message": i.message,
                "line_number": i.line_number,
                "fix_suggestion": i.fix_suggestion,
            }
            for i in result.critical_issues
        ],
    }
    rprint(json.dumps(output, indent=2))
    
def main() -> None:
    """Entry point for the mcpresso CLI.

    This function is registered as the console_scripts entry point in
    pyproject.toml: ``mcpresso = "mcpresso.cli:main"``.
    """
    app()


if __name__ == "__main__":
    main()
