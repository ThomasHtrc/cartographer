"""CLI entry point for graph-context."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click

from . import config
from .storage.store import GraphStore, DatabaseLockedError
from .indexer.structure import StructureIndexer
from .indexer.history import HistoryIndexer
from .indexer.plans import PlanIndexer
from .indexer import git_ops
from .plans.manager import PlanManager
from .context.ranker import Ranker
from .context.assembler import Assembler
from .context import formatter


@click.group()
@click.option("--repo", default=".", help="Path to the repository root.")
@click.pass_context
def cli(ctx: click.Context, repo: str) -> None:
    """graph-context: Graph-based codebase understanding for coding agents."""
    ctx.ensure_object(dict)
    ctx.obj["repo"] = str(Path(repo).resolve())


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--with-mcp", is_flag=True, help="Also generate .mcp.json for Claude Code integration.")
@click.option("--with-claude-md", is_flag=True, help="Also append graph-context instructions to CLAUDE.md.")
@click.pass_context
def init(ctx: click.Context, with_mcp: bool, with_claude_md: bool) -> None:
    """Initialize graph-context in the current repository."""
    repo = ctx.obj["repo"]
    project_dir = config.get_project_dir(repo)
    project_dir.mkdir(parents=True, exist_ok=True)

    meta = config.load_meta(repo)
    if not meta:
        config.save_meta(repo, {"initialized": True, "last_commit": None})
        click.echo(f"Initialized graph-context in {project_dir}")
    else:
        click.echo(f"graph-context already initialized in {project_dir}")

    if with_mcp:
        _write_mcp_json(repo)

    if with_claude_md:
        _write_claude_md(repo)


@cli.command()
@click.pass_context
def setup(ctx: click.Context) -> None:
    """One-command setup: init + index + MCP config + CLAUDE.md.

    Run this in your project root to get started with graph-context and Claude Code.
    """
    repo = ctx.obj["repo"]
    project_dir = config.get_project_dir(repo)

    # Check if already set up
    meta = config.load_meta(repo)
    mcp_exists = (Path(repo) / ".mcp.json").exists()
    claude_md = Path(repo) / "CLAUDE.md"
    claude_md_has_gc = claude_md.exists() and "graph-context" in claude_md.read_text()

    if meta and meta.get("initialized") and mcp_exists and claude_md_has_gc:
        click.echo("graph-context is already set up in this project.")
        click.echo("To re-index, run: graph-context index")
        click.echo("To re-index from scratch, run: graph-context index --clean")
        return

    # Init
    project_dir.mkdir(parents=True, exist_ok=True)
    if not meta:
        config.save_meta(repo, {"initialized": True, "last_commit": None})
        meta = config.load_meta(repo)
    click.echo(f"Initialized graph-context in {project_dir}")

    # Index
    ctx.invoke(index)

    # MCP + CLAUDE.md
    _write_mcp_json(repo)
    _write_claude_md(repo)

    click.echo("\nSetup complete! To register with Claude Code, run:")
    click.echo("  claude mcp add graph-context -- graph-context-mcp")


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--quiet", "-q", is_flag=True, help="Suppress per-file output.")
@click.option("--daemon", "-d", is_flag=True, help="Run in background as a daemon.")
@click.option("--stop", is_flag=True, help="Stop a running daemon.")
@click.option("--status", "show_status", is_flag=True, help="Check daemon status.")
@click.pass_context
def watch(ctx: click.Context, quiet: bool, daemon: bool, stop: bool, show_status: bool) -> None:
    """Watch for file changes and auto-reindex.

    Monitors supported source files and re-indexes them on save.
    Uses watchfiles (Rust-backed) with 1.6s debounce for batching rapid saves.

    Run in background:  graph-context watch --daemon
    Check status:       graph-context watch --status
    Stop daemon:        graph-context watch --stop

    Note: if you're running graph-context-mcp, the MCP server already keeps
    the index fresh in-process — you don't need a separate watcher daemon.
    """
    repo = ctx.obj["repo"]
    meta = config.load_meta(repo)
    if not meta or not meta.get("initialized"):
        click.echo("Error: graph-context not initialized. Run `graph-context setup` first.")
        raise SystemExit(1)

    from .watcher import run_watcher, start_daemon, stop_daemon, daemon_status

    if show_status:
        info = daemon_status(repo)
        if info and info["running"]:
            click.echo(f"Watcher running (PID {info['pid']})")
        else:
            click.echo("Watcher not running.")
        return

    if stop:
        if stop_daemon(repo):
            click.echo("Watcher stopped.")
        else:
            click.echo("No watcher running.")
        return

    if daemon:
        pid = start_daemon(repo)
        if os.getpid() != pid:
            # We're the parent
            click.echo(f"Watcher started in background (PID {pid})")
            click.echo(f"  Stop with: graph-context watch --stop")
            click.echo(f"  Log: .graph-context/watcher.log")
        return

    run_watcher(repo, quiet=quiet)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--incremental", is_flag=True, help="Only index changed files.")
@click.option("--layer", type=click.Choice(["structure", "history", "planning", "all"]), default="all")
@click.option("--clean", is_flag=True, help="Clear existing data before re-indexing.")
@click.pass_context
def index(ctx: click.Context, incremental: bool, layer: str, clean: bool) -> None:
    """Index the codebase into the graph."""
    repo = ctx.obj["repo"]
    db_path = config.get_db_path(repo)
    meta = config.load_meta(repo)

    with GraphStore(db_path) as store:
        if layer in ("structure", "all"):
            indexer = StructureIndexer(store, repo)
            t0 = time.time()

            if incremental and meta.get("last_commit"):
                stats = indexer.index_incremental(meta["last_commit"])
                mode = "incremental"
            else:
                stats = indexer.index_full()
                mode = "full"

            elapsed = time.time() - t0
            err_msg = f", {stats['errors']} errors" if stats.get("errors") else ""
            click.echo(
                f"Structure index ({mode}): "
                f"{stats['files_indexed']} files, "
                f"{stats['nodes_created']} nodes, "
                f"{stats['edges_created']} edges"
                f"{err_msg} "
                f"({elapsed:.2f}s)"
            )

        if layer in ("history", "all"):
            if git_ops.is_git_repo(repo):
                if clean:
                    store.clear_history()
                    click.echo("Cleared existing history data.")
                hist_indexer = HistoryIndexer(store, repo)
                t0 = time.time()

                since = meta.get("last_history_commit") if incremental else None
                hist_stats = hist_indexer.index(since_hash=since)

                elapsed = time.time() - t0
                click.echo(
                    f"History index: "
                    f"{hist_stats['commits']} commits, "
                    f"{hist_stats['changes']} changes, "
                    f"{hist_stats['affects']} affects, "
                    f"{hist_stats['co_changes']} co-change edges "
                    f"({elapsed:.2f}s)"
                )
            else:
                click.echo("History index: skipped (not a git repo)")

        if layer in ("planning", "all"):
            plan_indexer = PlanIndexer(store, repo)
            t0 = time.time()
            plan_stats = plan_indexer.index()
            elapsed = time.time() - t0
            total = plan_stats["created"] + plan_stats["updated"]
            if total > 0 or plan_stats["skipped"] > 0:
                click.echo(
                    f"Planning index: "
                    f"{plan_stats['created']} created, "
                    f"{plan_stats['updated']} updated, "
                    f"{plan_stats['skipped']} skipped "
                    f"({elapsed:.2f}s)"
                )

    # Update meta
    head = git_ops.get_head_hash(repo) if git_ops.is_git_repo(repo) else None
    if layer in ("structure", "all"):
        meta["last_commit"] = head
    if layer in ("history", "all"):
        meta["last_history_commit"] = head
    config.save_meta(repo, meta)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

@cli.group()
@click.pass_context
def query(ctx: click.Context) -> None:
    """Query the codebase graph."""
    pass


@query.command("definition")
@click.argument("symbol")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_definition(ctx: click.Context, symbol: str, fmt: str) -> None:
    """Find where a symbol is defined."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            """
            MATCH (n:Function)
            WHERE n.name = $name
            RETURN 'function' AS kind, n.name AS name, n.file_path AS file, n.line_start AS line, n.signature AS sig
            UNION ALL
            MATCH (n:Class)
            WHERE n.name = $name
            RETURN 'class' AS kind, n.name AS name, n.file_path AS file, n.line_start AS line, '' AS sig
            UNION ALL
            MATCH (n:Variable)
            WHERE n.name = $name
            RETURN 'variable' AS kind, n.name AS name, n.file_path AS file, n.line_start AS line, '' AS sig
            """,
            {"name": symbol},
        )
        _output(rows, ["kind", "name", "file", "line", "signature"], fmt)


@query.command("callers")
@click.argument("symbol")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_callers(ctx: click.Context, symbol: str, fmt: str) -> None:
    """Find what calls a function."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            """
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE callee.name = $name
            RETURN DISTINCT caller.name AS caller, caller.file_path AS file, caller.line_start AS line
            """,
            {"name": symbol},
        )
        _output(rows, ["caller", "file", "line"], fmt)


@query.command("callees")
@click.argument("symbol")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_callees(ctx: click.Context, symbol: str, fmt: str) -> None:
    """Find what a function calls."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            """
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE caller.name = $name
            RETURN DISTINCT callee.name AS callee, callee.file_path AS file, callee.line_start AS line
            """,
            {"name": symbol},
        )
        _output(rows, ["callee", "file", "line"], fmt)


@query.command("imports")
@click.argument("file")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_imports(ctx: click.Context, file: str, fmt: str) -> None:
    """Show what a file imports."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            "MATCH (f:File {path: $fp})-[:IMPORTS]->(target:File) RETURN target.path AS imported_file",
            {"fp": file},
        )
        _output(rows, ["imported_file"], fmt)


