"""MCPresso Registry — Semantic MCP Server Template Registry.

This module implements the persistent registry of successfully brewed MCP servers,
with semantic similarity search to enable retrieval-augmented generation on subsequent
brew requests.

Design Decision (for paper):
    The registry implements "semantic memory" for the generation system — analogous to
    episodic memory in cognitive architectures (Tulving, 1983). Unlike a traditional
    code template library (exact match), the registry uses dense vector embeddings from
    sentence-transformers to enable fuzzy semantic retrieval: "find me servers that do
    something similar to what I'm asking."

    Three resolution tiers:
    ┌─────────────────────────────────────────────────────────────┐
    │ Similarity > 0.85  → ADAPT     → ~10s  (modify existing)   │
    │ Similarity 0.60–0.85 → SEED    → ~30s  (few-shot grounding) │
    │ Similarity < 0.60  → FULL GEN → ~60s  (from scratch)       │
    └─────────────────────────────────────────────────────────────┘

    This tiered approach enables the empirical study on "reuse rate vs. quality score"
    described in the paper's Section 5.2: as the registry grows, what fraction of
    new brews benefit from prior knowledge? Does reuse improve or degrade quality?

Storage format:
    Each registry entry is persisted as a JSON file in ~/.mcpresso/registry/<id>.json
    The embedding vector is stored inline as a list of floats.
    The full registry index is maintained as registry_index.json for fast listing.

References:
    Tulving, E. (1983). Elements of episodic memory. Oxford University Press.
    Reimers & Gurevych (2019). Sentence-BERT. EMNLP 2019.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from mcpresso.models import (
    RegistryEntry,
    RegistryMatchType,
    RegistrySearchResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY_DIR = Path.home() / ".mcpresso" / "registry"
ADAPT_THRESHOLD = 0.85   # similarity > this → ADAPT mode
SEED_THRESHOLD = 0.60    # similarity > this → SEED mode (else FULL_GENERATION)
INDEX_FILE = "registry_index.json"

# Embedding model (loaded lazily to avoid slow startup)
_EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
_embedding_model = None  # lazy singleton


# ---------------------------------------------------------------------------
# Registry Class
# ---------------------------------------------------------------------------


class MCPRegistry:
    """Persistent semantic registry for successfully brewed MCP servers.

    Stores server code, metadata, and dense embeddings. On each new brew
    request, performs cosine similarity search to determine whether to
    adapt, seed, or regenerate from scratch.

    Attributes:
        registry_dir: Path to the local registry directory.
        adapt_threshold: Cosine similarity threshold for ADAPT mode.
        seed_threshold: Cosine similarity threshold for SEED mode.

    Example:
        >>> registry = MCPRegistry()
        >>> result = registry.search("A server that queries PostgreSQL")
        >>> if result:
        ...     print(f"Match: {result.similarity:.3f} → {result.match_type.value}")

        >>> registry.save(entry)
        >>> entries = registry.list_all()
        >>> print(f"Registry size: {len(entries)} entries")
    """

    def __init__(
        self,
        registry_dir: Path | str | None = None,
        adapt_threshold: float = ADAPT_THRESHOLD,
        seed_threshold: float = SEED_THRESHOLD,
    ) -> None:
        """Initialize the MCPRegistry.

        Args:
            registry_dir: Path to registry directory. Defaults to ~/.mcpresso/registry.
            adapt_threshold: Similarity threshold for ADAPT mode (default: 0.85).
            seed_threshold: Similarity threshold for SEED mode (default: 0.60).
        """
        env_dir = os.getenv("MCPRESSO_REGISTRY_DIR")
        if registry_dir is not None:
            self.registry_dir = Path(registry_dir)
        elif env_dir:
            self.registry_dir = Path(os.path.expanduser(env_dir))
        else:
            self.registry_dir = DEFAULT_REGISTRY_DIR

        self.adapt_threshold = float(
            os.getenv("MCPRESSO_SIMILARITY_THRESHOLD_ADAPT", str(adapt_threshold))
        )
        self.seed_threshold = float(
            os.getenv("MCPRESSO_SIMILARITY_THRESHOLD_SEED", str(seed_threshold))
        )

        self.registry_dir.mkdir(parents=True, exist_ok=True)
        logger.info("MCPRegistry initialized [dir=%s]", self.registry_dir)

    # ------------------------------------------------------------------
    # Core Operations
    # ------------------------------------------------------------------

    def search(self, description: str) -> RegistrySearchResult | None:
        """Search the registry for the most semantically similar entry.

        Computes a dense embedding of the input description and finds the
        registry entry with highest cosine similarity. Returns None if the
        registry is empty.

        Args:
            description: Natural language description of the desired server.

        Returns:
            RegistrySearchResult with the best match and its match type,
            or None if the registry is empty or no match exceeds seed_threshold.
        """
        entries = self.list_all()
        if not entries:
            logger.debug("Registry is empty; no search performed.")
            return None

        query_embedding = _embed(description)
        best_entry: RegistryEntry | None = None
        best_sim = -1.0

        for entry in entries:
            if not entry.embedding:
                continue
            sim = _cosine_similarity(
                np.array(query_embedding, dtype=np.float32),
                np.array(entry.embedding, dtype=np.float32),
            )
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry is None:
            return None

        match_type = _resolve_match_type(best_sim, self.adapt_threshold, self.seed_threshold)
        logger.info(
            "Registry search complete [best_sim=%.3f, match_type=%s, entry_id=%s]",
            best_sim,
            match_type.value,
            best_entry.id[:8],
        )

        return RegistrySearchResult(
            entry=best_entry,
            similarity=best_sim,
            match_type=match_type,
        )

    def save(self, entry: RegistryEntry) -> None:
        """Persist a registry entry to disk.

        Writes the entry as a JSON file and updates the registry index.

        Args:
            entry: RegistryEntry to persist.
        """
        entry_path = self.registry_dir / f"{entry.id}.json"
        entry_dict = _serialize_entry(entry)

        with open(entry_path, "w", encoding="utf-8") as f:
            json.dump(entry_dict, f, indent=2, default=str)

        self._update_index(entry)
        logger.info("Saved registry entry [id=%s, score=%.1f]", entry.id[:8], entry.validation_score)

    def get(self, entry_id: str) -> RegistryEntry | None:
        """Retrieve a specific registry entry by ID.

        Args:
            entry_id: UUID string of the registry entry.

        Returns:
            RegistryEntry if found, else None.
        """
        entry_path = self.registry_dir / f"{entry_id}.json"
        if not entry_path.exists():
            logger.warning("Registry entry not found: %s", entry_id)
            return None
        return _load_entry(entry_path)

    def list_all(self) -> list[RegistryEntry]:
        """List all registry entries sorted by creation date (newest first).

        Returns:
            List of all RegistryEntry objects in the registry.
        """
        index = self._load_index()
        entries: list[RegistryEntry] = []
        for entry_id in index.get("entry_ids", []):
            entry = self.get(entry_id)
            if entry is not None:
                entries.append(entry)

        # Sort by creation date, newest first
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    def delete(self, entry_id: str) -> bool:
        """Delete a registry entry by ID.

        Args:
            entry_id: UUID string of the entry to delete.

        Returns:
            True if deleted successfully, False if not found.
        """
        entry_path = self.registry_dir / f"{entry_id}.json"
        if not entry_path.exists():
            return False

        entry_path.unlink()
        self._remove_from_index(entry_id)
        logger.info("Deleted registry entry: %s", entry_id)
        return True

    def create_entry(
        self,
        description: str,
        source_code: str,
        validation_score: float,
        readiness_tier: str,
        brew_time_ms: float,
        tool_names: list[str] | None = None,
        repair_iterations: int = 0,
    ) -> RegistryEntry:
        """Create a new RegistryEntry with computed embedding and auto-extracted tags.

        This is a convenience factory method that handles embedding computation
        and tag extraction automatically.

        Args:
            description: Original NL description.
            source_code: Final validated server source code.
            validation_score: Overall validation score.
            readiness_tier: Execution readiness tier string.
            brew_time_ms: Total brew time in milliseconds.
            tool_names: Names of tools in this server.
            repair_iterations: Number of repair passes performed.

        Returns:
            A new RegistryEntry ready to be saved.
        """
        embedding = _embed(description)
        tags = _extract_tags(description, source_code)

        return RegistryEntry(
            id=str(uuid.uuid4()),
            description=description,
            embedding=embedding,
            source_code=source_code,
            validation_score=validation_score,
            readiness_tier=readiness_tier,
            tags=tags,
            created_at=datetime.now(timezone.utc),
            brew_time_ms=brew_time_ms,
            tool_names=tool_names or [],
            repair_iterations=repair_iterations,
        )

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    def export(self, output_path: Path | str) -> None:
        """Export the entire registry to a single JSON file.

        The exported format is suitable for sharing between machines and
        for use as benchmark datasets in the paper's evaluation section.

        Args:
            output_path: Path to write the export JSON file.
        """
        entries = self.list_all()
        export_data = {
            "mcpresso_registry_version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "entry_count": len(entries),
            "entries": [_serialize_entry(e) for e in entries],
        }

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, default=str)

        logger.info("Exported %d registry entries to %s", len(entries), output_path)

    def import_from(self, input_path: Path | str) -> int:
        """Import registry entries from an exported JSON file.

        Skips entries that already exist (by ID). Returns the count of
        newly imported entries.

        Args:
            input_path: Path to the export JSON file.

        Returns:
            Number of entries newly imported.
        """
        input_path = Path(input_path)
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        existing_ids = {e.id for e in self.list_all()}
        imported = 0

        for entry_dict in data.get("entries", []):
            entry = _deserialize_entry(entry_dict)
            if entry.id not in existing_ids:
                self.save(entry)
                imported += 1

        logger.info("Imported %d new entries from %s", imported, input_path)
        return imported

    # ------------------------------------------------------------------
    # Statistics (for benchmark/paper metrics)
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, object]:
        """Compute summary statistics for the registry.

        Returns a dict suitable for inclusion in benchmark reports.

        Returns:
            Dictionary with entry_count, mean_score, tier_distribution,
            mean_brew_time_ms, and reuse_potential_rate.
        """
        entries = self.list_all()
        if not entries:
            return {"entry_count": 0}

        scores = [e.validation_score for e in entries]
        brew_times = [e.brew_time_ms for e in entries]
        tier_counts: dict[str, int] = {}
        for e in entries:
            tier_counts[e.readiness_tier] = tier_counts.get(e.readiness_tier, 0) + 1

        return {
            "entry_count": len(entries),
            "mean_score": float(np.mean(scores)),
            "std_score": float(np.std(scores)),
            "min_score": float(np.min(scores)),
            "max_score": float(np.max(scores)),
            "tier_distribution": tier_counts,
            "mean_brew_time_ms": float(np.mean(brew_times)),
            "total_entries_by_tier": tier_counts,
        }

    # ------------------------------------------------------------------
    # Index Management
    # ------------------------------------------------------------------

    def _load_index(self) -> dict:
        """Load the registry index from disk.

        Returns:
            Index dict with 'entry_ids' list.
        """
        index_path = self.registry_dir / INDEX_FILE
        if not index_path.exists():
            return {"entry_ids": []}
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as exc:
            logger.warning("Failed to load registry index: %s. Starting fresh.", exc)
            return {"entry_ids": []}

    def _save_index(self, index: dict) -> None:
        """Persist the registry index to disk.

        Args:
            index: Index dict to persist.
        """
        index_path = self.registry_dir / INDEX_FILE
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)

    def _update_index(self, entry: RegistryEntry) -> None:
        """Add an entry ID to the registry index if not already present.

        Args:
            entry: The entry to add to the index.
        """
        index = self._load_index()
        if entry.id not in index.get("entry_ids", []):
            index.setdefault("entry_ids", []).append(entry.id)
            self._save_index(index)

    def _remove_from_index(self, entry_id: str) -> None:
        """Remove an entry ID from the registry index.

        Args:
            entry_id: ID to remove.
        """
        index = self._load_index()
        ids = index.get("entry_ids", [])
        if entry_id in ids:
            ids.remove(entry_id)
            index["entry_ids"] = ids
            self._save_index(index)


# ---------------------------------------------------------------------------
# Embedding Functions
# ---------------------------------------------------------------------------


def _get_embedding_model():
    """Lazily load the sentence-transformers embedding model.

    Returns:
        A SentenceTransformer model instance.

    Raises:
        ImportError: If sentence-transformers is not installed.
    """
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading embedding model: %s", _EMBEDDING_MODEL_NAME)
            _embedding_model = SentenceTransformer(_EMBEDDING_MODEL_NAME)
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is required for registry functionality. "
                "Install it with: pip install sentence-transformers"
            ) from exc
    return _embedding_model


def _embed(text: str) -> list[float]:
    """Compute a dense embedding for a text string.

    Uses the sentence-transformers all-MiniLM-L6-v2 model (384 dimensions).
    Falls back to a zero vector if the model cannot be loaded, allowing
    basic functionality without the embedding dependency.

    Args:
        text: Text string to embed.

    Returns:
        List of floats representing the dense embedding vector.
    """
    try:
        model = _get_embedding_model()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    except Exception as exc:
        logger.warning(
            "Embedding computation failed: %s. Using zero vector fallback.", exc
        )
        return [0.0] * 384


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two embedding vectors.

    Args:
        a: First embedding vector.
        b: Second embedding vector.

    Returns:
        Cosine similarity in range [-1.0, 1.0]. Higher is more similar.
    """
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


