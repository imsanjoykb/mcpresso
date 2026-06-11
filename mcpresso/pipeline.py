"""MCPresso Pipeline — End-to-End Orchestration of the Brew Pipeline.

This module implements the top-level orchestration logic that connects all
pipeline stages into a single coherent ``brew()`` call: generate → validate
→ repair → score → (optionally) test generate → save to registry → write output.

Design Decision (for paper):
    The pipeline is designed as a linear directed acyclic graph (DAG) of stages
    with clearly defined inter-stage contracts (the dataclass types in models.py).
    This separation of concerns enables independent testing of each stage and
    independent improvement without affecting other stages.

    Stage DAG:
        [Registry Search] → [Generate] → [Validate] → [Repair?]
                         → [Score] → [TestGen?] → [Registry Save] → [Output]

    The "Brewed in under 60 seconds" target is met by:
    - Registry adaptation (~10s) or seeding (~30s) rather than full generation
    - Parallel consistency generation (optional, enabled in scorer)
    - All validation/scoring is local computation (no additional API calls)
    - Repair is bounded to 3 iterations × ~10s each = ~30s max

    The 60-second timer is tracked as a hard wall-clock constraint and surfaced
    in the BrewResult.under_60_seconds field for benchmarking.

Progress Events:
    The pipeline emits progress via optional callback hooks injected at
    construction time. These are used by the CLI to drive the coffee brewing
    animation. The events map to brewing steps:
        on_start       → ☕ Starting the brew...
        on_generate    → ⚙ Grinding beans...
        on_validate    → 🔍 Checking the brew...
        on_repair      → 🔧 Fixing the blend...
        on_score       → 📊 Measuring the roast...
        on_testgen     → 🧪 Running quality checks...
        on_complete    → ✅ Your server is ready!
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from mcpresso.clientgen import MCPClientGenerator
from mcpresso.generator import MCPGenerator
from mcpresso.models import (
    BrewResult,
    ClientGenResult,
    GenerationResult,
    ReadinessTier,
    RegistryMatchType,
)
from mcpresso.registry import MCPRegistry
from mcpresso.repair import MCPRepairEngine
from mcpresso.scorer import MCPScorer
from mcpresso.testgen import MCPTestGenerator
from mcpresso.validator import MCPValidator

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress Event Callbacks Type Alias
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[str, str], None]
"""Callback type for pipeline progress events.

Args:
    event_name: One of 'start', 'generate', 'validate', 'repair', 'score',
                'testgen', 'complete'.
    message: Human-readable message describing the current step.
