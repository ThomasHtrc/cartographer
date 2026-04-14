"""Tests for the unified timeline view (past + future)."""

from __future__ import annotations

import pytest

from cartographer.storage.store import GraphStore
from cartographer.plans.manager import PlanManager
from cartographer.timeline import (
    resolve_target,
    get_timeline,
    format_markdown,
    format_json,
    render_html,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "tl_db"
    s = GraphStore(db_path)
    s.open()
    s.ensure_schema()  # all three layers
    yield s
    s.close()


def _seed_file(store: GraphStore, path: str, lang: str = "py") -> None:
    store.upsert_file(path, lang, "h", "2026-01-01T00:00:00")


def _seed_module(store: GraphStore, path: str) -> None:
    store.upsert_module(path, path.split("/")[-1] or path)


def _seed_function(
    store: GraphStore, fid: str, name: str, file_path: str,
    line_start: int = 1, line_end: int = 10,
) -> None:
    store.create_function(fid, name, file_path, line_start, line_end)


def _seed_class(
    store: GraphStore, cid: str, name: str, file_path: str,
    line_start: int = 1, line_end: int = 50,
) -> None:
    store.create_class(cid, name, file_path, line_start, line_end)


def _seed_commit(
    store: GraphStore, hash_: str, message: str, author: str,
    timestamp: str, files: list[tuple[str, int, int]],
) -> None:
    """Seed a Commit node + INCLUDES Change nodes + CHANGED_IN edges."""
    store.execute(
        "MERGE (c:Commit {hash: $h}) SET c.message = $m, c.author = $a, c.timestamp = $t",
        {"h": hash_, "m": message, "a": author, "t": timestamp},
    )
    for fp, adds, dels in files:
        ch_id = f"{hash_}::{fp}"
        store.execute(
            """MERGE (ch:Change {id: $id})
               SET ch.file_path = $fp, ch.additions = $adds,
                   ch.deletions = $dels, ch.change_type = 'M'""",
            {"id": ch_id, "fp": fp, "adds": adds, "dels": dels},
        )
        store.create_edge("INCLUDES", "Commit", hash_, "Change", ch_id)
        # CHANGED_IN edge if File node exists
        existing = store.query_one(
            "MATCH (f:File {path: $p}) RETURN f.path", {"p": fp}
        )
        if existing:
            store.create_edge("CHANGED_IN", "File", fp, "Commit", hash_)


def _affects_func(store: GraphStore, hash_: str, file_path: str, fid: str) -> None:
    ch_id = f"{hash_}::{file_path}"
    store.create_edge("AFFECTS_FUNC", "Change", ch_id, "Function", fid)


def _affects_class(store: GraphStore, hash_: str, file_path: str, cid: str) -> None:
    ch_id = f"{hash_}::{file_path}"
    store.create_edge("AFFECTS_CLASS", "Change", ch_id, "Class", cid)


# ---------------------------------------------------------------------------
# resolve_target
# ---------------------------------------------------------------------------

class TestResolveTarget:
    def test_exact_file(self, store):
        _seed_file(store, "src/auth/login.py")
        r = resolve_target(store, "src/auth/login.py")
        assert r == {"kind": "file", "name": "src/auth/login.py", "path": "src/auth/login.py"}

    def test_exact_module(self, store):
        _seed_module(store, "src/auth")
        r = resolve_target(store, "src/auth")
        assert r["kind"] == "module"
        assert r["path"] == "src/auth"

    def test_suffix_file(self, store):
        _seed_file(store, "src/auth/login.py")
        r = resolve_target(store, "auth/login.py")
        assert r["kind"] == "file"
        assert r["path"] == "src/auth/login.py"

    def test_function_by_name(self, store):
        _seed_file(store, "src/svc.py")
        _seed_function(store, "src/svc.py::process", "process", "src/svc.py")
        r = resolve_target(store, "process")
        assert r["kind"] == "function"
        assert r["name"] == "process"
        assert r["path"] == "src/svc.py"

    def test_class_by_name(self, store):
        _seed_file(store, "src/svc.py")
        _seed_class(store, "src/svc.py::Service", "Service", "src/svc.py")
        r = resolve_target(store, "Service")
        assert r["kind"] == "class"
        assert r["name"] == "Service"

    def test_unknown(self, store):
        assert resolve_target(store, "nope.py") is None
        assert resolve_target(store, "") is None


# ---------------------------------------------------------------------------
# Past events
# ---------------------------------------------------------------------------

class TestPast:
    def test_file_past_only(self, store):
        _seed_file(store, "src/a.py")
        _seed_commit(store, "h1", "first", "alice", "2026-01-01", [("src/a.py", 5, 1)])
        _seed_commit(store, "h2", "second", "bob",   "2026-02-01", [("src/a.py", 8, 2)])

        data = get_timeline(store, "src/a.py", include_neighbors=False)
        assert data["target"]["kind"] == "file"
        assert len(data["past"]) == 2
        # Newest first
        assert data["past"][0]["hash"] == "h2"
        assert data["past"][0]["additions"] == 8
        assert data["past"][1]["hash"] == "h1"
        assert data["future"] == []

    def test_module_aggregates_files(self, store):
        _seed_module(store, "src/auth")
        _seed_file(store, "src/auth/login.py")
        _seed_file(store, "src/auth/session.py")
        _seed_commit(
            store, "h1", "auth refactor", "alice", "2026-03-01",
            [("src/auth/login.py", 10, 2), ("src/auth/session.py", 4, 1)],
        )
        _seed_commit(
            store, "h2", "fix login", "alice", "2026-03-02",
            [("src/auth/login.py", 3, 1)],
        )

        data = get_timeline(store, "src/auth", include_neighbors=False)
        assert data["target"]["kind"] == "module"
        assert len(data["past"]) == 2
        # h1 should aggregate both files in module
        h1_entry = next(c for c in data["past"] if c["hash"] == "h1")
        assert sorted(h1_entry["files"]) == ["src/auth/login.py", "src/auth/session.py"]
        assert h1_entry["additions"] == 14
        assert h1_entry["deletions"] == 3

    def test_symbol_past_via_affects_edges(self, store):
        _seed_file(store, "src/svc.py")
        _seed_function(store, "src/svc.py::process", "process", "src/svc.py")
        _seed_commit(store, "h1", "tweak process", "alice", "2026-01-10", [("src/svc.py", 4, 0)])
        _affects_func(store, "h1", "src/svc.py", "src/svc.py::process")

        data = get_timeline(store, "process", include_neighbors=False)
        assert data["target"]["kind"] == "function"
        assert len(data["past"]) == 1
        assert data["past"][0]["hash"] == "h1"
        assert data["target"]["fallback"] is None

    def test_symbol_past_falls_back_to_file(self, store):
        # Function exists, no AFFECTS edges, but file has commits
        _seed_file(store, "src/svc.py")
        _seed_function(store, "src/svc.py::process", "process", "src/svc.py")
        _seed_commit(store, "h1", "edit svc", "alice", "2026-01-10", [("src/svc.py", 4, 0)])

        data = get_timeline(store, "process", include_neighbors=False)
        assert data["target"]["kind"] == "function"
        assert data["target"]["fallback"] == "file"
        assert len(data["past"]) == 1
        assert data["past"][0]["hash"] == "h1"


# ---------------------------------------------------------------------------
# Future events
# ---------------------------------------------------------------------------

class TestFuture:
    def test_file_future_only(self, store):
        _seed_file(store, "src/a.py")
        mgr = PlanManager(store)
        pid = mgr.create_plan(
            title="Refactor a.py", description="cleanup",
            status="active", targets=["src/a.py"],
        )
        mgr.create_intent(pid, description="step 1", status="completed")
        mgr.create_intent(pid, description="step 2", status="draft")

        data = get_timeline(store, "src/a.py", include_neighbors=False)
        assert data["past"] == []
        assert len(data["future"]) == 1
        plan = data["future"][0]
        assert plan["title"] == "Refactor a.py"
        assert plan["progress"]["pct"] == 50
        assert plan["progress"]["completed"] == 1
        assert plan["progress"]["total"] == 2
        # The next intent should be the draft one
        assert plan["next_intent"]["description"] == "step 2"

    def test_module_includes_file_targeted_plans(self, store):
        _seed_module(store, "src/auth")
        _seed_file(store, "src/auth/login.py")
        mgr = PlanManager(store)
        pid = mgr.create_plan(
            title="Auth work", status="active", targets=["src/auth/login.py"],
        )
        mgr.create_intent(pid, description="do thing", status="draft")

        data = get_timeline(store, "src/auth", include_neighbors=False)
        assert len(data["future"]) == 1
        assert data["future"][0]["id"] == pid

    def test_symbol_future(self, store):
        _seed_file(store, "src/svc.py")
        _seed_class(store, "src/svc.py::Service", "Service", "src/svc.py")
        mgr = PlanManager(store)
        pid = mgr.create_plan(
            title="Service rewrite", status="active", targets=["Service"],
        )
        mgr.create_intent(pid, description="rewrite", status="draft")

        data = get_timeline(store, "Service", include_neighbors=False)
        assert len(data["future"]) == 1
        assert data["future"][0]["title"] == "Service rewrite"

    def test_inactive_plans_excluded(self, store):
        _seed_file(store, "src/a.py")
        mgr = PlanManager(store)
        mgr.create_plan(
            title="done", status="completed", targets=["src/a.py"],
        )
        mgr.create_plan(
            title="abandoned", status="abandoned", targets=["src/a.py"],
        )

        data = get_timeline(store, "src/a.py", include_neighbors=False)
        assert data["future"] == []


# ---------------------------------------------------------------------------
# Neighbors
# ---------------------------------------------------------------------------

class TestNeighbors:
    def test_co_change_neighbors_for_file(self, store):
        _seed_file(store, "src/a.py")
        _seed_file(store, "src/b.py")
        _seed_file(store, "src/c.py")
        store.create_edge(
            "CO_CHANGES_WITH", "File", "src/a.py", "File", "src/b.py",
            props={"count": 12, "correlation": 0.0},
        )
        store.create_edge(
            "CO_CHANGES_WITH", "File", "src/a.py", "File", "src/c.py",
            props={"count": 3, "correlation": 0.0},
        )

        data = get_timeline(store, "src/a.py", include_neighbors=True)
        assert len(data["co_changes"]) == 2
        assert data["co_changes"][0]["file"] == "src/b.py"
        assert data["co_changes"][0]["count"] == 12
        assert data["co_changes"][1]["file"] == "src/c.py"

    def test_no_neighbors_when_disabled(self, store):
        _seed_file(store, "src/a.py")
        _seed_file(store, "src/b.py")
        store.create_edge(
            "CO_CHANGES_WITH", "File", "src/a.py", "File", "src/b.py",
            props={"count": 9, "correlation": 0.0},
        )
        data = get_timeline(store, "src/a.py", include_neighbors=False)
        assert data["co_changes"] == []
        assert data["callers"] == []


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_markdown_renders_progress_bar(self, store):
        _seed_file(store, "src/a.py")
        mgr = PlanManager(store)
        pid = mgr.create_plan(
            title="Refactor", status="active", targets=["src/a.py"],
        )
        mgr.create_intent(pid, description="step1", status="completed")
        mgr.create_intent(pid, description="step2", status="draft")
        mgr.create_intent(pid, description="step3", status="draft")

        data = get_timeline(store, "src/a.py", include_neighbors=False)
        md = format_markdown(data)
        assert "# Timeline: src/a.py" in md
        assert "Refactor" in md
        assert "1/3" in md
        assert "33%" in md
        # progress bar uses block elements
        assert "▓" in md or "░" in md
        assert "[x] step1" in md
        assert "[ ] step2 ← next" in md

    def test_json_round_trip(self, store):
        _seed_file(store, "src/a.py")
        _seed_commit(store, "h1", "first", "alice", "2026-01-01", [("src/a.py", 1, 0)])
        data = get_timeline(store, "src/a.py", include_neighbors=False)
        s = format_json(data)
        import json
        parsed = json.loads(s)
        assert parsed["target"]["kind"] == "file"
        assert len(parsed["past"]) == 1

    def test_markdown_empty_target(self, store):
        data = get_timeline(store, "ghost.py", include_neighbors=False)
        md = format_markdown(data)
        assert "not found" in md.lower()

    def test_render_html_self_contained(self, store):
        _seed_file(store, "src/a.py")
        _seed_commit(store, "h1", "fix things", "alice", "2026-04-01", [("src/a.py", 5, 1)])
        mgr = PlanManager(store)
        pid = mgr.create_plan(
            title="Roadmap", status="active", targets=["src/a.py"],
        )
        mgr.create_intent(pid, description="phase 1", status="draft")

        data = get_timeline(store, "src/a.py", include_neighbors=False)
        html = render_html(data)
        # Self-contained: no external scripts/styles
        assert "<script src=" not in html
        assert "http://" not in html
        assert "https://" not in html
        # Has embedded CSS
        assert "<style>" in html
        # Mentions target, plan title, and commit message
        assert "src/a.py" in html
        assert "Roadmap" in html
        assert "fix things" in html
        # Now divider
        assert "now" in html.lower()
        # Document scaffolding
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")