# ---------------------------------------------------------------------------
# Tag Extraction
# ---------------------------------------------------------------------------


# Keywords mapped to semantic tags
_TAG_KEYWORDS: dict[str, list[str]] = {
    "github": ["github", "git", "pull request", "issue", "repository", "repo"],
    "database": ["database", "sql", "postgres", "postgresql", "mysql", "sqlite", "db", "query"],
    "api": ["api", "rest", "endpoint", "http", "webhook", "request"],
    "slack": ["slack", "channel", "message", "notification"],
    "search": ["search", "find", "query", "lookup", "retrieve"],
    "file": ["file", "filesystem", "read", "write", "directory", "path"],
    "email": ["email", "smtp", "send mail", "gmail", "mailbox"],
    "weather": ["weather", "forecast", "temperature", "climate"],
    "ai": ["ai", "llm", "openai", "anthropic", "claude", "gpt", "summarize", "generate"],
    "monitoring": ["monitor", "metrics", "logs", "alert", "health"],
    "auth": ["auth", "oauth", "token", "login", "authentication"],
    "cloud": ["aws", "gcp", "azure", "s3", "bucket", "lambda"],
    "jira": ["jira", "ticket", "sprint", "project management"],
    "web": ["web", "browser", "scrape", "crawl", "html", "url"],
}


