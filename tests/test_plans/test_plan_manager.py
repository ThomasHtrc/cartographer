"""Tests for PlanManager (Layer 3)."""

import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from cartographer.storage.store import GraphStore
from cartographer.plans.manager import PlanManager


@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "test_db"
    s = GraphStore(db_path)
    s.open()
    s.ensure_schema(layers=("structure", "planning"))
    yield s
    s.close()


@pytest.fixture()
def mgr(store):
    return PlanManager(store)


class TestPlanCRUD:
    def test_create_and_get_plan(self, mgr):
        pid = mgr.create_plan(title="Auth rewrite", description="Rewrite auth module", author="alice")
        assert pid
        plan = mgr.get_plan(pid)
        assert plan is not None
        assert plan["title"] == "Auth rewrite"
        assert plan["description"] == "Rewrite auth module"
        assert plan["status"] == "draft"
        assert plan["author"] == "alice"

    def test_update_plan(self, mgr):
        pid = mgr.create_plan(title="Original")
        ok = mgr.update_plan(pid, title="Updated", status="active")
        assert ok is True
        plan = mgr.get_plan(pid)
        assert plan["title"] == "Updated"
        assert plan["status"] == "active"

    def test_update_nonexistent_plan(self, mgr):
        assert mgr.update_plan("nonexistent") is False

    def test_list_plans(self, mgr):
        mgr.create_plan(title="Plan A", status="draft")
        mgr.create_plan(title="Plan B", status="active")
        mgr.create_plan(title="Plan C", status="draft")

        all_plans = mgr.list_plans()
        assert len(all_plans) == 3

        drafts = mgr.list_plans(status="draft")
        assert len(drafts) == 2

        active = mgr.list_plans(status="active")
        assert len(active) == 1
        assert active[0]["title"] == "Plan B"

    def test_delete_plan(self, mgr):
        pid = mgr.create_plan(title="To delete")
        assert mgr.delete_plan(pid) is True
        assert mgr.get_plan(pid) is None
        assert mgr.delete_plan(pid) is False


class TestIntentCRUD:
    def test_create_intent(self, mgr):
        pid = mgr.create_plan(title="Parent plan")
        iid = mgr.create_intent(pid, description="Add caching", rationale="Speed up reads")
        assert iid

        plan = mgr.get_plan(pid)
        assert len(plan["intents"]) == 1
        assert plan["intents"][0]["description"] == "Add caching"
        assert plan["intents"][0]["rationale"] == "Speed up reads"

    def test_update_intent(self, mgr):
        pid = mgr.create_plan(title="Plan")
        iid = mgr.create_intent(pid, description="Original intent")
        ok = mgr.update_intent(iid, status="done", description="Updated intent")
        assert ok is True

        plan = mgr.get_plan(pid)
        assert plan["intents"][0]["status"] == "done"
        assert plan["intents"][0]["description"] == "Updated intent"

    def test_update_nonexistent_intent(self, mgr):
        assert mgr.update_intent("nonexistent") is False


class TestPlanDependencies:
    def test_depends_on_plan(self, mgr):
        dep_id = mgr.create_plan(title="Dependency")
        pid = mgr.create_plan(title="Dependent", depends_on=[dep_id])

        plan = mgr.get_plan(pid)
        assert len(plan["depends_on"]) == 1
        assert plan["depends_on"][0]["id"] == dep_id
        assert plan["depends_on"][0]["title"] == "Dependency"


class TestTargetLinking:
    def test_link_file_target(self, store, mgr):
        store.upsert_file("src/auth.py", "python", "abc123", "2024-01-01")
        pid = mgr.create_plan(title="Auth plan", targets=["src/auth.py"])

        plan = mgr.get_plan(pid)
        assert len(plan["targets"]) == 1
        assert plan["targets"][0]["kind"] == "File"
        assert plan["targets"][0]["id"] == "src/auth.py"

    def test_link_symbol_target(self, store, mgr):
        store.upsert_file("src/auth.py", "python", "abc123", "2024-01-01")
        store.create_class("src/auth.py::AuthService", "AuthService", "src/auth.py", 1, 50)
        pid = mgr.create_plan(title="Refactor plan", targets=["AuthService"])

        plan = mgr.get_plan(pid)
        assert len(plan["targets"]) == 1
        assert plan["targets"][0]["kind"] == "Class"

    def test_link_qualified_target(self, store, mgr):
        store.upsert_file("src/auth.py", "python", "abc123", "2024-01-01")
        store.create_function("src/auth.py::login", "login", "src/auth.py", 1, 10)
        pid = mgr.create_plan(title="Fix login", targets=["src/auth.py::login"])

        plan = mgr.get_plan(pid)
        assert len(plan["targets"]) == 1
        assert plan["targets"][0]["kind"] == "Function"

    def test_link_targets_post_creation(self, store, mgr):
        store.upsert_file("src/auth.py", "python", "abc123", "2024-01-01")
        pid = mgr.create_plan(title="Plan")
        count = mgr.link_targets(pid, ["src/auth.py"])
        assert count == 1


class TestYAMLIngestion:
    def test_ingest_plan_file(self, store, mgr, tmp_path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()

        plan_yaml = {
            "title": "YAML Plan",
            "description": "Loaded from YAML",
            "status": "active",
            "author": "bob",
            "intents": [
                {"description": "Step 1", "rationale": "Because", "status": "draft"},
            ],
        }
        (plans_dir / "my-plan.yaml").write_text(yaml.dump(plan_yaml))

        stats = mgr.ingest_plans_dir(plans_dir)
        assert stats["created"] == 1
        assert stats["skipped"] == 0

        plans = mgr.list_plans()
        assert len(plans) == 1
        assert plans[0]["title"] == "YAML Plan"

        plan = mgr.get_plan(plans[0]["id"])
        assert len(plan["intents"]) == 1

    def test_ingest_idempotent(self, store, mgr, tmp_path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "rerun.yaml").write_text(yaml.dump({"title": "Rerun"}))

        stats1 = mgr.ingest_plans_dir(plans_dir)
        assert stats1["created"] == 1

        stats2 = mgr.ingest_plans_dir(plans_dir)
        assert stats2["updated"] == 1
        assert stats2["created"] == 0

    def test_ingest_skips_invalid(self, store, mgr, tmp_path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "bad.yaml").write_text("not a dict")

        stats = mgr.ingest_plans_dir(plans_dir)
        assert stats["skipped"] == 1

    def test_ingest_nonexistent_dir(self, mgr):
        stats = mgr.ingest_plans_dir("/nonexistent/path")
        assert stats == {"created": 0, "updated": 0, "skipped": 0}