@query.command("module")
@click.argument("path")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_module(ctx: click.Context, path: str, fmt: str) -> None:
    """Show the structure of a module."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            """
            MATCH (f:File)-[:BELONGS_TO]->(m:Module {path: $mp})
            OPTIONAL MATCH (f)-[:CONTAINS_FUNC]->(fn:Function)
            OPTIONAL MATCH (f)-[:CONTAINS_CLASS]->(cls:Class)
            RETURN f.path AS file,
                   collect(DISTINCT fn.name) AS functions,
                   collect(DISTINCT cls.name) AS classes
            """,
            {"mp": path},
        )
        _output(rows, ["file", "functions", "classes"], fmt)


@query.command("blast-radius")
@click.argument("symbol")
@click.option("--depth", default=5, help="Max traversal depth.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_blast_radius(ctx: click.Context, symbol: str, depth: int, fmt: str) -> None:
    """Find everything that transitively depends on a symbol."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            f"""
            MATCH (target:Function {{name: $name}})<-[:CALLS*1..{depth}]-(caller:Function)
            RETURN DISTINCT caller.name AS dependent, caller.file_path AS file
            """,
            {"name": symbol},
        )
        _output(rows, ["dependent", "file"], fmt)


@query.command("cycles")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_cycles(ctx: click.Context, fmt: str) -> None:
    """Detect circular dependencies between files."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema(layers=("structure",))
        rows = store.query(
            """
            MATCH (a:File)-[:IMPORTS*2..6]->(a)
            RETURN DISTINCT a.path AS file_in_cycle
            """,
        )
        _output(rows, ["file_in_cycle"], fmt)


