"""Tests for MCP server tool handlers.

Tests the tool functions directly (not via MCP protocol) to verify
they produce correct output from a real graph.
"""

import json
import os
from unittest import mock

import pytest

from cartographer.storage.store import GraphStore
from cartographer import config


@pytest.fixture()
def project_dir(tmp_path):
    """Set up a fake project with an indexed graph."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize graph-context
    gc_dir = repo / ".graph-context"
    gc_dir.mkdir()
    db_path = gc_dir / "db"

    store = GraphStore(db_path)
    store.open()
    store.ensure_schema()

    # Populate with test data
    store.upsert_file("src/auth.py", "python", "aaa", "2024-01-01")
    store.upsert_file("src/models.py", "python", "bbb", "2024-01-01")
    store.upsert_module("src", "src")

    store.create_function(
        "src/auth.py::login", "login", "src/auth.py", 1, 10,
        signature="def login(username: str, password: str) -> User:"
    )
    store.create_function(
        "src/auth.py::validate", "validate", "src/auth.py", 12, 20,
        signature="def validate(token: str) -> bool:"
    )
    store.create_class("src/models.py::User", "User", "src/models.py", 1, 30)

    store.create_edge("CONTAINS_FUNC", "File", "src/auth.py", "Function", "src/auth.py::login")
    store.create_edge("CONTAINS_FUNC", "File", "src/auth.py", "Function", "src/auth.py::validate")
    store.create_edge("CONTAINS_CLASS", "File", "src/models.py", "Class", "src/models.py::User")
    store.create_edge("CALLS", "Function", "src/auth.py::login", "Function", "src/auth.py::validate")
    store.create_edge("IMPORTS", "File", "src/auth.py", "File", "src/models.py")
    store.create_edge("BELONGS_TO", "File", "src/auth.py", "Module", "src")
    store.create_edge("BELONGS_TO", "File", "src/models.py", "Module", "src")

    store.close()

    config.save_meta(str(repo), {"initialized": True, "last_commit": None})

    return str(repo)


@pytest.fixture()
def _patch_repo(project_dir):
    """Patch the MCP server to use our test repo."""
    from cartographer import mcp_server

    def _reset():
        mcp_server._shutdown_watchers()
        for store in list(mcp_server._store_cache.values()):
            try:
                store.close()
            except Exception:
                pass
        mcp_server._store_cache.clear()

    _reset()
    # Disable the auto-watcher for these tool tests — they don't need it
    # and the spawned threads keep Database mmap allocations alive between
    # tests, exhausting address space when many tests run in one process.
    with mock.patch.dict(
        os.environ,
        {"GRAPH_CONTEXT_REPO": project_dir, "GRAPH_CONTEXT_MCP_AUTOWATCH": "0"},
    ):
        yield
    _reset()


# ---------------------------------------------------------------------------
# Context tools
# ---------------------------------------------------------------------------

class TestContextTools:
    def test_context_markdown(self, _patch_repo):
        from cartographer.mcp_server import context
        result = context(focus=["src/auth.py"], budget=2000)
        assert "src/auth.py" in result
        assert "login" in result

    def test_context_json(self, _patch_repo):
        from cartographer.mcp_server import context
        result = context(focus=["src/auth.py"], budget=2000, format="json")
        data = json.loads(result)
        assert data["focal_points"] == ["src/auth.py"]
        assert data["files_included"] >= 1

    def test_context_no_results(self, _patch_repo):
        from cartographer.mcp_server import context
        result = context(focus=["nonexistent.py"])
        assert "no relevant" in result

    def test_repo_map(self, _patch_repo):
        from cartographer.mcp_server import repo_map
        result = repo_map(budget=4000)
        assert "src/auth.py" in result or "src/models.py" in result

    def test_repo_map_focused(self, _patch_repo):
        from cartographer.mcp_server import repo_map
        result = repo_map(focus=["src/auth.py"], budget=4000)
        assert "login" in result


# ---------------------------------------------------------------------------
# Navigation tools
# ---------------------------------------------------------------------------

class TestNavigationTools:
    def test_find_definition(self, _patch_repo):
        from cartographer.mcp_server import find_definition
        result = find_definition("login")
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["kind"] == "function"
        assert data[0]["file"] == "src/auth.py"

    def test_find_definition_class(self, _patch_repo):
        from cartographer.mcp_server import find_definition
        result = find_definition("User")
        data = json.loads(result)
        assert any(d["kind"] == "class" for d in data)

    def test_find_definition_not_found(self, _patch_repo):
        from cartographer.mcp_server import find_definition
        result = find_definition("nonexistent")
        assert "No definition" in result

    def test_find_callers(self, _patch_repo):
        from cartographer.mcp_server import find_callers
        result = find_callers("validate")
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["caller"] == "login"

    def test_find_callees(self, _patch_repo):
        from cartographer.mcp_server import find_callees
        result = find_callees("login")
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["callee"] == "validate"

    def test_module_structure(self, _patch_repo):
        from cartographer.mcp_server import module_structure
        result = module_structure("src")
        data = json.loads(result)
        assert len(data) >= 1
        files = {d["file"] for d in data}
        assert "src/auth.py" in files


# ---------------------------------------------------------------------------
# Plan tools
# ---------------------------------------------------------------------------

class TestPlanTools:
    def test_plan_crud(self, _patch_repo):
        from cartographer.mcp_server import plan_create, plan_list, plan_show, plan_update

        # Create
        result = plan_create(title="Auth rewrite", description="Modernize auth")
        data = json.loads(result)
        plan_id = data["id"]
        assert data["title"] == "Auth rewrite"

        # List
        result = plan_list()
        plans = json.loads(result)
        assert len(plans) == 1

        # Show
        result = plan_show(plan_id)
        plan = json.loads(result)
        assert plan["title"] == "Auth rewrite"
        assert plan["description"] == "Modernize auth"

        # Update
        result = plan_update(plan_id, status="active")
        assert "Updated" in result

        # Verify update
        result = plan_show(plan_id)
        plan = json.loads(result)
        assert plan["status"] == "active"

    def test_plan_add_intent(self, _patch_repo):
        from cartographer.mcp_server import plan_create, plan_add_intent, plan_show

        result = plan_create(title="Test plan")
        plan_id = json.loads(result)["id"]

        result = plan_add_intent(plan_id, description="Add caching", rationale="Speed")
        data = json.loads(result)
        assert data["plan_id"] == plan_id

        result = plan_show(plan_id)
        plan = json.loads(result)
        assert len(plan["intents"]) == 1


# ---------------------------------------------------------------------------
# Utility tools
# ---------------------------------------------------------------------------

class TestUtilityTools:
    def test_graph_stats(self, _patch_repo):
        from cartographer.mcp_server import graph_stats
        result = graph_stats()
        data = json.loads(result)
        assert data["File"] == 2
        assert data["Function"] == 2
        assert data["Class"] == 1

    def test_run_cypher(self, _patch_repo):
        from cartographer.mcp_server import run_cypher
        result = run_cypher("MATCH (f:File) RETURN f.path ORDER BY f.path")
        data = json.loads(result)
        assert len(data) == 2
        assert data[0][0] == "src/auth.py"
