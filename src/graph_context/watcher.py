"""File watcher: auto-reindex on file changes.

Uses watchfiles (Rust-backed) for efficient filesystem monitoring.
Debounces changes and re-indexes only modified files.
Supports daemon mode with PID file for background operation, and an
in-process mode used by the MCP server (run_with_store).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from watchfiles import watch, Change

from . import config
from .storage.store import GraphStore
from .indexer.structure import StructureIndexer, EXTRACTORS, _file_ext


# Directories to ignore
IGNORE_DIRS = {
    ".git", ".graph-context", "node_modules", "__pycache__",
    ".venv", "venv", "dist", "build", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "egg-info",
}


def _should_watch(path: Path, repo: Path) -> bool:
    """Check if a file change should trigger reindexing."""
    # Must be a supported extension
    ext = _file_ext(str(path))
    if ext not in EXTRACTORS:
        return False
    # Must not be in an ignored directory
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return False
    for part in rel.parts:
        if part in IGNORE_DIRS or part.startswith("."):
            return False
    return True


def run_with_store(
    store: GraphStore,
    repo_path: str | Path,
    stop_event: threading.Event | None = None,
    quiet: bool = True,
) -> None:
    """Run the watcher loop using an externally-managed GraphStore.

    Used by the MCP server's in-process watcher thread: the MCP process
    already holds the LadybugDB lock, so a separate Database object would
    fail. Instead we share the existing store (and its write lock).

    Args:
        store: An already-open GraphStore. Caller owns its lifecycle.
        repo_path: Path to the repository root.
        stop_event: Optional threading.Event — when set, the loop exits
            cleanly. Required for in-process / threaded use.
        quiet: Suppress per-file output (only show summaries).
    """
    repo = Path(repo_path).resolve()
    indexer = StructureIndexer(store, repo)

    if not quiet:
        print(f"Watching {repo} for changes... (Ctrl+C to stop)")

    try:
        for changes in watch(
            repo,
            watch_filter=lambda change, path: _should_watch(Path(path), repo),
            debounce=1600,  # ms — batch rapid saves
            step=200,       # ms — poll interval
            stop_event=stop_event,
            raise_interrupt=False,
        ):
            # Collect unique changed file paths
            changed: set[str] = set()
            for change_type, path_str in changes:
                path = Path(path_str)
                try:
                    rel = str(path.relative_to(repo))
                except ValueError:
                    continue

                if change_type == Change.deleted:
                    store.clear_file(rel)
                    if not quiet:
                        print(f"  removed: {rel}")
                else:
                    changed.add(rel)

            if changed:
                stats = indexer.index_files(list(changed))
                if not quiet:
                    files = ", ".join(sorted(changed))
                    print(
                        f"  reindexed: {stats['files_indexed']} files "
                        f"({stats['nodes_created']} nodes, "
                        f"{stats['edges_created']} edges) "
                        f"— {files}"
                    )

    except KeyboardInterrupt:
        if not quiet:
            print("\nStopped watching.")


def run_watcher(repo_path: str, quiet: bool = False) -> None:
    """Watch a repo for file changes and auto-reindex (standalone mode).

    Opens its own GraphStore. Use this for the daemon path; for the
    in-process MCP watcher, call ``run_with_store`` with a shared store.

    Args:
        repo_path: Path to the repository root.
        quiet: Suppress per-file output (only show summaries).
    """
    repo = Path(repo_path).resolve()
    db_path = config.get_db_path(str(repo))

    store = GraphStore(db_path)
    store.open()
    store.ensure_schema()
    try:
        run_with_store(store, repo, stop_event=None, quiet=quiet)
    finally:
        store.close()


def _pid_file(repo_path: str) -> Path:
    """Get the PID file path for a repo's watcher daemon."""
    return Path(config.get_project_dir(repo_path)) / "watcher.pid"


def start_daemon(repo_path: str) -> int:
    """Fork the watcher into a background daemon process.

    Returns the child PID. Writes a PID file for later stop/status.
    """
    repo = Path(repo_path).resolve()
    pid_path = _pid_file(str(repo))
    log_path = Path(config.get_project_dir(str(repo))) / "watcher.log"

    # Check if already running
    if pid_path.exists():
        old_pid = int(pid_path.read_text().strip())
        try:
            os.kill(old_pid, 0)  # Check if process exists
            return old_pid  # Already running
        except OSError:
            pid_path.unlink()  # Stale PID file

    pid = os.fork()
    if pid > 0:
        # Parent — write PID and return
        pid_path.write_text(str(pid))
        return pid

    # Child — detach and run
    os.setsid()

    # Redirect stdout/stderr to log file
    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    # Redirect stdin from /dev/null
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    try:
        run_watcher(str(repo), quiet=False)
    finally:
        pid_path = _pid_file(str(repo))
        if pid_path.exists():
            pid_path.unlink()
    os._exit(0)


def stop_daemon(repo_path: str) -> bool:
    """Stop a running watcher daemon. Returns True if stopped."""
    pid_path = _pid_file(repo_path)
    if not pid_path.exists():
        return False

    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for clean shutdown
        for _ in range(10):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break  # Process gone
    except OSError:
        pass  # Already dead

    if pid_path.exists():
        pid_path.unlink()
    return True


def daemon_status(repo_path: str) -> dict | None:
    """Check watcher daemon status. Returns {pid, running} or None."""
    pid_path = _pid_file(repo_path)
    if not pid_path.exists():
        return None

    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, 0)
        return {"pid": pid, "running": True}
    except OSError:
        pid_path.unlink()
        return {"pid": pid, "running": False}
