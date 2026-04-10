"""MCP server exposing cartographer tools to coding agents.

Wraps the existing cartographer logic (indexing, querying, context generation,
plan management) as MCP tools accessible via stdio transport.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config
from .storage.store import GraphStore
from .context.ranker import Ranker
from .context.assembler import Assembler
from .context import formatter
from .plans.manager import PlanManager

mcp = FastMCP(name="cartographer")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_path() -> str:
    """Get the repo path from environment or cwd."""
    return os.environ.get("CARTOGRAPHER_REPO", os.getcwd())


# Persistent store cache — avoids DB open/close overhead on every tool call.
_store_cache: dict[str, GraphStore] = {}

# Per-repo background watcher threads. Each entry is (Thread, stop_event).
# Spawned lazily from _open_store() so that the MCP server keeps the index
# fresh while it holds the LadybugDB lock — without this, every other tool
# (CLI index, daemon watcher) would be blocked by the lock.
_watcher_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
_watcher_lock = threading.Lock()


def _open_store() -> GraphStore:
    """Get or create a persistent store connection for the current repo.

    Also lazily spawns an in-process file watcher thread for the repo
    (unless ``CARTOGRAPHER_MCP_AUTOWATCH=0``), so that file edits get
    re-indexed without restarting the MCP server.
    """
    repo = _repo_path()
    if repo in _store_cache:
        return _store_cache[repo]
    db_path = config.get_db_path(repo)
    store = GraphStore(db_path)
    store.open()
    store.ensure_schema()
    _store_cache[repo] = store
    _ensure_watcher(repo, store)
    return store


def _ensure_watcher(repo: str, store: GraphStore) -> None:
    """Spawn a background watcher thread for ``repo`` if not already running.

    No-op if ``CARTOGRAPHER_MCP_AUTOWATCH=0`` is set in the environment.
    """
    if os.environ.get("CARTOGRAPHER_MCP_AUTOWATCH", "1") == "0":
        return
    with _watcher_lock:
        if repo in _watcher_threads:
            existing_thread, _ = _watcher_threads[repo]
            if existing_thread.is_alive():
                return
            # Stale entry — drop and respawn.
            del _watcher_threads[repo]

        from .watcher import run_with_store

        stop_event = threading.Event()
        thread = threading.Thread(
            target=run_with_store,
            kwargs={
                "store": store,
                "repo_path": repo,
                "stop_event": stop_event,
                "quiet": True,
            },
            name=f"cartographer-watcher[{Path(repo).name}]",
            daemon=True,
        )
        thread.start()
        _watcher_threads[repo] = (thread, stop_event)


def _shutdown_watchers() -> None:
    """Signal all watcher threads to stop and wait briefly for them to exit."""
    with _watcher_lock:
        items = list(_watcher_threads.items())
        _watcher_threads.clear()
    for _repo, (thread, stop_event) in items:
        stop_event.set()
    for _repo, (thread, _stop_event) in items:
        thread.join(timeout=2.0)


atexit.register(_shutdown_watchers)


def _resolve_path(store: GraphStore, path: str) -> str:
    """Resolve a possibly-partial path to an indexed file or module path.

    Tries exact match first, then suffix match against indexed paths.
    Returns the resolved path, or the original if no match found.
    """
    # Exact match — fast path
    rows = store.query(
        "MATCH (f:File {path: $p}) RETURN f.path", {"p": path},
    )
    if rows:
        return path
    rows = store.query(
        "MATCH (m:Module {path: $p}) RETURN m.path", {"p": path},
    )
    if rows:
        return path

    # Suffix match: find files/modules ending with the given path
    suffix = "/" + path.strip("/")
    file_rows = store.query(
        "MATCH (f:File) WHERE f.path ENDS WITH $s RETURN DISTINCT f.path",
        {"s": suffix},
    )
    mod_rows = store.query(
        "MATCH (m:Module) WHERE m.path ENDS WITH $s RETURN DISTINCT m.path",
        {"s": suffix},
    )

    # If we get exactly one match, use it. For files, derive the prefix.
    candidates = [r[0] for r in file_rows] + [r[0] for r in mod_rows]
    if not candidates:
        return path

    # Find common prefix among matches
    prefixes = set()
    for c in candidates:
        idx = c.find(path.strip("/"))
        if idx >= 0:
            prefixes.add(c[:idx])

    if len(prefixes) == 1:
        # Unambiguous — all matches share the same prefix
        return prefixes.pop() + path.strip("/")

    return path


def _resolve_paths(store: GraphStore, paths: list[str]) -> list[str]:
    """Resolve a list of possibly-partial paths."""
    return [_resolve_path(store, p) for p in paths]


def _no_results_hint(store: GraphStore, path: str, entity: str = "files") -> str:
    """Generate a helpful hint when no results found for a path."""
    suffix = "/" + path.strip("/")
    rows = store.query(
        "MATCH (f:File) WHERE f.path ENDS WITH $s RETURN f.path LIMIT 5",
        {"s": suffix},
    )
    if rows:
        suggestions = [r[0] for r in rows]
        return f"No {entity} found for '{path}'. Did you mean: {', '.join(suggestions)}?"
    return f"No {entity} found for '{path}'"


# ---------------------------------------------------------------------------
# Context generation tools
# ---------------------------------------------------------------------------

@mcp.tool()
def context(focus: list[str], budget: int = 4000, format: str = "markdown") -> str:
    """Generate ranked context for LLM prompts based on focal files/symbols.

    Use this to understand code around specific files or symbols before making changes.
    Returns a relevance-ranked map of signatures and declarations.

    Args:
        focus: File paths or symbol names to focus on (e.g. ["src/auth.py", "authenticate"])
        budget: Token budget for the output (default 4000)
        format: Output format — "markdown", "json", or "annotated"
    """
    store = _open_store()
    focus = _resolve_paths(store, focus)
    ranker = Ranker(store)
    ranked = ranker.rank(focus, max_results=100)
    if not ranked:
        hints = [_no_results_hint(store, f, "nodes") for f in focus]
        return "(no relevant nodes found) " + " | ".join(hints)

    asm = Assembler(_repo_path(), signature_only=True, store=store)
    assembled = asm.assemble(ranked, budget, focal_points=focus)

    if format == "json":
        return formatter.format_json(assembled)
    elif format == "annotated":
        return formatter.format_annotated(assembled)
    return formatter.format_markdown(assembled)


@mcp.tool()
def repo_map(focus: list[str] | None = None, budget: int = 8000) -> str:
    """Generate a repo map showing the most important files and symbols.

    Like aider's RepoMap — a condensed view of the codebase structure ranked by
    relevance. Use without focus for a global overview, or with focus for a
    targeted view around specific paths.

    Args:
        focus: Optional file paths or modules to focus on (omit for global map)
        budget: Token budget (default 8000)
    """
    store = _open_store()
    ranker = Ranker(store)

    if focus:
        focus = _resolve_paths(store, focus)
        ranked = ranker.rank(focus, max_results=200)
    else:
        files = store.query("MATCH (f:File) RETURN f.path")
        all_paths = [r[0] for r in files]
        if not all_paths:
            return "(no indexed files — run `cartographer index` first)"
        ranked = ranker.rank(all_paths, max_results=200)

    if not ranked:
        return "(no nodes found)"

    asm = Assembler(_repo_path(), signature_only=True, store=store)
    assembled = asm.assemble(ranked, budget, focal_points=focus or [])
    return formatter.format_markdown(assembled)


# ---------------------------------------------------------------------------
# Navigation query tools
# ---------------------------------------------------------------------------

@mcp.tool()
def find_definition(symbol: str) -> str:
    """Find where a symbol (function, class, variable) is defined.

    Args:
        symbol: The name of the symbol to find
    """
    store = _open_store()
    rows = store.query(
        """
        MATCH (n:Function) WHERE n.name = $name
        RETURN 'function' AS kind, n.name AS name, n.file_path AS file, n.line_start AS line, n.signature AS sig
        UNION ALL
        MATCH (n:Class) WHERE n.name = $name
        RETURN 'class' AS kind, n.name AS name, n.file_path AS file, n.line_start AS line, '' AS sig
        UNION ALL
        MATCH (n:Variable) WHERE n.name = $name
        RETURN 'variable' AS kind, n.name AS name, n.file_path AS file, n.line_start AS line, '' AS sig
        """,
        {"name": symbol},
    )
    if not rows:
        return f"No definition found for '{symbol}'"
    results = [
        {"kind": r[0], "name": r[1], "file": r[2], "line": r[3], "signature": r[4]}
        for r in rows
    ]
    return json.dumps(results, indent=2)


@mcp.tool()
def find_callers(symbol: str) -> str:
    """Find all functions that call a given function.

    Args:
        symbol: The function name to find callers for
    """
    store = _open_store()
    rows = store.query(
        """
        MATCH (caller:Function)-[:CALLS]->(callee:Function)
        WHERE callee.name = $name
        RETURN DISTINCT caller.name AS caller, caller.file_path AS file, caller.line_start AS line
        """,
        {"name": symbol},
    )
    if not rows:
        return f"No callers found for '{symbol}'"
    results = [{"caller": r[0], "file": r[1], "line": r[2]} for r in rows]
    return json.dumps(results, indent=2)


@mcp.tool()
def find_callees(symbol: str) -> str:
    """Find all functions called by a given function.

    Args:
        symbol: The function name to find callees for
    """
    store = _open_store()
    rows = store.query(
        """
        MATCH (caller:Function)-[:CALLS]->(callee:Function)
        WHERE caller.name = $name
        RETURN DISTINCT callee.name AS callee, callee.file_path AS file, callee.line_start AS line
        """,
        {"name": symbol},
    )
    if not rows:
        return f"No callees found for '{symbol}'"
    results = [{"callee": r[0], "file": r[1], "line": r[2]} for r in rows]
    return json.dumps(results, indent=2)


@mcp.tool()
def blast_radius(symbol: str, depth: int = 5) -> str:
    """Find everything that transitively depends on a symbol.

    Use this before making changes to understand the impact.

    Args:
        symbol: The function name to check
        depth: Max traversal depth (default 5)
    """
    store = _open_store()
    rows = store.query(
        f"""
        MATCH (target:Function {{name: $name}})<-[:CALLS*1..{depth}]-(caller:Function)
        RETURN DISTINCT caller.name AS dependent, caller.file_path AS file
        """,
        {"name": symbol},
    )
    if not rows:
        return f"No transitive dependents found for '{symbol}'"
    results = [{"dependent": r[0], "file": r[1]} for r in rows]
    return json.dumps(results, indent=2)


@mcp.tool()
def module_structure(path: str, recursive: bool = True) -> str:
    """Show the structure of a module (directory) — its files, functions, classes.

    Args:
        path: Module/directory path (e.g. "src/auth")
        recursive: Include subdirectories (default True)
    """
    store = _open_store()
    path = _resolve_path(store, path)

    # Step 1: Find matching files
    if recursive:
        # Try module-based lookup first (matches module and sub-modules)
        file_rows = store.query(
            """
            MATCH (f:File)-[:BELONGS_TO]->(m:Module)
            WHERE m.path = $mp OR m.path STARTS WITH $mp_prefix
            RETURN DISTINCT f.path
            ORDER BY f.path
            """,
            {"mp": path, "mp_prefix": path + "/"},
        )
        if not file_rows:
            # Fallback: match files by path prefix directly
            file_rows = store.query(
                """
                MATCH (f:File)
                WHERE f.path STARTS WITH $prefix
                RETURN f.path
                ORDER BY f.path
                """,
                {"prefix": path},
            )
    else:
        file_rows = store.query(
            """
            MATCH (f:File)-[:BELONGS_TO]->(m:Module {path: $mp})
            RETURN f.path
            ORDER BY f.path
            """,
            {"mp": path},
        )

    if not file_rows:
        return _no_results_hint(store, path, "files")

    # Step 2: For each file, get functions and classes
    results = []
    for (fp,) in file_rows:
        funcs = store.query(
            "MATCH (f:File {path: $fp})-[:CONTAINS_FUNC]->(fn:Function) RETURN fn.name",
            {"fp": fp},
        )
        classes = store.query(
            "MATCH (f:File {path: $fp})-[:CONTAINS_CLASS]->(cls:Class) RETURN cls.name",
            {"fp": fp},
        )
        results.append({
            "file": fp,
            "functions": [r[0] for r in funcs],
            "classes": [r[0] for r in classes],
        })

    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# History query tools
# ---------------------------------------------------------------------------

@mcp.tool()
def recent_changes(path: str, limit: int = 20) -> str:
    """Show recent changes to a file or module.

    Args:
        path: File path or module path prefix
        limit: Max results (default 20)
    """
    store = _open_store()
    path = _resolve_path(store, path)
    rows = store.query(
        """
        MATCH (f:File)-[:CHANGED_IN]->(c:Commit)
        WHERE f.path STARTS WITH $path OR f.path = $path
        RETURN f.path AS file, c.message AS message,
               c.author AS author, c.timestamp AS timestamp
        ORDER BY c.timestamp DESC
        LIMIT $lim
        """,
        {"path": path, "lim": limit},
    )
    if not rows:
        return f"No recent changes found for '{path}'"
    results = [
        {"file": r[0], "message": r[1], "author": r[2], "timestamp": r[3]}
        for r in rows
    ]
    return json.dumps(results, indent=2)


@mcp.tool()
def co_changes(file: str) -> str:
    """Find files that frequently change together with a given file.

    Useful for understanding hidden dependencies and coupling.

    Args:
        file: The file path to check
    """
    store = _open_store()
    file = _resolve_path(store, file)
    rows = store.query(
        """
        MATCH (f:File {path: $fp})-[r:CO_CHANGES_WITH]->(other:File)
        RETURN other.path AS file, r.count AS times
        ORDER BY r.count DESC
        """,
        {"fp": file},
    )
    if not rows:
        return f"No co-change data for '{file}'"
    results = [{"file": r[0], "times": r[1]} for r in rows]
    return json.dumps(results, indent=2)


@mcp.tool()
def search_commits(query: str, author: str | None = None, limit: int = 20) -> str:
    """Search commit messages for a term (case-insensitive).

    Use this to find cross-cutting concerns (e.g. "MNPI", "auth refactor")
    that span many files and can't be found by file path alone.

    Args:
        query: Search string to match against commit messages
        author: Optional author name filter (case-insensitive)
        limit: Max results (default 20)
    """
    store = _open_store()

    if author:
        rows = store.query(
            """
            MATCH (c:Commit)
            WHERE lower(c.message) CONTAINS lower($q)
              AND lower(c.author) CONTAINS lower($a)
            OPTIONAL MATCH (f:File)-[:CHANGED_IN]->(c)
            RETURN c.hash, c.message, c.author, c.timestamp,
                   collect(f.path) AS files
            ORDER BY c.timestamp DESC
            LIMIT $lim
            """,
            {"q": query, "a": author, "lim": limit},
        )
    else:
        rows = store.query(
            """
            MATCH (c:Commit)
            WHERE lower(c.message) CONTAINS lower($q)
            OPTIONAL MATCH (f:File)-[:CHANGED_IN]->(c)
            RETURN c.hash, c.message, c.author, c.timestamp,
                   collect(f.path) AS files
            ORDER BY c.timestamp DESC
            LIMIT $lim
            """,
            {"q": query, "lim": limit},
        )

    if not rows:
        return f"No commits matching '{query}'"
    results = [
        {
            "hash": r[0], "message": r[1], "author": r[2],
            "timestamp": r[3], "files": r[4],
        }
        for r in rows
    ]
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Plan management tools
# ---------------------------------------------------------------------------

@mcp.tool()
def plan_create(title: str, description: str = "", status: str = "draft",
                author: str = "", targets: list[str] | None = None) -> str:
    """Create a new plan for tracking intended code changes.

    Plans help maintain continuity across sessions by recording what changes
    are planned, why, and what code they affect.

    Args:
        title: Plan title
        description: What this plan is about
        status: draft, active, completed, or abandoned
        author: Who created this plan
        targets: File paths or symbol names this plan targets
    """
    store = _open_store()
    mgr = PlanManager(store)
    plan_id = mgr.create_plan(
        title=title, description=description, status=status,
        author=author, targets=targets,
    )
    return json.dumps({"id": plan_id, "title": title, "status": status})


@mcp.tool()
def plan_list(status: str | None = None) -> str:
    """List all plans, optionally filtered by status.

    Args:
        status: Filter by status (draft, active, completed, abandoned) or omit for all
    """
    store = _open_store()
    mgr = PlanManager(store)
    plans = mgr.list_plans(status=status)
    if not plans:
        return "(no plans)"
    return json.dumps(plans, indent=2, default=str)


@mcp.tool()
def plan_show(plan_id: str) -> str:
    """Show full details of a plan including targets, dependencies, and intents.

    Args:
        plan_id: The plan ID
    """
    store = _open_store()
    mgr = PlanManager(store)
    plan = mgr.get_plan(plan_id)
    if not plan:
        return f"Plan '{plan_id}' not found"
    return json.dumps(plan, indent=2, default=str)


@mcp.tool()
def plan_update(plan_id: str, title: str | None = None,
                description: str | None = None, status: str | None = None) -> str:
    """Update a plan's properties.

    Args:
        plan_id: The plan ID to update
        title: New title (optional)
        description: New description (optional)
        status: New status: draft, active, completed, abandoned (optional)
    """
    store = _open_store()
    mgr = PlanManager(store)
    if mgr.update_plan(plan_id, title=title, description=description, status=status):
        return f"Updated plan {plan_id}"
    return f"Plan '{plan_id}' not found"


@mcp.tool()
def plan_add_intent(plan_id: str, description: str, rationale: str = "",
                    affected_files: list[str] | None = None) -> str:
    """Add an intent (a specific change step) to a plan.

    Args:
        plan_id: The plan to add the intent to
        description: What this intent will do
        rationale: Why this change is needed
        affected_files: File paths this intent affects (auto-linked as plan targets)
    """
    store = _open_store()
    mgr = PlanManager(store)
    intent_id = mgr.create_intent(
        plan_id, description=description, rationale=rationale,
        affected_files=affected_files,
    )
    return json.dumps({"intent_id": intent_id, "plan_id": plan_id})


@mcp.tool()
def plan_update_intent(intent_id: str, status: str | None = None,
                       description: str | None = None) -> str:
    """Update an intent's status or description.

    Args:
        intent_id: The intent ID to update
        status: New status: draft, active, completed, abandoned
        description: New description (optional)
    """
    store = _open_store()
    mgr = PlanManager(store)
    if mgr.update_intent(intent_id, status=status, description=description):
        return f"Updated intent {intent_id}"
    return f"Intent '{intent_id}' not found"


# ---------------------------------------------------------------------------
# Utility tools
# ---------------------------------------------------------------------------

@mcp.tool()
def dead_code(path: str | None = None, include_methods: bool = False) -> str:
    """Find functions that are never called — likely dead code.

    Identifies functions with zero inbound CALLS edges. Filters out common
    entry points (main, __init__, test functions, CLI commands, etc.).

    Args:
        path: Optional file or module path to scope the search
        include_methods: Include class methods (default False — methods are
                         often called via dynamic dispatch which the static
                         graph can't see)
    """
    store = _open_store()

    if path:
        path = _resolve_path(store, path)
        rows = store.query(
            """
            MATCH (f:Function)
            WHERE f.file_path STARTS WITH $prefix
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
            WITH f, count(caller) AS caller_count
            WHERE caller_count = 0
            RETURN f.name, f.file_path, f.line_start, f.signature, f.is_method
            ORDER BY f.file_path, f.line_start
            """,
            {"prefix": path},
        )
    else:
        rows = store.query(
            """
            MATCH (f:Function)
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(f)
            WITH f, count(caller) AS caller_count
            WHERE caller_count = 0
            RETURN f.name, f.file_path, f.line_start, f.signature, f.is_method
            ORDER BY f.file_path, f.line_start
            """,
        )

    if not rows:
        return "No dead code candidates found."

    # Filter out known entry points and optionally methods
    entry_patterns = {
        "main", "__init__", "__main__", "setup", "teardown",
        "setUp", "tearDown", "setUpClass", "tearDownClass",
    }
    entry_prefixes = ("test_", "Test")

    results = []
    for r in rows:
        name, file_path, line, sig, is_method = r[0], r[1], r[2], r[3], r[4]

        # Skip known entry points
        if name in entry_patterns:
            continue
        if any(name.startswith(p) for p in entry_prefixes):
            continue

        # Skip methods unless explicitly included
        if is_method and not include_methods:
            continue

        results.append({
            "name": name, "file": file_path, "line": line,
            "signature": sig or "",
        })

    if not results:
        return "No dead code candidates found (after filtering entry points)."
    return json.dumps(results, indent=2)


@mcp.tool()
def timeline(target: str, limit: int = 20, format: str = "markdown",
             include_neighbors: bool = True) -> str:
    """Unified past+future view for a file, module, or symbol.

    Combines git history (commits affecting the target) with active/draft plans
    targeting it. Optionally includes co-change neighbors and (for symbols)
    blast-radius callers. Use this when you want the full story of an area in
    one call instead of stitching together recent_changes + plan_show + co_changes.

    Args:
        target: File path, module path, function name, or class name
        limit: Max past commits to return (default 20)
        format: "markdown" (default, agent-friendly) or "json" (programmatic)
        include_neighbors: Include co-change neighbors and callers (default True)
    """
    from .timeline import get_timeline, format_markdown, format_json
    store = _open_store()
    data = get_timeline(store, target, limit=limit, include_neighbors=include_neighbors)
    if data["target"] is None:
        return _no_results_hint(store, target, "timeline data")
    if format == "json":
        return format_json(data)
    return format_markdown(data)


@mcp.tool()
def reindex(scope: str = "incremental", layer: str = "structure") -> str:
    """Re-run indexing without restarting the MCP server.

    Useful as a manual escape hatch when the in-process watcher missed
    something (e.g. files were modified before the server started, or
    history needs a fresh pull).

    Args:
        scope: "incremental" — re-scan changed files only (structure layer);
               "all" — re-scan every file in the repo.
        layer: "structure" — code structure (files, symbols, calls);
               "history" — git commits + changes;
               "all" — both layers.
    """
    if scope not in ("incremental", "all"):
        return f"Invalid scope '{scope}'. Use 'incremental' or 'all'."
    if layer not in ("structure", "history", "all"):
        return f"Invalid layer '{layer}'. Use 'structure', 'history', or 'all'."

    repo = _repo_path()
    store = _open_store()
    summary: dict[str, dict] = {}

    if layer in ("structure", "all"):
        from .indexer.structure import StructureIndexer

        indexer = StructureIndexer(store, Path(repo))
        if scope == "all":
            summary["structure"] = indexer.index_full()
        else:
            summary["structure"] = indexer.index_incremental(since_hash=None)

    if layer in ("history", "all"):
        from .indexer.history import HistoryIndexer

        h_indexer = HistoryIndexer(store, repo)
        summary["history"] = h_indexer.index()

    return json.dumps(summary, indent=2, default=str)


@mcp.tool()
def graph_stats() -> str:
    """Show graph statistics — node and edge counts by type.

    Use this to verify the graph is indexed and get a sense of codebase size.
    """
    store = _open_store()
    stats = {}
    for label in ("File", "Module", "Function", "Class", "Type", "Variable",
                   "Endpoint", "Event", "Schema", "Commit", "Change", "Plan", "Intent"):
        rows = store.query(f"MATCH (n:{label}) RETURN count(n)")
        count = rows[0][0] if rows else 0
        if count > 0:
            stats[label] = count
    if not stats:
        return "(graph is empty — run `cartographer index` first)"
    return json.dumps(stats, indent=2)


@mcp.tool()
def run_cypher(query: str) -> str:
    """Execute a raw Cypher query against the graph.

    Use this for ad-hoc queries not covered by other tools. The graph uses
    LadybugDB (Kùzu-compatible Cypher).

    Args:
        query: A Cypher query string
    """
    store = _open_store()
    rows = store.query(query)
    if not rows:
        return "(no results)"
    return json.dumps(rows, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_server() -> None:
    """Start the MCP server on stdio."""
    mcp.run()