def _extract_tags(description: str, source_code: str) -> list[str]:
    """Auto-extract semantic tags from description and source code.

    Tags enable filtering and categorization of registry entries.

    Args:
        description: NL description of the server.
        source_code: Generated server source code.

    Returns:
        List of tag strings (e.g., ["github", "api", "search"]).
    """
    combined = (description + " " + source_code).lower()
    tags: list[str] = []

    for tag, keywords in _TAG_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            tags.append(tag)

    return sorted(set(tags))


# ---------------------------------------------------------------------------
# Match Type Resolution
# ---------------------------------------------------------------------------


def _resolve_match_type(
    similarity: float,
    adapt_threshold: float,
    seed_threshold: float,
) -> RegistryMatchType:
    """Determine the registry match type based on cosine similarity.

    Args:
        similarity: Cosine similarity score [0.0, 1.0].
        adapt_threshold: Threshold above which ADAPT mode is used.
        seed_threshold: Threshold above which SEED mode is used.

    Returns:
        RegistryMatchType indicating how to use the match.
    """
    if similarity >= adapt_threshold:
        return RegistryMatchType.ADAPT
    elif similarity >= seed_threshold:
        return RegistryMatchType.SEED
    else:
        return RegistryMatchType.FULL_GENERATION


# ---------------------------------------------------------------------------
# Serialization Helpers
# ---------------------------------------------------------------------------