# ---------------------------------------------------------------------------
# history queries
# ---------------------------------------------------------------------------

@query.command("recent")
@click.argument("path")
@click.option("--since", default="30", help="Number of days to look back (default: 30).")
@click.option("--limit", "max_results", default=20, help="Max results.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_recent(ctx: click.Context, path: str, since: str, max_results: int, fmt: str) -> None:
    """Show recent changes to a file or module."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        # Try as a file first, then as a module path prefix
        rows = store.query(
            """
            MATCH (f:File)-[:CHANGED_IN]->(c:Commit)
            WHERE f.path STARTS WITH $path OR f.path = $path
            RETURN f.path AS file, c.message AS commit_message,
                   c.author AS author, c.timestamp AS timestamp
            ORDER BY c.timestamp DESC
            LIMIT $lim
            """,
            {"path": path, "lim": max_results},
        )
        _output(rows, ["file", "commit_message", "author", "timestamp"], fmt)


@query.command("co-changes")
@click.argument("file")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_co_changes(ctx: click.Context, file: str, fmt: str) -> None:
    """Show files that frequently change together with a given file."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        rows = store.query(
            """
            MATCH (f:File {path: $fp})-[r:CO_CHANGES_WITH]->(other:File)
            RETURN other.path AS co_changed_file, r.count AS times
            ORDER BY r.count DESC
            """,
            {"fp": file},
        )
        _output(rows, ["co_changed_file", "times"], fmt)