"""

# ---------------------------------------------------------------------------
# Pipeline Configuration
# ---------------------------------------------------------------------------

BREW_TARGET_SECONDS = 60.0
REGISTRY_SAVE_THRESHOLD = 75.0  # only save servers with score >= this


# ---------------------------------------------------------------------------
# Pipeline Class
# ---------------------------------------------------------------------------


class MCPressoPipeline:
    """End-to-end MCP server generation pipeline.

    Orchestrates all stages of the brew pipeline from a natural language
    description to a validated, optionally repaired, and optionally tested
    MCP server Python file.

    Design Decision:
        The pipeline is intentionally "thin" — it contains no generation,
        validation, or repair logic itself. All business logic lives in the
        specialist modules (generator, validator, repair, scorer, etc.).
        The pipeline is purely an orchestrator.

    Attributes:
        generator: MCPGenerator instance for server generation.
        validator: MCPValidator instance for quality validation.
        repair_engine: MCPRepairEngine for auto-repair.
        scorer: MCPScorer for confidence scoring.
        registry: MCPRegistry for template reuse.
        test_generator: MCPTestGenerator for test co-generation.
        on_progress: Optional callback for progress events.

    Example:
        >>> pipeline = MCPressoPipeline()
        >>> result = pipeline.brew(
        ...     description="A server that queries PostgreSQL and returns results",
        ...     auto_repair=True,
        ...     output_path="./pg_server.py",
        ... )
        >>> print(f"✅ Score: {result.final_score:.1f} | {result.readiness_tier.value}")
        >>> print(f"⏱  Brewed in {result.total_time_ms/1000:.1f}s")
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        registry_dir: str | None = None,
        on_progress: ProgressCallback | None = None,
        enable_consistency_check: bool = False,
    ) -> None:
        """Initialize the MCPressoPipeline with all sub-components.

        Args:
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            model: Model identifier. Falls back to MCPRESSO_MODEL env var.
            registry_dir: Override registry directory path.
            on_progress: Callback for progress events (event_name, message).
            enable_consistency_check: If True, generate twice for consistency
                                      scoring (increases total time by ~30s).
        """
        resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        resolved_model = model or os.getenv("MCPRESSO_MODEL")

        self.generator = MCPGenerator(
            model=resolved_model or "claude-sonnet-4-20250514",
            api_key=resolved_key,
        )
        self.validator = MCPValidator()
        self.repair_engine = MCPRepairEngine(
            model=resolved_model or "claude-sonnet-4-20250514",
            api_key=resolved_key,
        )
        self.scorer = MCPScorer()
        self.registry = MCPRegistry(registry_dir=registry_dir)
        self.test_generator = MCPTestGenerator(
            model=resolved_model or "claude-sonnet-4-20250514",
            api_key=resolved_key,
        )
        self.on_progress = on_progress
        self.enable_consistency_check = enable_consistency_check

        logger.info("MCPressoPipeline initialized")

    def brew(
        self,
        description: str,
        auto_repair: bool = True,
        output_path: str | None = None,
        with_tests: bool = False,
        with_client: bool = False,
        save_to_registry: bool = True,
    ) -> BrewResult:
        """Run the complete brew pipeline: generate → validate → repair → score.

        This is the primary public API for MCPresso. Accepts a plain English
        description and returns a fully validated, optionally repaired MCP server.

        Args:
            description: Plain English description of the MCP server's purpose.
            auto_repair: If True, automatically fix critical validation issues
                         (up to 3 iterations). Default: True.
            output_path: File path to write the generated server. If None,
                         the server code is returned but not written to disk.
            with_tests: If True, generate a pytest test suite alongside the server.
            with_client: If True, generate a companion client script alongside
                         the server as ``client_<name>.py``. Instant — no LLM call.
            save_to_registry: If True and score >= 75, save to local registry.

        Returns:
            BrewResult with complete audit trail, source code, scores, and
            optional test suite.

        Raises:
            ValueError: If description is empty.
            RuntimeError: If generation fails after registry fallback.
        """
        if not description or not description.strip():
            raise ValueError("Description cannot be empty.")

        pipeline_start = time.monotonic()
        self._emit("start", f"Starting the brew for: {description[:80]}...")

        # ------------------------------------------------------------------
        # Stage 0: Registry Search
        # ------------------------------------------------------------------
        self._emit("generate", "⚙ Grinding beans... (searching registry)")
        registry_result = None
        seed_code: str | None = None
        seed_id: str | None = None
        match_type = RegistryMatchType.FULL_GENERATION

        try:
            registry_result = self.registry.search(description)
        except Exception as exc:
            logger.warning("Registry search failed (non-blocking): %s", exc)

        if registry_result is not None:
            seed_code = registry_result.entry.source_code
            seed_id = registry_result.entry.id
            match_type = registry_result.match_type
            logger.info(
                "Registry match found [sim=%.3f, type=%s]",
                registry_result.similarity,
                match_type.value,
            )

        # ------------------------------------------------------------------
        # Stage 1: Generate
        # ------------------------------------------------------------------
        tier_msg = {
            RegistryMatchType.ADAPT: "adapting existing server (~10s)",
            RegistryMatchType.SEED: "using registry seed (~30s)",
            RegistryMatchType.FULL_GENERATION: "generating from scratch (~60s)",
        }
        self._emit("generate", f"⚙ Grinding beans... ({tier_msg[match_type]})")

        generation_result = asyncio.run(self.generator.generate(
            description=description,
            seed_server_code=seed_code,
            seed_server_id=seed_id,
            match_type=match_type,
        ))
        logger.info(
            "Generation complete [time=%.1fms, tools=%d]",
            generation_result.generation_time_ms,
            len(generation_result.tool_definitions),
        )

        # Optional second generation for self-consistency scoring
        alt_code: str | None = None
        if self.enable_consistency_check:
            try:
                alt_result = asyncio.run(self.generator.generate(
                    description=description,
                    seed_server_code=seed_code,
                    seed_server_id=seed_id,
                    match_type=match_type,
                ))
                alt_code = alt_result.source_code
                logger.info("Consistency check generation complete.")
            except Exception as exc:
                logger.warning("Consistency check generation failed: %s", exc)

        # ------------------------------------------------------------------
        # Stage 2: Validate
        # ------------------------------------------------------------------
        self._emit("validate", "🔍 Checking the brew... (validating)")
        validation_report = self.validator.validate(generation_result.source_code)
        logger.info(
            "Validation complete [score=%.1f, critical=%d, warnings=%d]",
            validation_report.overall_score,
            len(validation_report.critical_issues),
            len(validation_report.warnings),
        )

        # ------------------------------------------------------------------
        # Stage 3: Repair (optional)
        # ------------------------------------------------------------------
        repair_result = None
        final_source_code = generation_result.source_code
        final_report = validation_report

        if auto_repair and (
            validation_report.critical_issues
            or validation_report.overall_score < REGISTRY_SAVE_THRESHOLD
        ):
            self._emit("repair", "🔧 Fixing the blend... (auto-repairing)")

            repair_result = self.repair_engine.repair(
                source_code=generation_result.source_code,
                validation_report=validation_report,
            )
            final_source_code = repair_result.repaired_code
            final_report = repair_result.final_report

            logger.info(
                "Repair complete [iterations=%d, score: %.1f → %.1f, success=%s]",
                repair_result.repair_iterations,
                repair_result.initial_score,
                repair_result.final_score,
                repair_result.success,
            )

        # ------------------------------------------------------------------
        # Stage 4: Score
        # ------------------------------------------------------------------
        self._emit("score", "📊 Measuring the roast... (scoring)")
        confidence_score = self.scorer.compute_score(
            source_code=final_source_code,
            validation_report=final_report,
            alternative_code=alt_code,
        )
        logger.info(
            "Scoring complete [overall=%.1f, tier=%s]",
            confidence_score.overall_score,
            confidence_score.readiness_tier.value,
        )

        # ------------------------------------------------------------------
        # Stage 5: Test Generation (optional)
        # ------------------------------------------------------------------
        test_result = None
        if with_tests:
            self._emit("testgen", "🧪 Running quality checks... (generating tests)")
            try:
                # Determine server name from output path or use default
                if output_path:
                    server_name = Path(output_path).stem
                else:
                    server_name = "mcp_server"

                test_result = self.test_generator.generate(
                    source_code=final_source_code,
                    tool_definitions=generation_result.tool_definitions,
                    server_name=server_name,
                )
                logger.info(
                    "Test generation complete [tests=%d, coverage=%.1f%%]",
                    test_result.test_count,
                    test_result.estimated_coverage,
                )
            except Exception as exc:
                logger.warning("Test generation failed (non-blocking): %s", exc)

        # ------------------------------------------------------------------
        # Stage 5b: Client Generation (optional, deterministic — no LLM)
        # ------------------------------------------------------------------
        client_result: ClientGenResult | None = None
        if with_client:
            self._emit("clientgen", "☕ Pouring the cup... (generating client)")
            try:
                server_file = Path(output_path).name if output_path else "server.py"
                server_name = Path(server_file).stem
                client_gen = MCPClientGenerator()
                client_result = client_gen.generate(
                    tool_definitions=generation_result.tool_definitions,
                    server_file=server_file,
                    server_name=server_name,
                )
                logger.info(
                    "Client generation complete [tools=%d, time=%.1fms]",
                    client_result.tool_call_count,
                    client_result.generation_time_ms,
                )
            except Exception as exc:
                logger.warning("Client generation failed (non-blocking): %s", exc)

        # ------------------------------------------------------------------
        # Stage 6: Output
        # ------------------------------------------------------------------
        total_ms = (time.monotonic() - pipeline_start) * 1000
        under_60s = total_ms <= BREW_TARGET_SECONDS * 1000

        final_score = confidence_score.overall_score
        readiness_tier = confidence_score.readiness_tier

        # Write output file if requested
        if output_path:
            _write_output(
                output_path=output_path,
                source_code=final_source_code,
                test_result=test_result,
                client_result=client_result,
            )
            logger.info("Server written to %s", output_path)

        # ------------------------------------------------------------------
        # Stage 7: Registry Save (if quality threshold met)
        # ------------------------------------------------------------------
        registry_entry_id: str | None = None
        if save_to_registry and final_score >= REGISTRY_SAVE_THRESHOLD:
            try:
                tool_names = [t.name for t in generation_result.tool_definitions]
                repair_iters = repair_result.repair_iterations if repair_result else 0
                entry = self.registry.create_entry(
                    description=description,
                    source_code=final_source_code,
                    validation_score=final_score,
                    readiness_tier=readiness_tier.value,
                    brew_time_ms=total_ms,
                    tool_names=tool_names,
                    repair_iterations=repair_iters,
                )
                self.registry.save(entry)
                registry_entry_id = entry.id
                logger.info("Saved to registry [id=%s]", entry.id[:8])
            except Exception as exc:
                logger.warning("Registry save failed (non-blocking): %s", exc)

        # ------------------------------------------------------------------
        # Complete
        # ------------------------------------------------------------------
        emoji = "✅" if readiness_tier in (
            ReadinessTier.PRODUCTION_READY, ReadinessTier.STAGING_READY
        ) else "⚠️"
        self._emit(
            "complete",
            f"{emoji} Your server is ready! "
            f"Score: {final_score:.1f}/100 | {readiness_tier.value} | "
            f"{'✓ Under 60s' if under_60s else '⚠ Exceeded 60s'} ({total_ms/1000:.1f}s)",
        )

        brew_result = BrewResult(
            description=description,
            output_path=output_path,
            source_code=final_source_code,
            generation_result=generation_result,
            validation_report=final_report,
            repair_result=repair_result,
            confidence_score=confidence_score,
            test_result=test_result,
            final_score=final_score,
            readiness_tier=readiness_tier,
            total_time_ms=total_ms,
            under_60_seconds=under_60s,
            registry_entry_id=registry_entry_id,
        )

        logger.info(
            "Brew complete [brew_id=%s, total_ms=%.1f, under_60s=%s, score=%.1f, tier=%s]",
            brew_result.brew_id[:8],
            total_ms,
            under_60s,
            final_score,
            readiness_tier.value,
        )

        return brew_result

    def validate_file(self, file_path: str) -> "ValidationReportResult":
        """Validate an existing MCP server file.

        Convenience method for the ``mcpresso validate`` CLI command.

        Args:
            file_path: Path to the Python MCP server file to validate.

        Returns:
            ValidationReport from the validator.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        from mcpresso.models import ValidationReport

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        source_code = path.read_text(encoding="utf-8")
        return self.validator.validate(source_code)

    def repair_file(
        self,
        file_path: str,
        output_path: str | None = None,
    ) -> "RepairResult":
        """Repair an existing MCP server file.

        Convenience method for the ``mcpresso repair`` CLI command.

        Args:
            file_path: Path to the Python MCP server file to repair.
            output_path: Where to write the repaired file. If None, overwrites
                         the input file.

        Returns:
            RepairResult with the repaired code and audit trail.

        Raises:
            FileNotFoundError: If the input file does not exist.
        """
        from mcpresso.models import RepairResult

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        source_code = path.read_text(encoding="utf-8")
        report = self.validator.validate(source_code)
        result = self.repair_engine.repair(source_code, report)

        write_target = output_path or file_path
        Path(write_target).write_text(result.repaired_code, encoding="utf-8")
        logger.info("Repaired file written to %s", write_target)

        return result

    def _emit(self, event: str, message: str) -> None:
        """Emit a progress event to the registered callback.

        Args:
            event: Event name identifier.
            message: Human-readable progress message.
        """
        logger.debug("Pipeline event [%s]: %s", event, message)
        if self.on_progress:
            try:
                self.on_progress(event, message)
            except Exception as exc:
                logger.warning("Progress callback raised: %s", exc)


# ---------------------------------------------------------------------------
# Output Writing
# ---------------------------------------------------------------------------


def _write_output(
    output_path: str,
    source_code: str,
    test_result: "TestGenResult | None",
    client_result: "ClientGenResult | None" = None,
) -> None:
    """Write server source code and optional test/client files to disk.

    Creates parent directories if they don't exist.

    Args:
        output_path: Path for the server .py file.
        source_code: Server source code to write.
        test_result: Optional TestGenResult; if provided, writes test file
                     alongside the server as test_<name>.py.
        client_result: Optional ClientGenResult; if provided, writes a client
                       script alongside the server as client_<name>.py.
    """
    server_path = Path(output_path)
    server_path.parent.mkdir(parents=True, exist_ok=True)
    server_path.write_text(source_code, encoding="utf-8")

    if test_result is not None:
        test_path = server_path.parent / f"test_{server_path.stem}.py"
        test_path.write_text(test_result.test_file, encoding="utf-8")
        logger.info("Test suite written to %s", test_path)

    if client_result is not None:
        client_path = server_path.parent / f"client_{server_path.stem}.py"
        client_path.write_text(client_result.client_file, encoding="utf-8")
        logger.info("Client script written to %s", client_path)
