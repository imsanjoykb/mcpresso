# MCPresso — Local Testing Guide

> "Brew your MCP server in under 60 seconds"

This guide is for anyone who clones this repository and wants to run MCPresso locally end-to-end — from install through generating, validating, and testing real MCP servers.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Clone & Install](#clone--install)
3. [Environment Setup](#environment-setup)
4. [Quick Smoke Test (no API key needed)](#quick-smoke-test)
5. [CLI Commands Reference](#cli-commands-reference)
   - [brew](#brew)
   - [validate / taste](#validate--taste)
   - [repair](#repair)
   - [testgen](#testgen)
   - [registry](#registry)
6. [Sample Brew Queries](#sample-brew-queries)
7. [Run a Generated Server + Client](#run-a-generated-server--client)
8. [Run the Unit Tests](#run-the-unit-tests)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Version | Check |
|-------------|---------|-------|
| Python | 3.10+ | `python --version` |
| pip | latest | `pip --version` |
| Git | any | `git --version` |
| Anthropic API key | — | [console.anthropic.com](https://console.anthropic.com/) |

---

## Clone & Install

```bash
# 1. Clone the repository
git clone https://github.com/your-org/mcpresso.git
cd mcpresso

# 2. (Optional but recommended) Create a virtual environment
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows CMD
.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate

# 3. Install MCPresso in editable mode (registers the `mcpresso` CLI command)
pip install -e .
```

> **Windows PATH note:** If `mcpresso` is not found after install, add the pip Scripts directory to your PATH:
> ```powershell
> # Run once — adds permanently to user PATH
> $sp = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
> [Environment]::SetEnvironmentVariable("PATH", "$([Environment]::GetEnvironmentVariable('PATH','User'));$sp", "User")
> # Apply to current session
> $env:PATH += ";$sp"
> ```

---

## Environment Setup

Copy the example env file and add your Anthropic API key:

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Edit `.env`:

```dotenv
# Required for brew / repair / testgen commands
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional
MCPRESSO_LOG_LEVEL=WARNING    # DEBUG for verbose output
```

Verify the key is loaded:

```bash
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print('Key set:', bool(os.getenv('ANTHROPIC_API_KEY')))"
```

Expected output: `Key set: True`

---

## Quick Smoke Test

Run this **without** an API key to verify the install is correct:

```bash
# 1. Check CLI is available
mcpresso --help

# 2. Check version
mcpresso version

# 3. Validate the included sample calculator server
mcpresso validate calc_server.py

# 4. Validate and output JSON report
mcpresso validate calc_server.py --report json

# 5. Run the included calculator client (no API key needed)
python client_calc_server.py
```

Expected output for step 5:
```
============================================================
  Server: calculator  (calc_server.py)
  Tools available: 4
============================================================
  * add                  Add two numbers together ...
  * subtract             Subtract the second number ...
  * multiply             Multiply two numbers together ...
  * divide               Divide the first number by ...

CALL -> add(a=9.5, b=9.5)
  [OK]  Result: 9.5 + 9.5 = 19.0

CALL -> subtract(a=9.5, b=9.5)
  [OK]  Result: 9.5 - 9.5 = 0.0

CALL -> multiply(a=9.5, b=9.5)
  [OK]  Result: 9.5 * 9.5 = 90.25

CALL -> divide(a=9.5, b=9.5)
  [OK]  Result: 9.5 / 9.5 = 1.0
============================================================
  All tool calls completed.
============================================================
```

---

## CLI Commands Reference

### brew

Generate a production-ready MCP server from a plain English description.

```bash
# Basic brew (auto-repair enabled by default)
mcpresso brew "A server that adds, subtracts, multiplies and divides numbers"

# Save to a specific file
mcpresso brew "A server that fetches GitHub issues" --output github_server.py

# Generate server + companion client for immediate testing
mcpresso brew "A todo list server with create, list, complete, and delete tools" \
    --output todo_server.py --with-client

# Generate server + test suite
mcpresso brew "A weather forecast server" --output weather_server.py --with-tests

# Generate everything: server + client + tests
mcpresso brew "A URL shortener server" --output url_server.py --with-client --with-tests

# Skip auto-repair (faster, less polished)
mcpresso brew "A simple echo server" --no-repair

# Verbose output with per-category validation scores
mcpresso brew "A Slack notification server" --verbose

# Pass API key directly (overrides .env)
mcpresso brew "A database query server" --api-key sk-ant-xxxx
```

**What the scorecard shows:**
```
╭──────────────── ☕ Brew Complete ────────────────╮
│ Score:         ████████████████░░░░ 84.2/100      │
│ Readiness:     ✅ STAGING_READY                   │
│ Brew Time:     ✓ 45.2s  (under 60s ✓)             │
│ Tools Generated: 3                                │
│ Output:        todo_server.py                     │
│ Client:        client_todo_server.py  <- run this │
╰──────────────────────────────────────────────────╯
```

---

### validate / taste

Validate an existing MCP server file against all 5 quality categories.

```bash
# Rich terminal report (default)
mcpresso validate calc_server.py

# Same command using the coffee-themed alias
mcpresso taste calc_server.py

# Machine-readable JSON output (pipe to file or jq)
mcpresso validate calc_server.py --report json

# Save JSON report
mcpresso validate calc_server.py --report json > report.json

# Validate any generated server
mcpresso validate todo_server.py
mcpresso validate github_server.py
```

**Exit codes:**
- `0` — execution_ready (score ≥ 75)
- `2` — not execution_ready

**Sample output:**
```
╭──────────── Validation Report ───────────╮
│ File:       calc_server.py               │
│ Status:     ✅ EXECUTION READY           │
│ Score:      ██████████████████░░ 94.7    │
│ Confidence: HIGH                         │
│ Critical:   0 issues                     │
│ Warnings:   1 issues                     │
╰──────────────────────────────────────────╯

Category                          Score  Passed  Failed
Structural Integrity              100.0    6       0
Protocol Compliance                95.0    5       1
Security Posture                   90.0    6       1
Robustness & Reliability           95.0    5       0
Documentation & Observability      93.0    5       1
```

---

### repair

Auto-repair critical issues in an existing server file (up to 3 LLM iterations).

```bash
# Repair in-place (overwrites the file)
mcpresso repair broken_server.py

# Repair to a new file
mcpresso repair broken_server.py --output fixed_server.py

# Verbose: show each fix applied
mcpresso repair broken_server.py --verbose
```

---

### testgen

Generate a pytest test suite for an existing MCP server.

```bash
# Auto-detect output path (creates test_<filename>.py)
mcpresso testgen calc_server.py
# Output: test_calc_server.py

# Specify output path
mcpresso testgen todo_server.py --output tests/test_todo.py

# Generate and run the tests
mcpresso testgen calc_server.py
pytest test_calc_server.py -v
```

---

### registry

Manage the local server template registry (`~/.mcpresso/registry/`).

```bash
# List all previously brewed servers
mcpresso registry list

# Show more entries
mcpresso registry list --limit 50

# Semantic similarity search
mcpresso registry search "github issues"
mcpresso registry search "database query"
mcpresso registry search "todo list management"

# Show registry statistics
mcpresso registry stats

# Export entire registry to JSON (for sharing or paper benchmarks)
mcpresso registry export registry_export.json
```

---

## Sample Brew Queries

Copy any of these directly into `mcpresso brew "..."`:

### Simple / Quick (~20–40s)
```bash
mcpresso brew "A calculator server with add, subtract, multiply, and divide" \
    --output calc_server.py --with-client

mcpresso brew "A server that converts temperatures between Celsius and Fahrenheit" \
    --output temp_server.py --with-client

mcpresso brew "A server that returns the current UTC time and converts Unix timestamps" \
    --output time_server.py --with-client

mcpresso brew "A text utilities server with word count, character count, and reverse string" \
    --output text_server.py --with-client
```

### Medium Complexity (~40–60s)
```bash
mcpresso brew "A todo list server with create, list, complete, delete, and update tools" \
    --output todo_server.py --with-client

mcpresso brew "A file system server that reads, writes, lists, and deletes files safely" \
    --output fs_server.py --with-client

mcpresso brew "A JSON validator and formatter server with schema validation support" \
    --output json_server.py --with-client

mcpresso brew "A URL utilities server that validates URLs, extracts domains, and checks redirects" \
    --output url_server.py --with-client
```

### Advanced / Multi-Tool (~50–90s)
```bash
mcpresso brew "A GitHub integration server that lists repos, fetches issues, and creates comments using the GitHub REST API" \
    --output github_server.py --with-client --with-tests

mcpresso brew "A PostgreSQL database server that executes queries, lists tables, and describes schemas with connection pooling" \
    --output postgres_server.py --with-client

mcpresso brew "A Slack notification server that sends messages, creates channels, and lists workspace members" \
    --output slack_server.py --with-client --with-tests

mcpresso brew "A weather server that gets current conditions and 5-day forecasts using the OpenWeatherMap API" \
    --output weather_server.py --with-client

mcpresso brew "An email server with send, read inbox, search, and draft capabilities using SMTP and IMAP" \
    --output email_server.py --with-client
```

---

## Run a Generated Server + Client

After any `mcpresso brew --with-client`, two files are created:

```
my_server.py            ← the MCP server
client_my_server.py     ← auto-generated test client
```

**Run the client to test all tools immediately:**
```bash
python client_my_server.py
```

**The client will:**
1. Connect to the server via stdio
2. Discover all registered tools via `list_tools()`
3. Call each tool with inferred example arguments
4. Print `[OK]` for success or `[ERR]` for exceptions

**Run the server standalone** (to inspect it or connect a different client):
```bash
# The server listens on stdio — it's typically launched by the client
# For manual inspection, you can run it directly:
python my_server.py
# Then send MCP protocol messages via stdin
```

**Validate the generated server:**
```bash
mcpresso validate my_server.py
```

---

## Run the Unit Tests

```bash
# Install test dependencies (already included in pyproject.toml)
pip install -e ".[dev]"

# Run all unit tests
pytest tests/ -v

# Run specific test modules
pytest tests/test_validator.py -v
pytest tests/test_scorer.py -v
pytest tests/test_registry.py -v

# Run with coverage
pytest tests/ --cov=mcpresso --cov-report=term-missing

# Run a generated test suite (after mcpresso testgen)
pytest test_calc_server.py -v
```

---

## Troubleshooting

### `mcpresso` command not found

```powershell
# Find the Scripts directory
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"

# Add it to PATH for this session
$env:PATH += ";C:\Users\<YourUser>\AppData\Roaming\Python\Python313\Scripts"

# Add permanently
$sp = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
[Environment]::SetEnvironmentVariable("PATH", "$([Environment]::GetEnvironmentVariable('PATH','User'));$sp", "User")
```

### `ANTHROPIC_API_KEY not set` error

```bash
# Check .env exists and has the key
cat .env           # macOS/Linux
type .env          # Windows CMD

# Or pass directly
mcpresso brew "..." --api-key sk-ant-xxxxxxx
```

### `sentence-transformers` download is slow (first run)

The first `brew` command downloads the `all-MiniLM-L6-v2` model (~80MB) for semantic registry search. Subsequent runs use the cached model.

```bash
# Pre-download the model before your first brew
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

### Brew takes > 60 seconds

This is expected on the first run (no registry entries yet, full generation). Subsequent brews on similar topics use the registry for speed:
- similarity > 0.85 → adapt (~10s)
- similarity 0.60–0.85 → seed (~30s)
- similarity < 0.60 → full generation (~60s)

### Generated server has low score

```bash
# Auto-repair it
mcpresso repair my_server.py --verbose

# Or re-brew with repair explicitly enabled
mcpresso brew "..." --repair --output my_server.py
```

### Registry is slow on first search

The embedding model is loaded lazily on first registry access. To warm it up:

```bash
python -c "from mcpresso.registry import MCPRegistry; MCPRegistry()"
```

---

## File Structure After a Full Brew

```
mcpresso/
├── my_server.py              ← generated MCP server
├── client_my_server.py       ← auto-generated test client
├── test_my_server.py         ← pytest suite (--with-tests)
└── ~/.mcpresso/
    └── registry/
        └── *.json            ← saved server entries for reuse
```

---

*Generated with ❤️ by MCPresso — "Brew your MCP server in under 60 seconds"*