@query.command("churn")
@click.argument("path")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_churn(ctx: click.Context, path: str, fmt: str) -> None:
    """Show most frequently changed files in a module/directory."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        rows = store.query(
            """
            MATCH (f:File)-[:CHANGED_IN]->(c:Commit)
            WHERE f.path STARTS WITH $path
            RETURN f.path AS file, count(c) AS changes
            ORDER BY changes DESC
            """,
            {"path": path},
        )
        _output(rows, ["file", "changes"], fmt)


@query.command("authors")
@click.argument("path")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_authors(ctx: click.Context, path: str, fmt: str) -> None:
    """Show who has worked on a file or module."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        rows = store.query(
            """
            MATCH (f:File)-[:CHANGED_IN]->(c:Commit)
            WHERE f.path STARTS WITH $path OR f.path = $path
            RETURN c.author AS author, count(c) AS commits
            ORDER BY commits DESC
            """,
            {"path": path},
        )
        _output(rows, ["author", "commits"], fmt)


@query.command("affected-symbols")
@click.argument("commit_hash")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_affected_symbols(ctx: click.Context, commit_hash: str, fmt: str) -> None:
    """Show which functions/classes were affected by a commit."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        rows = store.query(
            """
            MATCH (c:Commit {hash: $hash})-[:INCLUDES]->(ch:Change)-[:AFFECTS_FUNC]->(fn:Function)
            RETURN 'function' AS kind, fn.name AS name, fn.file_path AS file, ch.file_path AS changed_file
            UNION ALL
            MATCH (c:Commit {hash: $hash})-[:INCLUDES]->(ch:Change)-[:AFFECTS_CLASS]->(cls:Class)
            RETURN 'class' AS kind, cls.name AS name, cls.file_path AS file, ch.file_path AS changed_file
            """,
            {"hash": commit_hash},
        )
        _output(rows, ["kind", "name", "file", "changed_file"], fmt)


@query.command("search-commits")
@click.argument("search_query")
@click.option("--author", default=None, help="Filter by author (case-insensitive).")
@click.option("--limit", "max_results", default=20, help="Max results.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_search_commits(ctx: click.Context, search_query: str, author: str | None, max_results: int, fmt: str) -> None:
    """Search commit messages for a term (case-insensitive)."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        if author:
            rows = store.query(
                """
                MATCH (c:Commit)
                WHERE lower(c.message) CONTAINS lower($q)
                  AND lower(c.author) CONTAINS lower($a)
                RETURN c.hash, c.message, c.author, c.timestamp
                ORDER BY c.timestamp DESC
                LIMIT $lim
                """,
                {"q": search_query, "a": author, "lim": max_results},
            )
        else:
            rows = store.query(
                """
                MATCH (c:Commit)
                WHERE lower(c.message) CONTAINS lower($q)
                RETURN c.hash, c.message, c.author, c.timestamp
                ORDER BY c.timestamp DESC
                LIMIT $lim
                """,
                {"q": search_query, "lim": max_results},
            )
        _output(rows, ["hash", "message", "author", "timestamp"], fmt)


