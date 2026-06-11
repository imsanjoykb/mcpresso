"""MCPresso Repair Engine — Intelligent Auto-Repair for Generated MCP Servers.

This module implements the iterative repair loop that automatically fixes critical
issues identified by the validation engine. It uses Claude to understand the context
of each issue and generate targeted, minimal fixes — maintaining an audit trail of
every change for research reproducibility.

Design Decision (for paper):
    The repair engine implements "agentic self-correction" — a pattern where an LLM
    iteratively evaluates its own output, identifies deficiencies, and generates
    improved versions. Key design choices:

    1. Issue-focused prompting: each repair call includes the specific issues to fix,
       preventing the model from making unnecessary changes to working code.
    2. Iteration cap (max 3): prevents infinite loops and bounds API cost. After 3
       passes, remaining issues are surfaced to the user with human-readable guidance.
    3. Audit trail (FixRecord): every change is logged with before/after snippets and
       the issue ID it resolved — enabling reproducibility studies in the paper.
    4. Validation-gated iterations: each iteration validates the repaired code before
       deciding whether to continue, ensuring we stop as soon as the threshold is met.

References:
    Self-Refine: Iterative Refinement with Self-Feedback (Madaan et al., 2023)
    Reflexion: Language Agents with Verbal Reinforcement Learning (Shinn et al., 2023)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

import anthropic
from dotenv import load_dotenv

from mcpresso.models import (
    FixRecord,
    Issue,
    IssueSeverity,
    RepairResult,
    ValidationReport,
)
from mcpresso.validator import MCPValidator

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 8192
MAX_REPAIR_ITERATIONS = 3
REPAIR_SUCCESS_THRESHOLD = 75.0

_REPAIR_SYSTEM_PROMPT = """\
You are an expert MCP (Model Context Protocol) server engineer performing a targeted
code repair. You will be given:
1. The current Python source code of an MCP server
2. A list of specific issues that must be fixed

Your task is to fix ALL listed issues while making the MINIMUM necessary changes.
Do not restructure code that is working. Do not add unnecessary features.
Do not change working logic unless it is directly related to a listed issue.

## Output Format
Wrap the COMPLETE repaired Python source code between these exact delimiters:
<REPAIRED_START>
# complete repaired code here
<REPAIRED_END>

After the closing delimiter, add a brief JSON audit block listing what you changed:
<CHANGES_START>
[
  {
    "issue_id": "abc12345",
    "description": "What was changed and why",
    "before": "the old code snippet",
    "after": "the new code snippet"
  }
]
<CHANGES_END>

