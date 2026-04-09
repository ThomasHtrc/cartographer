"""Tests for the in-process watcher thread spawned by the MCP server."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from graph_context import config, mcp_server
from graph_context.storage.store import GraphStore


@pytest.fixture()
def isolated_mcp_state(monkeypatch, tmp_path):
    """Reset the MCP server's module-level caches around each test.

    The MCP server keeps a per-repo store cache and a per-repo watcher
    thread registry; tests must not leak state between each other.
    """
    # Save and clear
    saved_stores = dict(mcp_server._store_cache)
    saved_watchers = dict(mcp_server._watcher_threads)
    mcp_server._store_cache.clear()
    mcp_server._watcher_threads.clear()

    # Point _repo_path() at the tmp dir and pre-create the .graph-context tree
    repo = tmp_path
    config.get_db_path(str(repo)).parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GRAPH_CONTEXT_REPO", str(repo))

    yield repo

    # Cleanup: stop any spawned watchers and close stores
    mcp_server._shutdown_watchers()
    for store in mcp_server._store_cache.values():
        store.close()
    mcp_server._store_cache.clear()
    mcp_server._store_cache.update(saved_stores)
    mcp_server._watcher_threads.update(saved_watchers)


class TestWatcherSpawn:
    def test_open_store_spawns_watcher(self, isolated_mcp_state, monkeypatch):
        monkeypatch.setenv("GRAPH_CONTEXT_MCP_AUTOWATCH", "1")
        repo = str(isolated_mcp_state)

        store = mcp_server._open_store()
        assert isinstance(store, GraphStore)

        assert repo in mcp_server._watcher_threads
        thread, stop_event = mcp_server._watcher_threads[repo]
        assert thread.is_alive()

        # Calling _open_store() again must not spawn a second watcher
        store2 = mcp_server._open_store()
        assert store2 is store
        assert len(mcp_server._watcher_threads) == 1

    def test_autowatch_disabled_via_env(self, isolated_mcp_state, monkeypatch):
        monkeypatch.setenv("GRAPH_CONTEXT_MCP_AUTOWATCH", "0")

        mcp_server._open_store()
        assert mcp_server._watcher_threads == {}

    def test_shutdown_watchers_stops_threads(self, isolated_mcp_state, monkeypatch):
        monkeypatch.setenv("GRAPH_CONTEXT_MCP_AUTOWATCH", "1")
        mcp_server._open_store()
        repo = str(isolated_mcp_state)
        thread, stop_event = mcp_server._watcher_threads[repo]

        mcp_server._shutdown_watchers()

        # Give the thread up to 2s to notice the stop event
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert mcp_server._watcher_threads == {}


class TestWatcherIndexesChanges:
    def test_watcher_picks_up_new_python_file(self, isolated_mcp_state, monkeypatch):
        """End-to-end: spawn the watcher, drop a .py file in the repo, wait
        for the debounce window to fire, assert the file shows up indexed."""
        monkeypatch.setenv("GRAPH_CONTEXT_MCP_AUTOWATCH", "1")
        repo = isolated_mcp_state
        store = mcp_server._open_store()

        # Wait briefly for the watcher to start its filesystem listener
        time.sleep(0.5)

        # Drop a Python file with a single function
        new_file = repo / "new_module.py"
        new_file.write_text("def hello():\n    return 1\n")

        # Wait for debounce (1.6s) plus indexing time
        deadline = time.time() + 8.0
        while time.time() < deadline:
            rows = store.query(
                "MATCH (f:File {path: $p}) RETURN f.path",
                {"p": "new_module.py"},
            )
            if rows:
                break
            time.sleep(0.2)

        rows = store.query(
            "MATCH (f:File {path: $p}) RETURN f.path",
            {"p": "new_module.py"},
        )
        assert rows, "watcher did not index the new file within timeout"

        # The Function should also be present
        fn_rows = store.query(
            "MATCH (n:Function) WHERE n.file_path = $p RETURN n.name",
            {"p": "new_module.py"},
        )
        assert any(r[0] == "hello" for r in fn_rows)
