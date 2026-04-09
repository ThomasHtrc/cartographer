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
| **Cross-cutting concerns** — "find all MNPI-related changes" | `search_commits` | Searches commit messages case-insensitively. Finds concepts spread across many files. |
| **Story of an area** — "what's been happening in this file and what's planned?" | `timeline` | Past commits + active plans + neighbors in one call. Saves a `recent_changes` + `plan_show` + `co_changes` round trip. |
| **Dead code cleanup** | `dead_code` | Finds functions with zero callers. Static call graph analysis grep can't do. |
| **Continuity** — "what was planned?" | `plan_list` / `plan_show` | Cross-session memory with progress tracking, dependency chains, and next-step recommendations. |
| **Reading implementation details** | `Read` / `Grep` | Graph-context gives signatures, not full code. Read the source for logic. |
| **Tracing sequential flow** | `Grep` + `Read` | For "how does X work end-to-end?", reading the actual code is more detailed. |

## Decision tree

1. **Before modifying a function**: `blast_radius(symbol="fn_name")` — always check impact first.
2. **Exploring an unfamiliar area**: `repo_map(focus=["src/module"])` — get the lay of the land.
3. **Starting a task**: `context(focus=["file1.py", "symbol_name"])` — understand the neighborhood. If the files are targeted by an active plan, you'll see that plan's summary and pending intents in the output.
4. **Checking hidden dependencies**: `co_changes(file="src/foo.py")` — what else usually changes with this file?
5. **Tracing a concept across history**: `search_commits(query="auth refactor")` — find all related commits and files touched.
6. **Resuming work**: `plan_list(status="active")` — check what was planned, see progress (e.g. "3/5 intents done").
7. **Cleaning up code**: `dead_code(path="src/module")` — find unused functions.
8. **Resuming an unfamiliar area**: `timeline(target)` — read recent commits + pending plans before touching code, in one shot.
9. **Need actual code**: Use `Read` — graph-context gives structure, not implementation.

## Tool reference

### High-value tools (use these proactively)
- **`blast_radius(symbol, depth=5)`** — Transitive dependents of a function. The killer feature — saves 3-5 rounds of grep.
- **`co_changes(file)`** — Files that frequently change together. Reveals coupling grep can't see.
- **`repo_map(focus?, budget=8000)`** — Ranked codebase overview. Use with focus for a targeted view, or without for a global map.
- **`context(focus, budget=4000, format="markdown")`** — Ranked context around focal files/symbols. Includes active plan annotations when focal files are plan targets. Formats: markdown, json, annotated.
- **`timeline(target, limit=20, format="markdown", include_neighbors=True)`** — Past commits + active plans + co-change neighbors + (for symbols) callers, in one structured view. Target can be a file, module, or symbol name. The fastest way to understand "what's been happening here and what's planned next." Pair with `--format html` from the CLI for a self-contained visualization for humans.

### Navigation
- **`find_definition(symbol)`** — Where a function/class/variable is defined, with full signature.
- **`find_callers(symbol)`** — All functions that call a given function.
- **`find_callees(symbol)`** — All functions called by a given function.
- **`module_structure(path, recursive=True)`** — Files, functions, and classes in a directory.
- **`dead_code(path?, include_methods=False)`** — Functions never called (likely dead code). Filters out entry points and test functions.

### History
- **`recent_changes(path, limit=20)`** — Recent commits touching a file or module. Cleaner than git log (no merge noise).
- **`co_changes(file)`** — Hidden coupling via co-change frequency.
- **`search_commits(query, author?, limit=20)`** — Search commit messages case-insensitively. Use for cross-cutting concerns.

### Plans (cross-session continuity)
- **`plan_list(status?)`** — List plans with progress (e.g. "3/5"). Filter by: draft, active, completed, abandoned.
- **`plan_show(plan_id)`** — Full details: targets, intents, progress %, dependency chain, blocked status, and next recommended intent.
- **`plan_create(title, description, targets?)`** — Record intended changes. Targets auto-resolve to files, modules, or symbols.
- **`plan_add_intent(plan_id, description, rationale, affected_files?)`** — Add a change step. Affected files are auto-linked as plan targets.
- **`plan_update(plan_id, status?)`** — Update plan status.
- **`plan_update_intent(intent_id, status?, description?)`** — Update an intent's status (e.g. mark as completed) or description.

### Low-level
- **`graph_stats()`** — Node/edge counts. Verify the graph is indexed.
- **`run_cypher(query)`** — Raw Cypher for ad-hoc queries.

## Tips

- **Partial paths work** — `src/api/routes.py` resolves even if the full path is `apps/myapp/src/api/routes.py`.
- **Combine tools** — Use `blast_radius` to find affected code, then `Read` the specific files that matter.
- **Graph stays fresh automatically** if the file watcher is running (`graph-context watch --daemon`). Otherwise, run `graph-context index --incremental` after significant changes.
- **Plans persist across sessions** — always check `plan_list(status="active")` when resuming work. Update intent status with `plan_update_intent` as you complete steps.
- **Context includes plan awareness** — when you query `context()` for files targeted by an active plan, the plan summary and pending intents appear in the output automatically.