def _serialize_entry(entry: RegistryEntry) -> dict:
    """Serialize a RegistryEntry to a JSON-compatible dict.

    Args:
        entry: RegistryEntry to serialize.

    Returns:
        JSON-serializable dict representation.
    """
    return {
        "id": entry.id,
        "description": entry.description,
        "embedding": entry.embedding,
        "source_code": entry.source_code,
        "validation_score": entry.validation_score,
        "readiness_tier": entry.readiness_tier,
        "tags": entry.tags,
        "created_at": entry.created_at.isoformat(),
        "brew_time_ms": entry.brew_time_ms,
        "tool_names": entry.tool_names,
        "repair_iterations": entry.repair_iterations,
    }


def _deserialize_entry(data: dict) -> RegistryEntry:
    """Deserialize a RegistryEntry from a JSON dict.

    Args:
        data: Dict loaded from JSON storage.

    Returns:
        RegistryEntry instance.
    """
    created_at = data.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError:
            created_at = datetime.now(timezone.utc)
    elif not isinstance(created_at, datetime):
        created_at = datetime.now(timezone.utc)

    return RegistryEntry(
        id=data.get("id", str(uuid.uuid4())),
        description=data.get("description", ""),
        embedding=data.get("embedding", []),
        source_code=data.get("source_code", ""),
        validation_score=float(data.get("validation_score", 0.0)),
        readiness_tier=data.get("readiness_tier", "NEEDS_REPAIR"),
        tags=data.get("tags", []),
        created_at=created_at,
        brew_time_ms=float(data.get("brew_time_ms", 0.0)),
        tool_names=data.get("tool_names", []),
        repair_iterations=int(data.get("repair_iterations", 0)),
    )


def _load_entry(path: Path) -> RegistryEntry | None:
    """Load a RegistryEntry from a JSON file path.

    Args:
        path: Path to the JSON entry file.

    Returns:
        Loaded RegistryEntry, or None if loading fails.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _deserialize_entry(data)
    except (json.JSONDecodeError, IOError, KeyError) as exc:
        logger.warning("Failed to load registry entry from %s: %s", path, exc)
        return None
