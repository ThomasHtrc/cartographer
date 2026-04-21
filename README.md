# Cartographer

**Your coding agent's map of the codebase.**

Cartographer builds a graph of your codebase — structure (files, functions, classes, call edges, imports) and history (commits, co-changes, authorship) — and exposes it to Claude Code via MCP tools. Instead of grepping blind, your agent can ask "what breaks if I change this function?", "what files always change together?", or "what's been happening in this area and what's planned next?"

## What it gives your agent

| Without Cartographer | With Cartographer |
|---|---|
| Grep for callers, hope you found them all | `blast_radius("fn_name")` — transitive dependents in one call |
| Read 10 files to understand an area | `repo_map(focus=["src/auth"])` — ranked overview of what matters |
| No memory between sessions | `plan_list(status="active")` — pick up where you left off |
| Manual `git log` for context | `timeline("src/api/routes.py")` — past commits + active plans + co-change neighbors |
| Can't see hidden coupling | `co_changes("src/billing.py")` — files that always change together |

## Quick start

### 1. Install

```bash
pip install git+https://github.com/ThomasHtrc/graph-context.git'[mcp,watch]'
```

Requires Python 3.11+.

### 2. Set up a repo

```bash
cd your-project
cartographer setup
```

This does four things:
- Initializes a `.cartographer/` directory for the graph database
- Indexes your codebase structure (files, functions, classes, call graph)
- Indexes git history (commits, co-changes, authorship)
- Writes `.mcp.json` and appends tool docs to `CLAUDE.md`

### 3. Register with Claude Code

```bash
claude mcp add cartographer -- cartographer-mcp
```

That's it. Launch Claude Code in your project and the tools are available.

## How it works

Cartographer uses [tree-sitter](https://tree-sitter.github.io/) to parse your code and [LadybugDB](https://github.com/nicholasgasior/ladybug) (a Kùzu fork) to store the resulting graph. The graph has three layers:

- **Structure** — files, modules, classes, functions, variables, and edges between them (calls, imports, contains, belongs-to)
- **History** — commits, file changes, co-change frequency, authorship
- **Plans** — cross-session intent tracking with progress, dependencies, and next-step recommendations

The MCP server runs in-process with a background watcher thread that keeps the index fresh as you edit files. No separate daemon needed.

### Supported languages

- Python
- TypeScript / JavaScript / TSX

## MCP tools

When registered with Claude Code, Cartographer exposes these tools:

### High-value (use proactively)

- **`blast_radius(symbol, depth=5)`** — Transitive dependents of a function. The killer feature.
- **`co_changes(file)`** — Files that historically change together. Reveals coupling grep can't see.
- **`repo_map(focus?, budget=8000)`** — Ranked codebase overview. With focus for a targeted view, or without for a global map.
- **`context(focus, budget=4000)`** — Ranked context around focal files/symbols, with active plan annotations.
- **`timeline(target, limit=20)`** — Past commits + active plans + co-change neighbors in one structured view.

### Navigation

- **`find_definition(symbol)`** — Where a function/class is defined, with full signature.
- **`find_callers(symbol)`** / **`find_callees(symbol)`** — Call graph traversal.
- **`module_structure(path)`** — Files, functions, and classes in a directory.
- **`dead_code(path?)`** — Functions with zero callers.

### History

- **`recent_changes(path, limit=20)`** — Commits touching a file or module.
- **`search_commits(query, author?)`** — Search commit messages across the repo.

### Plans (cross-session memory)

- **`plan_create`** / **`plan_list`** / **`plan_show`** / **`plan_update`** — Track intended changes across sessions.
- **`plan_add_intent`** / **`plan_update_intent`** — Break plans into steps with progress tracking.

### Low-level

- **`graph_stats()`** — Node/edge counts.
- **`run_cypher(query)`** — Raw Cypher for ad-hoc graph queries.
- **`reindex(scope, layer)`** — Re-run indexing without restarting the MCP server.

## CLI

Cartographer also works from the command line:

```bash
cartographer query callers validate     # who calls this function?
cartographer query blast-radius login   # what depends on this?
cartographer query co-changes src/auth.py
cartographer timeline src/auth.py       # past + future view
cartographer stats                      # verify the graph is indexed
cartographer cypher "MATCH (f:File) RETURN f.path"  # raw Cypher
```

### Keeping the index fresh

The MCP server automatically watches for file changes and re-indexes in the background. If you're using the CLI without the MCP server, you can either:

```bash
cartographer watch --daemon   # background watcher
cartographer index --incremental  # one-shot refresh
```

## When to use Cartographer vs standard tools

Cartographer shines at **structural and historical queries** — impact analysis, hidden coupling, orientation in unfamiliar code, cross-session continuity. It gives signatures, not source code.

For **reading implementation details** or **tracing sequential flow**, use `Read` and `Grep` — they're better at "show me the actual code."

The best workflow combines both: use Cartographer to figure out *what* to look at, then read the files that matter.

## Requirements

- Python 3.11+
- A git repository (history indexing uses git)
- Claude Code (for the MCP integration) — though the CLI works standalone