@query.command("dead-code")
@click.option("--path", default=None, help="Scope to a file or module path.")
@click.option("--include-methods", is_flag=True, help="Include class methods.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_dead_code(ctx: click.Context, path: str | None, include_methods: bool, fmt: str) -> None:
    """Find functions that are never called (likely dead code)."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()

        if path:
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

        entry_patterns = {
            "main", "__init__", "__main__", "setup", "teardown",
            "setUp", "tearDown", "setUpClass", "tearDownClass",
        }
        entry_prefixes = ("test_", "Test")

        filtered = []
        for r in rows:
            name, is_method = r[0], r[4]
            if name in entry_patterns:
                continue
            if any(name.startswith(p) for p in entry_prefixes):
                continue
            if is_method and not include_methods:
                continue
            filtered.append(r[:4])  # name, file_path, line_start, signature

        if fmt == "json":
            import json as json_mod
            data = [{"name": r[0], "file": r[1], "line": r[2], "signature": r[3] or ""} for r in filtered]
            click.echo(json_mod.dumps(data, indent=2))
        else:
            _output(filtered, ["name", "file", "line", "signature"], fmt)


# ---------------------------------------------------------------------------
# plan queries (on the query subgroup)
# ---------------------------------------------------------------------------

@query.command("plans")
@click.argument("path")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def query_plans_for(ctx: click.Context, path: str, fmt: str) -> None:
    """Show plans targeting a file, module, or symbol."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        rows = store.query(
            """
            MATCH (p:Plan)-[:TARGETS_FILE]->(f:File)
            WHERE f.path STARTS WITH $path OR f.path = $path
            RETURN p.id AS plan_id, p.title AS title, p.status AS status, f.path AS target
            UNION ALL
            MATCH (p:Plan)-[:TARGETS_MODULE]->(m:Module)
            WHERE m.path STARTS WITH $path OR m.path = $path
            RETURN p.id AS plan_id, p.title AS title, p.status AS status, m.path AS target
            UNION ALL
            MATCH (p:Plan)-[:TARGETS_FUNC]->(fn:Function)
            WHERE fn.name = $path OR fn.file_path STARTS WITH $path
            RETURN p.id AS plan_id, p.title AS title, p.status AS status, fn.name AS target
            UNION ALL
            MATCH (p:Plan)-[:TARGETS_CLASS]->(c:Class)
            WHERE c.name = $path OR c.file_path STARTS WITH $path
            RETURN p.id AS plan_id, p.title AS title, p.status AS status, c.name AS target
            """,
            {"path": path},
        )
        _output(rows, ["plan_id", "title", "status", "target"], fmt)


# ---------------------------------------------------------------------------
# plan management commands
# ---------------------------------------------------------------------------

@cli.group()
@click.pass_context
def plan(ctx: click.Context) -> None:
    """Manage plans (Layer 3)."""
    pass


@plan.command("create")
@click.argument("title")
@click.option("--description", "-d", default="", help="Plan description.")
@click.option("--status", type=click.Choice(["draft", "active", "completed", "abandoned"]), default="draft")
@click.option("--author", default="", help="Plan author.")
@click.option("--targets", "-t", multiple=True, help="Target files, modules, or symbols.")
@click.option("--depends-on", multiple=True, help="Plan IDs this plan depends on.")
@click.pass_context
def plan_create(ctx: click.Context, title: str, description: str, status: str, author: str, targets: tuple, depends_on: tuple) -> None:
    """Create a new plan."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        plan_id = mgr.create_plan(
            title=title, description=description, status=status, author=author,
            targets=list(targets) if targets else None,
            depends_on=list(depends_on) if depends_on else None,
        )
        click.echo(f"Created plan {plan_id}: {title}")


@plan.command("list")
@click.option("--status", type=click.Choice(["draft", "active", "completed", "abandoned"]), default=None)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.pass_context
def plan_list(ctx: click.Context, status: str | None, fmt: str) -> None:
    """List plans."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        plans = mgr.list_plans(status=status)
        if not plans:
            click.echo("(no plans)")
            return
        rows = [[p["id"], p["title"], p["status"], p["updated_at"], p["author"]] for p in plans]
        _output(rows, ["id", "title", "status", "updated_at", "author"], fmt)