Be precise. Do not hallucinate issue IDs — only reference the exact IDs provided.
"""


# ---------------------------------------------------------------------------
# Repair Engine Class
# ---------------------------------------------------------------------------


class MCPRepairEngine:
    """Intelligent auto-repair engine for generated MCP servers.

    Uses Claude to fix critical validation issues through an iterative
    refinement loop, maintaining a complete audit trail of all changes.

    The repair loop works as follows:
        1. Run validation to get initial issues.
        2. Build a repair prompt from critical issues.
        3. Call Claude to fix the issues.
        4. Re-validate the repaired code.
        5. If score >= 75 or max iterations reached, stop.
        6. Otherwise, repeat with remaining critical issues.

    Attributes:
        model: Anthropic model identifier for repair.
        max_tokens: Maximum completion tokens.
        max_iterations: Maximum repair iterations (default: 3).
        client: Anthropic API client.
        validator: MCPValidator instance for post-repair validation.

    Example:
        >>> engine = MCPRepairEngine()
        >>> result = engine.repair(source_code, validation_report)
        >>> print(f"Iterations: {result.repair_iterations}")
        >>> print(f"Score: {result.initial_score:.1f} → {result.final_score:.1f}")
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_iterations: int = MAX_REPAIR_ITERATIONS,
        api_key: str | None = None,
        on_iteration: Callable[[int, float], None] | None = None,
    ) -> None:
        """Initialize the MCPRepairEngine.

        Args:
            model: Anthropic model to use for repair.
            max_tokens: Maximum tokens for repair completion.
            max_iterations: Maximum repair iterations (default: 3).
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            on_iteration: Optional callback called after each repair iteration
                          with (iteration_number, current_score).

        Raises:
            ValueError: If no API key is available.
        """
        self.model = model or os.getenv("MCPRESSO_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self.on_iteration = on_iteration

        resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key not found. Set ANTHROPIC_API_KEY in your environment "
                "or .env file, or pass api_key= to MCPRepairEngine()."
            )
        self.client = anthropic.Anthropic(api_key=resolved_key)
        self.validator = MCPValidator()
        logger.info("MCPRepairEngine initialized [model=%s, max_iterations=%d]",
                    self.model, self.max_iterations)

    def repair(
        self,
        source_code: str,
        validation_report: ValidationReport,
    ) -> RepairResult:
        """Run the iterative repair loop to fix critical issues.

        Attempts to resolve all critical issues through up to ``max_iterations``
        repair passes. Each pass re-validates the code and only continues if
        there are still critical issues remaining below the success threshold.

        Args:
            source_code: Current Python source code to repair.
            validation_report: Validation report from the initial validation run.

        Returns:
            RepairResult with repaired code, full audit trail, iteration count,
            and final validation report.
        """
        initial_score = validation_report.overall_score
        logger.info(
            "Starting repair [initial_score=%.1f, critical_issues=%d]",
            initial_score,
            len(validation_report.critical_issues),
        )

        # Fast exit: nothing to repair
        if not validation_report.critical_issues and initial_score >= REPAIR_SUCCESS_THRESHOLD:
            logger.info("No critical issues found; skipping repair.")
            return RepairResult(
                repaired_code=source_code,
                fixes_applied=[],
                repair_iterations=0,
                initial_score=initial_score,
                final_score=initial_score,
                final_report=validation_report,
                success=validation_report.execution_ready,
                remaining_issues=validation_report.warnings,
            )

        current_code = source_code
        current_report = validation_report
        all_fixes: list[FixRecord] = []
        iteration = 0

        while (
            iteration < self.max_iterations
            and (
                current_report.critical_issues
                or current_report.overall_score < REPAIR_SUCCESS_THRESHOLD
            )
        ):
            iteration += 1
            # Only repair critical issues — leave warnings for human review
            issues_to_fix = current_report.critical_issues
            if not issues_to_fix:
                # No critical issues left; bump up with warnings
                issues_to_fix = current_report.warnings[:5]

            logger.info(
                "Repair iteration %d/%d [issues_to_fix=%d, current_score=%.1f]",
                iteration, self.max_iterations,
                len(issues_to_fix),
                current_report.overall_score,
            )

            repaired, fixes = self._repair_iteration(current_code, issues_to_fix)
            all_fixes.extend(fixes)
            current_code = repaired

            # Re-validate after repair
            current_report = self.validator.validate(current_code)
            logger.info(
                "Post-repair validation [iteration=%d, score=%.1f, critical=%d]",
                iteration,
                current_report.overall_score,
                len(current_report.critical_issues),
            )

            if self.on_iteration:
                self.on_iteration(iteration, current_report.overall_score)

            # Stop early if we've reached the threshold
            if (
                current_report.overall_score >= REPAIR_SUCCESS_THRESHOLD
                and not current_report.critical_issues
            ):
                logger.info("Repair threshold reached after %d iteration(s).", iteration)
                break

        success = (
            current_report.overall_score >= REPAIR_SUCCESS_THRESHOLD
            and not current_report.critical_issues
        )

        if not success:
            logger.warning(
                "Repair did not reach threshold after %d iteration(s). "
                "Final score: %.1f. Remaining critical: %d",
                iteration,
                current_report.overall_score,
                len(current_report.critical_issues),
            )

        return RepairResult(
            repaired_code=current_code,
            fixes_applied=all_fixes,
            repair_iterations=iteration,
            initial_score=initial_score,
            final_score=current_report.overall_score,
            final_report=current_report,
            success=success,
            remaining_issues=current_report.critical_issues + current_report.warnings,
        )

    def _repair_iteration(
        self,
        source_code: str,
        issues: list[Issue],
    ) -> tuple[str, list[FixRecord]]:
        """Perform a single repair iteration.

        Builds a structured repair prompt listing all issues to fix,
        calls the Claude API, extracts the repaired code and change log,
        and returns both for audit recording.

        Args:
            source_code: Current source code to repair.
            issues: List of issues that must be fixed in this iteration.

        Returns:
            Tuple of (repaired_source_code, list_of_fix_records).
        """
        prompt = _build_repair_prompt(source_code, issues)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_REPAIR_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw_text = response.content[0].text
            logger.debug(
                "Repair API call complete [tokens_used=%d]",
                response.usage.input_tokens + response.usage.output_tokens,
            )
        except anthropic.APIError as exc:
            logger.error("Anthropic API error during repair: %s", exc)
            # Return original code unchanged on API failure
            return source_code, []

        repaired_code = _extract_repaired_code(raw_text, source_code)
        fix_records = _extract_fix_records(raw_text, issues)

        return repaired_code, fix_records


# ---------------------------------------------------------------------------
# Prompt Building
# ---------------------------------------------------------------------------


def _build_repair_prompt(source_code: str, issues: list[Issue]) -> str:
    """Build a structured repair prompt listing all issues to fix.

    Args:
        source_code: Current source code.
        issues: Issues that must be fixed.

    Returns:
        Formatted prompt string for the repair API call.
    """
    issues_text_parts = []
    for issue in issues:
        parts = [
            f"Issue ID: {issue.id}",
            f"Severity: {issue.severity.value}",
            f"Category: {issue.category}",
            f"Problem: {issue.message}",
        ]
        if issue.line_number:
            parts.append(f"Line: {issue.line_number}")
        if issue.code_snippet:
            parts.append(f"Snippet: {issue.code_snippet}")
        if issue.fix_suggestion:
            parts.append(f"Suggested Fix: {issue.fix_suggestion}")
        issues_text_parts.append("\n".join(parts))

    issues_text = "\n\n---\n\n".join(issues_text_parts)

    return (
        f"Please fix the following {len(issues)} issue(s) in this MCP server:\n\n"
        f"## Issues to Fix\n\n{issues_text}\n\n"
        f"## Current Source Code\n\n```python\n{source_code}\n```\n\n"
        f"Fix ALL listed issues. Return the complete repaired code and change log."
    )


