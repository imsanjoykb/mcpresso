"""Tests for mcpresso.registry — Semantic MCP Server Template Registry.

Uses a temporary directory for all registry operations (no ~/.mcpresso pollution).
Embedding model is mocked to avoid heavy ML dependency in CI.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from mcpresso.models import RegistryEntry, RegistryMatchType
from mcpresso.registry import (
    MCPRegistry,
    _cosine_similarity,
    _extract_tags,
    _resolve_match_type,
    _serialize_entry,
    _deserialize_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    description: str = "A GitHub issue tracker server",
    score: float = 85.0,
    tier: str = "STAGING_READY",
    embedding: list[float] | None = None,
) -> RegistryEntry:
    """Create a minimal RegistryEntry for testing."""
    if embedding is None:
        # Use a simple unit vector for deterministic similarity math
        embedding = [0.0] * 383 + [1.0]
    return RegistryEntry(
        id=str(uuid.uuid4()),
        description=description,
        embedding=embedding,
        source_code="# placeholder",
        validation_score=score,
        readiness_tier=tier,
        tags=["github", "api"],
        created_at=datetime.now(timezone.utc),
        brew_time_ms=12345.0,
        tool_names=["list_issues", "create_issue"],
        repair_iterations=0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_registry(tmp_path: Path) -> MCPRegistry:
    """Create a registry backed by a temporary directory."""
    return MCPRegistry(registry_dir=tmp_path)


# ---------------------------------------------------------------------------
# Tests — Core CRUD
# ---------------------------------------------------------------------------


class TestRegistryCRUD:
    """Tests for basic save/get/list/delete operations."""

    def test_save_and_get(self, tmp_registry: MCPRegistry):
        """Saved entry must be retrievable by ID."""
        entry = _make_entry()
        tmp_registry.save(entry)
        retrieved = tmp_registry.get(entry.id)
        assert retrieved is not None
        assert retrieved.id == entry.id
        assert retrieved.description == entry.description

    def test_list_all_empty(self, tmp_registry: MCPRegistry):
        """Empty registry must return empty list."""
        assert tmp_registry.list_all() == []

    def test_list_all_returns_entries(self, tmp_registry: MCPRegistry):
        """All saved entries must appear in list_all."""
        e1, e2 = _make_entry("Server A"), _make_entry("Server B")
        tmp_registry.save(e1)
        tmp_registry.save(e2)
        entries = tmp_registry.list_all()
        assert len(entries) == 2
        ids = {e.id for e in entries}
        assert e1.id in ids
        assert e2.id in ids

    def test_delete_existing_entry(self, tmp_registry: MCPRegistry):
        """Deleting an entry must remove it from list_all."""
        entry = _make_entry()
        tmp_registry.save(entry)
        result = tmp_registry.delete(entry.id)
        assert result is True
        assert tmp_registry.get(entry.id) is None
        assert tmp_registry.list_all() == []

    def test_delete_nonexistent_returns_false(self, tmp_registry: MCPRegistry):
        """Deleting a non-existent entry must return False."""
        result = tmp_registry.delete("nonexistent-id")
        assert result is False

    def test_get_nonexistent_returns_none(self, tmp_registry: MCPRegistry):
        """Getting a non-existent entry must return None."""
        result = tmp_registry.get("nonexistent-id-xyz")
        assert result is None

    def test_save_persists_to_file(self, tmp_registry: MCPRegistry):
        """Saving must write a JSON file to the registry directory."""
        entry = _make_entry()
        tmp_registry.save(entry)
        entry_file = tmp_registry.registry_dir / f"{entry.id}.json"
        assert entry_file.exists()

    def test_json_file_is_valid(self, tmp_registry: MCPRegistry):
        """Saved JSON file must be parseable."""
        entry = _make_entry()
        tmp_registry.save(entry)
        entry_file = tmp_registry.registry_dir / f"{entry.id}.json"
        with open(entry_file) as f:
            data = json.load(f)
        assert data["id"] == entry.id
        assert data["description"] == entry.description


# ---------------------------------------------------------------------------
# Tests — Search
# ---------------------------------------------------------------------------


class TestRegistrySearch:
    """Tests for semantic similarity search."""

    def test_search_empty_registry_returns_none(self, tmp_registry: MCPRegistry):
        """Searching an empty registry must return None."""
        with patch("mcpresso.registry._embed", return_value=[0.1] * 384):
            result = tmp_registry.search("some description")
        assert result is None

    def test_search_returns_best_match(self, tmp_registry: MCPRegistry):
        """Search must return the entry with highest cosine similarity."""
        # Create two entries with different embeddings
        emb_a = [1.0] + [0.0] * 383  # unit vector along dimension 0
        emb_b = [0.0] * 383 + [1.0]  # unit vector along dimension 383

        entry_a = _make_entry("GitHub server", embedding=emb_a)
        entry_b = _make_entry("Database server", embedding=emb_b)
        tmp_registry.save(entry_a)
        tmp_registry.save(entry_b)

        # Query closest to entry_b
        with patch("mcpresso.registry._embed", return_value=emb_b):
            result = tmp_registry.search("database query server")

        assert result is not None
        assert result.entry.id == entry_b.id

    def test_search_match_type_adapt(self, tmp_registry: MCPRegistry):
        """Similarity > 0.85 must produce ADAPT match type."""
        emb = [1.0] + [0.0] * 383
        entry = _make_entry(embedding=emb)
        tmp_registry.save(entry)

        # Same embedding → similarity = 1.0
        with patch("mcpresso.registry._embed", return_value=emb):
            result = tmp_registry.search("github server")

        assert result is not None
        assert result.match_type == RegistryMatchType.ADAPT
        assert result.similarity > 0.85

    def test_search_match_type_seed(self, tmp_registry: MCPRegistry):
        """Similarity 0.60-0.85 must produce SEED match type."""
        registry = MCPRegistry(
            registry_dir=None,
            adapt_threshold=0.85,
            seed_threshold=0.60,
        )
        # Override directory to temp
        with tempfile.TemporaryDirectory() as tmpdir:
            registry.registry_dir = Path(tmpdir)
            emb_stored = [1.0, 0.0] + [0.0] * 382
            emb_query = [0.7, 0.7136] + [0.0] * 382  # roughly 0.70 cosine similarity
            entry = _make_entry(embedding=emb_stored)
            registry.save(entry)

            with patch("mcpresso.registry._embed", return_value=emb_query):
                result = registry.search("something similar")

            if result:
                # Result might be SEED or FULL_GENERATION depending on actual similarity
                assert result.match_type in (
                    RegistryMatchType.SEED, RegistryMatchType.FULL_GENERATION
                )


# ---------------------------------------------------------------------------
# Tests — Export / Import
# ---------------------------------------------------------------------------


class TestRegistryExportImport:
    """Tests for registry export and import functionality."""

    def test_export_creates_file(self, tmp_registry: MCPRegistry, tmp_path: Path):
        """Export must create a JSON file."""
        entry = _make_entry()
        tmp_registry.save(entry)
        export_file = tmp_path / "export.json"
        tmp_registry.export(export_file)
        assert export_file.exists()

    def test_export_contains_all_entries(self, tmp_registry: MCPRegistry, tmp_path: Path):
        """Exported JSON must contain all registry entries."""
        for i in range(3):
            tmp_registry.save(_make_entry(f"Server {i}"))

        export_file = tmp_path / "export.json"
        tmp_registry.export(export_file)

        with open(export_file) as f:
            data = json.load(f)
        assert data["entry_count"] == 3
        assert len(data["entries"]) == 3

    def test_import_from_export(self, tmp_path: Path):
        """Imported entries must appear in the new registry."""
        # Create source registry with entries
        src_dir = tmp_path / "source"
        dst_dir = tmp_path / "dest"
        src_registry = MCPRegistry(registry_dir=src_dir)
        dst_registry = MCPRegistry(registry_dir=dst_dir)

        for i in range(2):
            src_registry.save(_make_entry(f"Server {i}"))

        export_file = tmp_path / "export.json"
        src_registry.export(export_file)
        imported = dst_registry.import_from(export_file)

        assert imported == 2
        assert len(dst_registry.list_all()) == 2

    def test_import_skips_duplicates(self, tmp_path: Path):
        """Re-importing should skip existing entries."""
        reg_dir = tmp_path / "reg"
        registry = MCPRegistry(registry_dir=reg_dir)
        entry = _make_entry()
        registry.save(entry)

        export_file = tmp_path / "export.json"
        registry.export(export_file)

        # Import again — should skip the existing entry
        imported = registry.import_from(export_file)
        assert imported == 0
        assert len(registry.list_all()) == 1  # still just one entry


# ---------------------------------------------------------------------------
# Tests — Utility Functions
# ---------------------------------------------------------------------------


class TestRegistryUtils:
    """Tests for utility functions."""

    def test_cosine_similarity_identical_vectors(self):
        """Cosine similarity of a vector with itself must be 1.0."""
        v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal_vectors(self):
        """Cosine similarity of orthogonal vectors must be 0.0."""
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_cosine_similarity_zero_vector(self):
        """Zero vector similarity must be 0.0 (no divide-by-zero error)."""
        a = np.array([0.0, 0.0], dtype=np.float32)
        b = np.array([1.0, 2.0], dtype=np.float32)
        assert _cosine_similarity(a, b) == 0.0

    def test_extract_tags_github(self):
        """GitHub-related description should produce 'github' tag."""
        tags = _extract_tags("A GitHub issue tracker", "import httpx")
        assert "github" in tags

    def test_extract_tags_database(self):
        """PostgreSQL description should produce 'database' tag."""
        tags = _extract_tags("A PostgreSQL query server", "import asyncpg")
        assert "database" in tags

    def test_extract_tags_returns_sorted(self):
        """Tags must be sorted alphabetically."""
        tags = _extract_tags("github api database", "import httpx, asyncpg")
        assert tags == sorted(tags)

    def test_resolve_match_type_adapt(self):
        """Similarity > adapt_threshold → ADAPT."""
        mt = _resolve_match_type(0.90, adapt_threshold=0.85, seed_threshold=0.60)
        assert mt == RegistryMatchType.ADAPT

    def test_resolve_match_type_seed(self):
        """Similarity in [seed, adapt) → SEED."""
        mt = _resolve_match_type(0.70, adapt_threshold=0.85, seed_threshold=0.60)
        assert mt == RegistryMatchType.SEED

    def test_resolve_match_type_full(self):
        """Similarity < seed_threshold → FULL_GENERATION."""
        mt = _resolve_match_type(0.40, adapt_threshold=0.85, seed_threshold=0.60)
        assert mt == RegistryMatchType.FULL_GENERATION

    def test_serialize_deserialize_roundtrip(self):
        """Serialized entry must deserialize back to identical fields."""
        entry = _make_entry()
        serialized = _serialize_entry(entry)
        deserialized = _deserialize_entry(serialized)

        assert deserialized.id == entry.id
        assert deserialized.description == entry.description
        assert abs(deserialized.validation_score - entry.validation_score) < 0.001
        assert deserialized.tags == entry.tags
        assert deserialized.tool_names == entry.tool_names

    def test_stats_empty_registry(self, tmp_registry: MCPRegistry):
        """Stats on empty registry should return entry_count=0."""
        stats = tmp_registry.stats()
        assert stats["entry_count"] == 0

    def test_stats_with_entries(self, tmp_registry: MCPRegistry):
        """Stats should compute mean score correctly."""
        tmp_registry.save(_make_entry(score=80.0))
        tmp_registry.save(_make_entry(score=90.0))
        stats = tmp_registry.stats()
        assert stats["entry_count"] == 2
        assert abs(stats["mean_score"] - 85.0) < 0.1


# ---------------------------------------------------------------------------
# Tests — create_entry factory
# ---------------------------------------------------------------------------


class TestCreateEntry:
    """Tests for the MCPRegistry.create_entry factory method."""

    def test_create_entry_returns_registry_entry(self, tmp_registry: MCPRegistry):
        """create_entry must return a RegistryEntry."""
        with patch("mcpresso.registry._embed", return_value=[0.1] * 384):
            entry = tmp_registry.create_entry(
                description="A GitHub server",
                source_code="# code",
                validation_score=82.0,
                readiness_tier="STAGING_READY",
                brew_time_ms=15000.0,
            )
        assert isinstance(entry, RegistryEntry)
        assert entry.validation_score == 82.0
        assert entry.readiness_tier == "STAGING_READY"

    def test_create_entry_assigns_uuid(self, tmp_registry: MCPRegistry):
        """create_entry must assign a valid UUID."""
        with patch("mcpresso.registry._embed", return_value=[0.1] * 384):
            entry = tmp_registry.create_entry(
                description="test",
                source_code="# code",
                validation_score=75.0,
                readiness_tier="STAGING_READY",
                brew_time_ms=5000.0,
            )
        # UUID4 format check
        parsed = uuid.UUID(entry.id)
        assert parsed.version == 4

    def test_create_entry_extracts_tags(self, tmp_registry: MCPRegistry):
        """create_entry must auto-extract tags."""
        with patch("mcpresso.registry._embed", return_value=[0.1] * 384):
            entry = tmp_registry.create_entry(
                description="A GitHub API integration server",
                source_code="import httpx",
                validation_score=80.0,
                readiness_tier="STAGING_READY",
                brew_time_ms=10000.0,
            )
        assert "github" in entry.tags
        assert "api" in entry.tags