@plan.command("show")
@click.argument("plan_id")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="json")
@click.pass_context
def plan_show(ctx: click.Context, plan_id: str, fmt: str) -> None:
    """Show plan details."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        p = mgr.get_plan(plan_id)
        if not p:
            click.echo(f"Plan {plan_id} not found.")
            return
        if fmt == "json":
            click.echo(json.dumps(p, indent=2, default=str))
        else:
            click.echo(f"Plan: {p['title']} [{p['status']}]")
            click.echo(f"  ID: {p['id']}")
            click.echo(f"  Author: {p['author']}")
            click.echo(f"  Description: {p['description']}")
            click.echo(f"  Created: {p['created_at']}")
            click.echo(f"  Updated: {p['updated_at']}")
            if p["targets"]:
                click.echo("  Targets:")
                for t in p["targets"]:
                    click.echo(f"    - [{t['kind']}] {t['id']} ({t['name']})")
            if p["depends_on"]:
                click.echo("  Depends on:")
                for d in p["depends_on"]:
                    click.echo(f"    - {d['id']}: {d['title']} [{d['status']}]")
            if p["intents"]:
                click.echo("  Intents:")
                for i in p["intents"]:
                    click.echo(f"    - [{i['status']}] {i['description']}")
                    if i["rationale"]:
                        click.echo(f"      Rationale: {i['rationale']}")


@plan.command("update")
@click.argument("plan_id")
@click.option("--title", default=None)
@click.option("--description", "-d", default=None)
@click.option("--status", type=click.Choice(["draft", "active", "completed", "abandoned"]), default=None)
@click.pass_context
def plan_update(ctx: click.Context, plan_id: str, title: str | None, description: str | None, status: str | None) -> None:
    """Update a plan's properties."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        if mgr.update_plan(plan_id, title=title, description=description, status=status):
            click.echo(f"Updated plan {plan_id}")
        else:
            click.echo(f"Plan {plan_id} not found.")


@plan.command("link")
@click.argument("plan_id")
@click.option("--targets", "-t", multiple=True, required=True, help="Target files, modules, or symbols.")
@click.pass_context
def plan_link(ctx: click.Context, plan_id: str, targets: tuple) -> None:
    """Link a plan to target files, modules, or symbols."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        linked = mgr.link_targets(plan_id, list(targets))
        click.echo(f"Linked {linked} target(s) to plan {plan_id}")


@plan.command("delete")
@click.argument("plan_id")
@click.pass_context
def plan_delete(ctx: click.Context, plan_id: str) -> None:
    """Delete a plan."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        if mgr.delete_plan(plan_id):
            click.echo(f"Deleted plan {plan_id}")
        else:
            click.echo(f"Plan {plan_id} not found.")


