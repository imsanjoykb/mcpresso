# ☕ MCPresso

> **Brew your MCP server in under 60 seconds.**

MCPresso is a production-grade Python package that transforms natural language descriptions into fully validated, security-audited [MCP (Model Context Protocol)](https://modelcontextprotocol.io) servers. It combines LLM-powered code generation with a 5-category static analysis engine, iterative auto-repair, composite confidence scoring, semantic template reuse, and automatic test suite co-generation — all in a single `brew` command.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Architecture

```
╔══════════════════════════════════════════════════════════════════════╗
║                        MCPresso Pipeline                            ║
║                                                                      ║
║  Natural Language Description                                        ║
║         │                                                            ║
║         ▼                                                            ║
║  ┌─────────────┐    ┌───────────────────────────────────────────┐   ║
║  │  Registry   │───▶│           generator.py                    │   ║
║  │  Search     │    │  (ADAPT ~10s / SEED ~30s / FULL ~60s)     │   ║
║  │  (semantic) │    └───────────────┬───────────────────────────┘   ║
║  └─────────────┘                    │                                ║
║                                     ▼                                ║
║                          ┌─────────────────────┐                    ║
║                          │   validator.py       │                    ║
║                          │  5 Categories:       │                    ║
║                          │  1. Structural       │                    ║
║                          │  2. Protocol         │                    ║
║                          │  3. Security         │                    ║
║                          │  4. Robustness       │                    ║
║                          │  5. Documentation    │                    ║
║                          └──────────┬──────────┘                    ║
║                                     │                                ║
║                                     ▼                                ║
║                          ┌─────────────────────┐                    ║
║                          │    repair.py         │◀─── (≤3 iters)    ║
║                          │  Auto-fix criticals  │                    ║
║                          └──────────┬──────────┘                    ║
║                                     │                                ║
║                                     ▼                                ║
║                          ┌─────────────────────┐                    ║
║                          │    scorer.py         │                    ║
║                          │  Composite Score:    │                    ║
║                          │  • Validation  40%   │                    ║
║                          │  • Complexity  20%   │                    ║
║                          │  • Consistency 20%   │                    ║
║                          │  • Security    20%   │                    ║
║                          └──────────┬──────────┘                    ║
║                                     │                                ║
║                          ┌──────────▼──────────┐                    ║
║                          │    testgen.py        │  (optional)        ║
║                          │  3 tests/tool:       │                    ║
║                          │  happy/edge/security │                    ║
║                          └──────────┬──────────┘                    ║
║                                     │                                ║
║                          ┌──────────▼──────────┐                    ║
║                          │    registry.py       │                    ║
║                          │  Save if score ≥ 75  │                    ║
║                          └─────────────────────┘                    ║
║                                                                      ║
║         Output: server.py  +  test_server.py  +  BrewResult         ║
╚══════════════════════════════════════════════════════════════════════╝
```

---

## Quickstart

### Installation

```bash
pip install mcpresso
```

Or from source:

```bash
git clone https://github.com/your-org/mcpresso
cd mcpresso
pip install -e ".[dev]"
```

### Configure API Key

```bash
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

### Brew Your First MCP Server

```bash
# Generate a GitHub issue tracker server in under 60 seconds
mcpresso brew "A server that fetches GitHub issues and summarizes them" \
  --output github_server.py \
  --repair

# Generate with test suite
mcpresso brew "A PostgreSQL query server with connection pooling" \
  --output pg_server.py \
  --with-tests

# Validate an existing server
mcpresso validate pg_server.py

# Validate with JSON output for CI pipelines
mcpresso validate pg_server.py --report json > report.json

# Auto-repair a broken server
mcpresso repair broken_server.py --output fixed_server.py

# Generate tests for an existing server
mcpresso testgen my_server.py --output tests/test_my_server.py

# Browse the registry
mcpresso registry list
mcpresso registry search "slack notification"
mcpresso registry export backup.json
```

### Python API

```python
from mcpresso.pipeline import MCPressoPipeline

pipeline = MCPressoPipeline()

result = pipeline.brew(
    description="A server that queries PostgreSQL and returns query results",
    auto_repair=True,
    output_path="./pg_server.py",
    with_tests=True,
)

print(f"✅ Score: {result.final_score:.1f}/100")
print(f"📊 Tier: {result.readiness_tier.value}")
print(f"⏱  Time: {result.total_time_ms/1000:.1f}s")
print(f"🔧 Repairs: {result.repair_result.repair_iterations if result.repair_result else 0}")
print(f"🧪 Tests: {result.test_result.test_count if result.test_result else 0}")
```

---

## CLI Reference

### `mcpresso brew`

```
mcpresso brew "description" [OPTIONS]

Options:
  --output, -o TEXT      Output file path (e.g., server.py)
  --repair/--no-repair   Auto-repair critical issues (default: enabled)
  --with-tests           Generate pytest suite alongside the server
  --verbose, -v          Show detailed category breakdown
  --api-key TEXT         Anthropic API key (overrides env var)
```

**Brewing sequence output:**
```
  ⚙ Grinding beans... (generating from scratch ~60s)
  🔍 Checking the brew... (validating)
  🔧 Fixing the blend... (auto-repairing)
  📊 Measuring the roast... (scoring)
  🧪 Running quality checks... (generating tests)
  ✅ Your server is ready!

  ╭─ ☕ Brew Complete — a1b2c3d4 ────────────────────────────────╮
  │  Score:          ████████████████░░░░ 82.4/100              │
  │  Readiness:      ✅ STAGING_READY                           │
  │  Brew Time:      ✓ 43.2s  (under 60s ✓)                    │
  │  Tools Generated: 3                                          │
  │  Registry Match:  FULL_GENERATION                           │
  │  Repair:         1 iteration(s) | 61.3 → 82.4              │
  │  Tests:          9 tests | 3 security | ~71% coverage       │
  │  Output:         ./pg_server.py                             │
  ╰──────────────────────────────────────────────────────────────╯
```

### `mcpresso validate` / `mcpresso taste`

```
mcpresso validate server.py [--report rich|json]
mcpresso taste server.py        # alias for validate
```

### `mcpresso repair`

```
mcpresso repair broken.py [--output fixed.py]
```

### `mcpresso testgen`

```
mcpresso testgen server.py [--output tests/test_server.py]
```

### `mcpresso registry`

```
mcpresso registry list [--limit N]
mcpresso registry search "query"
mcpresso registry export output.json
mcpresso registry stats
```

---

## Module Reference

| Module | Purpose |
|--------|---------|
| `mcpresso/models.py` | All dataclasses and enums (single source of truth) |
| `mcpresso/generator.py` | NL → MCP server code via Claude API |
| `mcpresso/validator.py` | 5-category static analysis engine (no API calls) |
| `mcpresso/repair.py` | Iterative auto-repair via Claude API (max 3 iterations) |
| `mcpresso/scorer.py` | Composite 4-component confidence scoring |
| `mcpresso/registry.py` | Persistent semantic template registry |
| `mcpresso/testgen.py` | Automatic pytest suite co-generation |
| `mcpresso/pipeline.py` | End-to-end orchestration pipeline |
| `mcpresso/cli.py` | Typer + Rich terminal interface |
| `mcpresso/benchmark.py` | 20-case empirical benchmark harness |

---

## Validation Categories

| # | Category | Weight | What it checks |
|---|----------|--------|----------------|
| 1 | **Structural Integrity** | 20% | Valid Python syntax, MCP SDK imports, Server() instantiation, tool handlers registered, `if __name__ == "__main__"` present |
| 2 | **Protocol Compliance** | 25% | Tools have `name`/`description`/`inputSchema`, return types match MCP spec, async/await patterns, `McpError` with correct codes |
| 3 | **Security Posture** | 25% | No hardcoded secrets/API keys, no `eval()`/`exec()`, env vars for credentials, input validation, no path traversal |
| 4 | **Robustness** | 20% | `try/except` around external calls, no bare `except:`, timeout handling, resource cleanup |
| 5 | **Documentation** | 10% | Docstrings on handlers, human-readable tool descriptions, `logging` statements, type hints |

---

## Readiness Tiers

| Tier | Score | Critical Issues | Description |
|------|-------|-----------------|-------------|
| 🏆 `PRODUCTION_READY` | ≥ 90 | 0 | Deploy to production |
| ✅ `STAGING_READY` | 75–89 | 0 | Safe for staging/testing |
| ⚠️ `DEVELOPMENT_ONLY` | 50–74 | any | Development use only |
| ❌ `NEEDS_REPAIR` | < 50 | any | Requires repair before use |

---

## Registry Reuse Tiers

The semantic registry enables "agentic memory" — MCPresso learns from its own outputs:

| Similarity | Mode | Est. Time | How it works |
|-----------|------|-----------|--------------|
| ≥ 0.85 | **ADAPT** | ~10s | Modifies only differing parts of existing server |
| 0.60–0.85 | **SEED** | ~30s | Uses similar server as few-shot example |
| < 0.60 | **FULL_GENERATION** | ~60s | Generates from scratch |

---

## Benchmark Results (Placeholder)

Run the benchmark suite to populate this table:

```bash
python -c "
from mcpresso.benchmark import MCPressoBenchmark, BENCHMARK_CASES
bench = MCPressoBenchmark(output_dir='./benchmark_results')
report = bench.run(cases=BENCHMARK_CASES)
bench.save_report(report)
bench.print_summary(report)
"
```

| Metric | Value |
|--------|-------|
| Success Rate (STAGING_READY+) | — |
| P50 Generation Latency | — |
| P95 Generation Latency | — |
| Mean Validation Score | — |
| Repair Convergence Rate | — |
| Token Efficiency (tokens/success) | — |
| Security Detection Rate | — |
| Registry Reuse Rate | — |
| Mean Test Coverage (est.) | — |

*Run `mcpresso benchmark` with your API key to populate.*

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `MCPRESSO_MODEL` | `claude-sonnet-4-20250514` | Model to use |
| `MCPRESSO_REGISTRY_DIR` | `~/.mcpresso/registry` | Registry directory |
| `MCPRESSO_SIMILARITY_THRESHOLD_ADAPT` | `0.85` | ADAPT mode threshold |
| `MCPRESSO_SIMILARITY_THRESHOLD_SEED` | `0.60` | SEED mode threshold |
| `MCPRESSO_LOG_LEVEL` | `WARNING` | Logging level |

---

## Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests (no API calls — all LLM interactions are mocked)
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=mcpresso --cov-report=term-missing

# Run specific test module
pytest tests/test_validator.py -v
pytest tests/test_scorer.py -v
pytest tests/test_registry.py -v
```

---

## Design Principles (for Research)

MCPresso is designed as a research artifact for empirical study of LLM-assisted code generation quality. Key design patterns documented in the codebase:

1. **Iterative Refinement Loop** (`pipeline.py`): brew → validate → repair constitutes an agentic self-correction cycle, analogous to self-refine (Madaan et al., 2023).

2. **Composite Ensemble Scoring** (`scorer.py`): Four orthogonal quality signals (validation, complexity, consistency, security) are combined with learned weights, inspired by ensemble methods.

3. **Semantic Memory & Reuse** (`registry.py`): Dense retrieval using sentence-transformers enables few-shot grounding from prior successful generations — a novel contribution for MCP server generation.

4. **Co-Generation** (`testgen.py`): Simultaneous server + test suite synthesis using shared tool definitions, addressing the "no tests for generated code" deployment risk.

5. **Separation of Concerns**: Each module has a single well-defined responsibility with clean dataclass interfaces, enabling independent benchmarking of each stage.

---

## License

MIT License — see [LICENSE](LICENSE).

---

## Citation

If you use MCPresso in academic work, please cite:

```bibtex
@software{mcpresso2025,
  title   = {MCPresso: Brewing Production-Ready MCP Servers in Under 60 Seconds},
  year    = {2025},
  note    = {Python package. \url{https://github.com/your-org/mcpresso}},
  version = {0.1.0}
}
```