# ---------------------------------------------------------------------------
# Response Parsing
# ---------------------------------------------------------------------------


def _extract_repaired_code(response_text: str, fallback: str) -> str:
    """Extract repaired source code from the repair API response.

    Looks for content between <REPAIRED_START> and <REPAIRED_END> delimiters.
    Falls back to markdown code fences, then returns the original code unchanged.

    Args:
        response_text: Raw API response text.
        fallback: Original source code to return if extraction fails.

    Returns:
        Extracted repaired Python source code.
    """
    import re

    # Strategy 1: explicit delimiters
    match = re.search(r"<REPAIRED_START>\s*(.*?)\s*<REPAIRED_END>", response_text, re.DOTALL)
    if match:
        code = match.group(1).strip()
        # Strip markdown fence if present inside delimiters
        fence_match = re.match(r"```(?:python)?\s*(.*?)\s*```", code, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()
        return code

    # Strategy 2: markdown python fence
    match = re.search(r"```python\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Strategy 3: any markdown fence
    match = re.search(r"```\s*(.*?)\s*```", response_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    logger.warning("Could not extract repaired code from response; returning original.")
    return fallback


def _extract_fix_records(response_text: str, issues: list[Issue]) -> list[FixRecord]:
    """Extract fix audit records from the repair API response.

    Parses the <CHANGES_START> / <CHANGES_END> JSON block from the response.
    Falls back to creating minimal records from issue descriptions if parsing fails.

    Args:
        response_text: Raw API response text.
        issues: Original issues submitted for repair (used as fallback).

    Returns:
        List of FixRecord objects representing applied changes.
    """
    import json
    import re

    # Try to parse structured change log
    match = re.search(r"<CHANGES_START>\s*(.*?)\s*<CHANGES_END>", response_text, re.DOTALL)
    if match:
        try:
            changes = json.loads(match.group(1).strip())
            records = []
            for change in changes:
                records.append(FixRecord(
                    issue_id=str(change.get("issue_id", "unknown")),
                    description=str(change.get("description", "Applied fix")),
                    before_snippet=str(change.get("before", "")),
                    after_snippet=str(change.get("after", "")),
                    category=_get_issue_category(
                        str(change.get("issue_id", "")), issues
                    ),
                ))
            return records
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to parse change log JSON: %s", exc)

    # Fallback: create minimal records from the issues list
    return [
        FixRecord(
            issue_id=issue.id,
            description=f"Attempted fix for: {issue.message}",
            before_snippet=issue.code_snippet or "",
            after_snippet="(see repaired code)",
            category=issue.category,
        )
        for issue in issues
    ]


def _get_issue_category(issue_id: str, issues: list[Issue]) -> str:
    """Look up category for a given issue ID.

    Args:
        issue_id: Issue identifier to look up.
        issues: List of issues to search.

    Returns:
        Category string if found, else 'unknown'.
    """
    for issue in issues:
        if issue.id == issue_id:
            return issue.category
    return "unknown"


# ---------------------------------------------------------------------------
# Human-Readable Fix Suggestions (for issues that couldn't be auto-fixed)
# ---------------------------------------------------------------------------


def format_remaining_issues(repair_result: RepairResult) -> str:
    """Format unresolved issues as a human-readable remediation guide.

    Called when the repair engine exhausts its iterations and the code
    still has unresolved issues. Provides actionable guidance for manual
    intervention.

    Args:
        repair_result: The completed (but potentially unsuccessful) repair result.

    Returns:
        Formatted multi-line string with issue-by-issue remediation guidance.
    """
    if not repair_result.remaining_issues:
        return "✅ All issues resolved successfully."

    lines = [
        f"⚠️  {len(repair_result.remaining_issues)} issue(s) could not be automatically resolved",
        f"   after {repair_result.repair_iterations} repair iteration(s).",
        f"   Final validation score: {repair_result.final_score:.1f}/100",
        "",
        "Manual remediation required:",
        "",
    ]

    critical = [i for i in repair_result.remaining_issues if i.severity == IssueSeverity.CRITICAL]
    warnings = [i for i in repair_result.remaining_issues if i.severity == IssueSeverity.WARNING]

    if critical:
        lines.append(f"🔴 CRITICAL ({len(critical)} issues):")
        for i, issue in enumerate(critical, 1):
            lines.append(f"  {i}. [{issue.category}] {issue.message}")
            if issue.fix_suggestion:
                lines.append(f"     Fix: {issue.fix_suggestion}")
            lines.append("")

    if warnings:
        lines.append(f"🟡 WARNINGS ({len(warnings)} issues):")
        for i, issue in enumerate(warnings, 1):
            lines.append(f"  {i}. [{issue.category}] {issue.message}")
            if issue.fix_suggestion:
                lines.append(f"     Fix: {issue.fix_suggestion}")
            lines.append("")

    return "\n".join(lines)