@plan.command("intent")
@click.argument("plan_id")
@click.option("--description", "-d", required=True, help="Intent description.")
@click.option("--rationale", "-r", default="", help="Why this intent.")
@click.pass_context
def plan_add_intent(ctx: click.Context, plan_id: str, description: str, rationale: str) -> None:
    """Add an intent to a plan."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        mgr = PlanManager(store)
        intent_id = mgr.create_intent(plan_id, description=description, rationale=rationale)
        click.echo(f"Created intent {intent_id} on plan {plan_id}")


# ---------------------------------------------------------------------------
# context generation
# ---------------------------------------------------------------------------

@cli.command("context")
@click.option("--focus", "-f", multiple=True, required=True, help="Focal file paths or symbol names.")
@click.option("--budget", "-b", default=4000, help="Token budget (default: 4000).")
@click.option("--max-results", default=100, help="Max nodes to rank.")
@click.option("--format", "fmt", type=click.Choice(["markdown", "json", "annotated"]), default="markdown")
@click.option("--full-code", is_flag=True, help="Include full code blocks instead of signatures only.")
@click.pass_context
def context_cmd(ctx: click.Context, focus: tuple, budget: int, max_results: int, fmt: str, full_code: bool) -> None:
    """Generate ranked context for LLM prompts."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        ranker = Ranker(store)
        ranked = ranker.rank(list(focus), max_results=max_results)
        if not ranked:
            click.echo("(no relevant nodes found)")
            return

        assembler = Assembler(repo, signature_only=not full_code, store=store)
        assembled = assembler.assemble(ranked, budget, focal_points=list(focus))

        if fmt == "json":
            click.echo(formatter.format_json(assembled))
        elif fmt == "annotated":
            click.echo(formatter.format_annotated(assembled))
        else:
            click.echo(formatter.format_markdown(assembled))


