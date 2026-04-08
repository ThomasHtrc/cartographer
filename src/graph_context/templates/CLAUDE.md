# graph-context

This project has a **graph-context** index — a graph of codebase structure, git history, and plans in `.graph-context/db/`, exposed via MCP tools.

## When to use graph-context vs standard tools

| Task | Best tool | Why |
|------|-----------|-----|
| **Impact analysis** — "what breaks if I change X?" | `blast_radius` | Traces transitive callers across files. Grep only finds direct string matches. |
| **Hidden coupling** — "what else should I check?" | `co_changes` | Reveals files that historically change together. No manual cross-referencing needed. |
| **Orientation** — "what's in this area of the codebase?" | `repo_map` / `context` | Surfaces related files you didn't know to look for, ranked by relevance. |
| **Call graph** — "who calls this?" / "what does this call?" | `find_callers` / `find_callees` | Returns structured results in one call vs. multi-round grep. |
| **Find a definition** | `find_definition` | Returns full typed signature without needing to Read the file. |
| **Continuity** — "what was planned?" | `plan_list` / `plan_show` | Cross-session memory of what's in progress and why. |
| **Reading implementation details** | `Read` / `Grep` | Graph-context gives signatures, not full code. Read the source for logic. |
| **Tracing sequential flow** | `Grep` + `Read` | For "how does X work end-to-end?", reading the actual code is more detailed. |
| **Cross-cutting concerns** | `git log --grep` / `Grep` | Concepts spread across many files (e.g. "MNPI") aren't captured by file-oriented graph queries. |

## Decision tree

1. **Before modifying a function**: `blast_radius(symbol="fn_name")` — always check impact first.
2. **Exploring an unfamiliar area**: `repo_map(focus=["src/module"])` — get the lay of the land.
3. **Starting a task**: `context(focus=["file1.py", "symbol_name"])` — understand the neighborhood.
4. **Checking hidden dependencies**: `co_changes(file="src/foo.py")` — what else usually changes with this file?
5. **Resuming work**: `plan_list(status="active")` — check what was planned in previous sessions.
6. **Need actual code**: Use `Read` — graph-context gives structure, not implementation.

## Tool reference

### High-value tools (use these proactively)
- **`blast_radius(symbol, depth=5)`** — Transitive dependents of a function. The killer feature — saves 3-5 rounds of grep.
- **`co_changes(file)`** — Files that frequently change together. Reveals coupling grep can't see.
- **`repo_map(focus?, budget=8000)`** — Ranked codebase overview. Use with focus for a targeted view, or without for a global map.
- **`context(focus, budget=4000, format="markdown")`** — Ranked context around focal files/symbols. Formats: markdown, json, annotated.

### Navigation
- **`find_definition(symbol)`** — Where a function/class/variable is defined, with full signature.
- **`find_callers(symbol)`** — All functions that call a given function.
- **`find_callees(symbol)`** — All functions called by a given function.
- **`module_structure(path, recursive=True)`** — Files, functions, and classes in a directory.

### History
- **`recent_changes(path, limit=20)`** — Recent commits touching a file or module. Cleaner than git log (no merge noise).
- **`co_changes(file)`** — Hidden coupling via co-change frequency.

### Plans (cross-session continuity)
- **`plan_list(status?)`** — List plans. Filter by: draft, active, completed, abandoned.
- **`plan_show(plan_id)`** — Full plan details with targets, intents, and rationale.
- **`plan_create(title, description, targets?)`** — Record intended changes.
- **`plan_add_intent(plan_id, description, rationale)`** — Add a specific change step.
- **`plan_update(plan_id, status?)`** — Update plan status.

### Low-level
- **`graph_stats()`** — Node/edge counts. Verify the graph is indexed.
- **`run_cypher(query)`** — Raw Cypher for ad-hoc queries.

## Tips

- **Partial paths work** — `src/api/routes.py` resolves even if the full path is `apps/myapp/src/api/routes.py`.
- **Combine tools** — Use `blast_radius` to find affected code, then `Read` the specific files that matter.
- **Graph may be stale** after significant code changes. Run `graph-context index --incremental` to refresh.
- **Plans persist across sessions** — always check `plan_list(status="active")` when resuming work.