@cli.command("map")
@click.option("--focus", "-f", multiple=True, help="Focal file paths or modules (default: entire repo).")
@click.option("--budget", "-b", default=8000, help="Token budget (default: 8000).")
@click.option("--format", "fmt", type=click.Choice(["markdown", "json", "annotated"]), default="markdown")
@click.pass_context
def map_cmd(ctx: click.Context, focus: tuple, budget: int, fmt: str) -> None:
    """Generate a repo map (like aider's RepoMap)."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        ranker = Ranker(store)

        if focus:
            ranked = ranker.rank(list(focus), max_results=200)
        else:
            # No focal point — rank everything by global PageRank
            # Use all files as weak focal points
            files = store.query("MATCH (f:File) RETURN f.path")
            all_paths = [r[0] for r in files]
            if not all_paths:
                click.echo("(no indexed files)")
                return
            ranked = ranker.rank(all_paths, max_results=200)

        if not ranked:
            click.echo("(no nodes found)")
            return

        assembler = Assembler(repo, signature_only=True, store=store)
        assembled = assembler.assemble(ranked, budget, focal_points=list(focus))

        if fmt == "json":
            click.echo(formatter.format_json(assembled))
        elif fmt == "annotated":
            click.echo(formatter.format_annotated(assembled))
        else:
            click.echo(formatter.format_markdown(assembled))


# ---------------------------------------------------------------------------
# timeline (past + future view)
# ---------------------------------------------------------------------------

@cli.command("timeline")
@click.argument("target")
@click.option("--format", "fmt", type=click.Choice(["markdown", "json", "html"]), default="markdown",
              help="Output format (default: markdown).")
@click.option("--limit", default=20, help="Max past commits to show (default: 20).")
@click.option("--no-neighbors", is_flag=True, default=False,
              help="Skip co-change neighbors and callers.")
@click.option("--out", type=click.Path(), default=None,
              help="Write output to file instead of stdout.")
@click.option("--open", "open_browser", is_flag=True, default=False,
              help="Open the result in a browser (implies --format html).")
@click.pass_context
def timeline_cmd(
    ctx: click.Context,
    target: str,
    fmt: str,
    limit: int,
    no_neighbors: bool,
    out: str | None,
    open_browser: bool,
) -> None:
    """Show a unified past+future timeline for a file, module, or symbol."""
    from .timeline import get_timeline, format_markdown, format_json, render_html

    if open_browser:
        fmt = "html"

    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        data = get_timeline(
            store, target, limit=limit, include_neighbors=not no_neighbors,
        )

        if data["target"] is None:
            click.echo(f"(target '{target}' not found in the graph)")
            return

        if fmt == "html":
            content = render_html(data)
        elif fmt == "json":
            content = format_json(data)
        else:
            content = format_markdown(data)

        out_path: Path | None = Path(out) if out else None
        if open_browser and out_path is None:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8",
            )
            tmp.write(content)
            tmp.close()
            out_path = Path(tmp.name)
        elif out_path is not None:
            out_path.write_text(content, encoding="utf-8")

        if out_path is not None:
            click.echo(f"Wrote {out_path}")
        else:
            click.echo(content)

        if open_browser and out_path is not None:
            import webbrowser
            webbrowser.open(f"file://{out_path.resolve()}")


# ---------------------------------------------------------------------------
# cypher (escape hatch)
# ---------------------------------------------------------------------------

@cli.command("cypher")
@click.argument("query_str")
@click.pass_context
def raw_cypher(ctx: click.Context, query_str: str) -> None:
    """Execute a raw Cypher query."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        rows = store.query(query_str)
        for row in rows:
            click.echo(row)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@cli.command("stats")
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show graph statistics."""
    repo = ctx.obj["repo"]
    with GraphStore(config.get_db_path(repo)) as store:
        store.ensure_schema()
        for label in ("File", "Module", "Function", "Class", "Type", "Variable", "Endpoint", "Event", "Schema", "Commit", "Change", "Plan", "Intent"):
            rows = store.query(f"MATCH (n:{label}) RETURN count(n)")
            count = rows[0][0] if rows else 0
            if count > 0:
                click.echo(f"  {label:12s} {count:>6d}")


# ---------------------------------------------------------------------------
# mcp server
# ---------------------------------------------------------------------------

@cli.command("mcp")
@click.pass_context
def mcp_cmd(ctx: click.Context) -> None:
    """Start the MCP server (stdio transport)."""
    import os
    os.environ["GRAPH_CONTEXT_REPO"] = ctx.obj["repo"]
    from .mcp_server import run_server
    run_server()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_mcp_json(repo: str) -> None:
    """Write .mcp.json for Claude Code MCP integration."""
    mcp_path = Path(repo) / ".mcp.json"
    if mcp_path.exists():
        click.echo(f".mcp.json already exists at {mcp_path}")
        return
    template = Path(__file__).parent / "templates" / "mcp.json"
    mcp_path.write_text(template.read_text())
    click.echo(f"Created {mcp_path}")


def _write_claude_md(repo: str) -> None:
    """Append graph-context instructions to CLAUDE.md."""
    claude_md = Path(repo) / "CLAUDE.md"
    template = Path(__file__).parent / "templates" / "CLAUDE.md"
    snippet = template.read_text()

    if claude_md.exists():
        existing = claude_md.read_text()
        if "graph-context" in existing:
            click.echo("CLAUDE.md already contains graph-context instructions")
            return
        with claude_md.open("a") as f:
            f.write("\n\n" + snippet)
        click.echo(f"Appended graph-context instructions to {claude_md}")
    else:
        claude_md.write_text(snippet)
        click.echo(f"Created {claude_md}")


def _output(rows: list[list], columns: list[str], fmt: str) -> None:
    """Format and print query results."""
    if not rows:
        click.echo("(no results)")
        return

    if fmt == "json":
        data = [dict(zip(columns, row)) for row in rows]
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        # Simple table format
        widths = [len(c) for c in columns]
        for row in rows:
            for i, val in enumerate(row):
                widths[i] = max(widths[i], len(str(val)))

        header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
        click.echo(header)
        click.echo("  ".join("-" * w for w in widths))
        for row in rows:
            line = "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row))
            click.echo(line)


# ---------------------------------------------------------------------------
# main() entry point — wraps cli() to print DatabaseLockedError cleanly
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point used by the ``graph-context`` console script.

    Runs the Click group and intercepts ``DatabaseLockedError`` so the user
    sees a clean message instead of a traceback when the LadybugDB lock is
    held by another process (typically a running graph-context-mcp server).
    """
    try:
        cli(standalone_mode=True)
    except DatabaseLockedError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)
