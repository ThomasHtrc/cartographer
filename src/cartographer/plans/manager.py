"""Plan manager: CRUD for Plan and Intent nodes (Layer 3)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..storage.store import GraphStore


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlanManager:
    """Manages Plan and Intent nodes in the graph."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    # -- Plan CRUD ------------------------------------------------------------

    def create_plan(
        self,
        title: str,
        description: str = "",
        status: str = "draft",
        author: str = "",
        targets: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> str:
        """Create a Plan node and link it to targets. Returns the plan id."""
        plan_id = _new_id()
        now = _now()

        self.store.execute_write(
            """CREATE (p:Plan {
                id: $id, title: $title, description: $descr,
                status: $status, created_at: $now, updated_at: $now, author: $author
            })""",
            {"id": plan_id, "title": title, "descr": description,
             "status": status, "now": now, "author": author},
        )

        if targets:
            self._link_targets(plan_id, targets)

        if depends_on:
            for dep_id in depends_on:
                try:
                    self.store.create_edge("DEPENDS_ON_PLAN", "Plan", plan_id, "Plan", dep_id)
                except Exception:
                    pass

        return plan_id

    def update_plan(
        self,
        plan_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> bool:
        """Update a Plan node's properties. Returns True if found."""
        existing = self.store.query_one(
            "MATCH (p:Plan {id: $id}) RETURN p.id", {"id": plan_id}
        )
        if not existing:
            return False

        sets: list[str] = ["p.updated_at = $now"]
        params: dict[str, Any] = {"id": plan_id, "now": _now()}

        if title is not None:
            sets.append("p.title = $title")
            params["title"] = title
        if description is not None:
            sets.append("p.description = $descr")
            params["descr"] = description
        if status is not None:
            sets.append("p.status = $status")
            params["status"] = status

        self.store.execute_write(
            f"MATCH (p:Plan {{id: $id}}) SET {', '.join(sets)}",
            params,
        )
        return True

    def get_plan(self, plan_id: str) -> dict | None:
        """Get a plan by id, including its targets and dependencies."""
        row = self.store.query_one(
            """MATCH (p:Plan {id: $id})
            RETURN p.id, p.title, p.description, p.status,
                   p.created_at, p.updated_at, p.author""",
            {"id": plan_id},
        )
        if not row:
            return None

        plan = {
            "id": row[0], "title": row[1], "description": row[2],
            "status": row[3], "created_at": row[4], "updated_at": row[5],
            "author": row[6],
        }

        # Gather targets
        targets = []
        for target_type, rel_type in [
            ("File", "TARGETS_FILE"), ("Module", "TARGETS_MODULE"),
            ("Class", "TARGETS_CLASS"), ("Function", "TARGETS_FUNC"),
        ]:
            pk = "path" if target_type in ("File", "Module") else "id"
            name_field = "path" if target_type == "File" else "name"
            rows = self.store.query(
                f"MATCH (p:Plan {{id: $id}})-[:{rel_type}]->(t:{target_type}) "
                f"RETURN '{target_type}' AS kind, t.{pk} AS target_id, t.{name_field} AS name",
                {"id": plan_id},
            )
            for r in rows:
                targets.append({"kind": r[0], "id": r[1], "name": r[2]})
        plan["targets"] = targets

        # Gather dependencies
        deps = self.store.query(
            "MATCH (p:Plan {id: $id})-[:DEPENDS_ON_PLAN]->(dep:Plan) "
            "RETURN dep.id, dep.title, dep.status",
            {"id": plan_id},
        )
        plan["depends_on"] = [{"id": d[0], "title": d[1], "status": d[2]} for d in deps]

        # Gather intents
        intents = self.store.query(
            "MATCH (i:Intent)-[:IMPLEMENTS]->(p:Plan {id: $id}) "
            "RETURN i.id, i.description, i.rationale, i.status",
            {"id": plan_id},
        )
        plan["intents"] = [
            {"id": i[0], "description": i[1], "rationale": i[2], "status": i[3]}
            for i in intents
        ]

        # Progress
        plan["progress"] = self.get_plan_progress(plan_id)

        # Blocking: plans that depend on this one
        blocking = self.store.query(
            "MATCH (blocker:Plan)-[:DEPENDS_ON_PLAN]->(p:Plan {id: $id}) "
            "RETURN blocker.id, blocker.title, blocker.status",
            {"id": plan_id},
        )
        plan["blocking"] = [{"id": b[0], "title": b[1], "status": b[2]} for b in blocking]

        # Blocked by incomplete dependencies
        plan["blocked"] = any(d["status"] != "completed" for d in plan["depends_on"])

        # Next intent: first incomplete intent
        next_intent = self.store.query_one(
            "MATCH (i:Intent)-[:IMPLEMENTS]->(p:Plan {id: $id}) "
            "WHERE i.status IN ['draft', 'in_progress', 'active'] "
            "RETURN i.id, i.description, i.status",
            {"id": plan_id},
        )
        plan["next_intent"] = (
            {"id": next_intent[0], "description": next_intent[1], "status": next_intent[2]}
            if next_intent else None
        )

        return plan

    def list_plans(self, status: str | None = None) -> list[dict]:
        """List all plans, optionally filtered by status, with progress."""
        if status:
            rows = self.store.query(
                """MATCH (p:Plan)
                WHERE p.status = $status
                OPTIONAL MATCH (i:Intent)-[:IMPLEMENTS]->(p)
                WITH p, count(i) AS total_intents,
                     count(CASE WHEN i.status = 'completed' THEN 1 END) AS done_intents
                RETURN p.id, p.title, p.status, p.updated_at, p.author,
                       total_intents, done_intents
                ORDER BY p.updated_at DESC""",
                {"status": status},
            )
        else:
            rows = self.store.query(
                """MATCH (p:Plan)
                OPTIONAL MATCH (i:Intent)-[:IMPLEMENTS]->(p)
                WITH p, count(i) AS total_intents,
                     count(CASE WHEN i.status = 'completed' THEN 1 END) AS done_intents
                RETURN p.id, p.title, p.status, p.updated_at, p.author,
                       total_intents, done_intents
                ORDER BY p.updated_at DESC""",
            )
        return [
            {
                "id": r[0], "title": r[1], "status": r[2],
                "updated_at": r[3], "author": r[4],
                "progress": f"{r[6]}/{r[5]}" if r[5] > 0 else "no intents",
            }
            for r in rows
        ]

    def get_plan_progress(self, plan_id: str) -> dict:
        """Calculate completion percentage from intent statuses."""
        rows = self.store.query(
            "MATCH (i:Intent)-[:IMPLEMENTS]->(p:Plan {id: $id}) "
            "RETURN i.status, count(i)",
            {"id": plan_id},
        )
        total = sum(r[1] for r in rows)
        if total == 0:
            return {"total": 0, "completed": 0, "in_progress": 0, "draft": 0, "pct": 0}
        status_counts = {r[0]: r[1] for r in rows}
        completed = status_counts.get("completed", 0)
        return {
            "total": total,
            "completed": completed,
            "in_progress": status_counts.get("in_progress", 0) + status_counts.get("active", 0),
            "draft": status_counts.get("draft", 0),
            "pct": round(100 * completed / total),
        }

    def delete_plan(self, plan_id: str) -> bool:
        """Delete a plan and its edges."""
        existing = self.store.query_one(
            "MATCH (p:Plan {id: $id}) RETURN p.id", {"id": plan_id}
        )
        if not existing:
            return False
        self.store.execute_write("MATCH (p:Plan {id: $id}) DETACH DELETE p", {"id": plan_id})
        return True

    # -- Intent CRUD ----------------------------------------------------------

    def create_intent(
        self,
        plan_id: str,
        description: str,
        rationale: str = "",
        status: str = "draft",
        affected_files: list[str] | None = None,
    ) -> str:
        """Create an Intent node linked to a Plan. Returns intent id.

        If affected_files is provided, auto-links them as plan targets.
        """
        intent_id = _new_id()
        self.store.execute_write(
            """CREATE (i:Intent {
                id: $id, description: $descr, rationale: $rat, status: $status
            })""",
            {"id": intent_id, "descr": description, "rat": rationale, "status": status},
        )
        self.store.create_edge("IMPLEMENTS", "Intent", intent_id, "Plan", plan_id)

        if affected_files:
            self._link_targets(plan_id, affected_files)

        return intent_id

    def update_intent(self, intent_id: str, status: str | None = None, description: str | None = None) -> bool:
        existing = self.store.query_one("MATCH (i:Intent {id: $id}) RETURN i.id", {"id": intent_id})
        if not existing:
            return False
        sets: list[str] = []
        params: dict[str, Any] = {"id": intent_id}
        if status is not None:
            sets.append("i.status = $status")
            params["status"] = status
        if description is not None:
            sets.append("i.description = $descr")
            params["descr"] = description
        if sets:
            self.store.execute_write(f"MATCH (i:Intent {{id: $id}}) SET {', '.join(sets)}", params)
        return True

    # -- Target linking -------------------------------------------------------

    def link_targets(self, plan_id: str, targets: list[str]) -> int:
        """Add TARGETS edges from a plan to structural entities. Returns count linked."""
        return self._link_targets(plan_id, targets)

    def _link_targets(self, plan_id: str, targets: list[str]) -> int:
        """Resolve target strings and create TARGETS edges.

        Target strings can be:
          - A file path: "src/auth/login.ts"
          - A module path: "src/auth"
          - A symbol name: "OrderService" (resolved to Class/Function)
          - A qualified id: "src/auth/login.ts::OrderService"
        """
        linked = 0
        for target in targets:
            if "::" in target:
                # Qualified id — try Function, Class, Type, Variable
                for table, rel in [
                    ("Function", "TARGETS_FUNC"), ("Class", "TARGETS_CLASS"),
                ]:
                    found = self.store.query_one(
                        f"MATCH (t:{table} {{id: $tid}}) RETURN t.id", {"tid": target}
                    )
                    if found:
                        try:
                            self.store.create_edge(rel, "Plan", plan_id, table, target)
                            linked += 1
                        except Exception:
                            pass
                        break
            else:
                # Try as File path first
                found = self.store.query_one(
                    "MATCH (f:File {path: $p}) RETURN f.path", {"p": target}
                )
                if found:
                    try:
                        self.store.create_edge("TARGETS_FILE", "Plan", plan_id, "File", target)
                        linked += 1
                    except Exception:
                        pass
                    continue

                # Try as Module path
                found = self.store.query_one(
                    "MATCH (m:Module {path: $p}) RETURN m.path", {"p": target}
                )
                if found:
                    try:
                        self.store.create_edge("TARGETS_MODULE", "Plan", plan_id, "Module", target)
                        linked += 1
                    except Exception:
                        pass
                    continue

                # Try as symbol name (Class or Function)
                for table, rel in [
                    ("Class", "TARGETS_CLASS"), ("Function", "TARGETS_FUNC"),
                ]:
                    matches = self.store.query(
                        f"MATCH (t:{table}) WHERE t.name = $name RETURN t.id", {"name": target}
                    )
                    for match in matches:
                        try:
                            self.store.create_edge(rel, "Plan", plan_id, table, match[0])
                            linked += 1
                        except Exception:
                            pass
                    if matches:
                        break

        return linked

    # -- YAML ingestion -------------------------------------------------------

    def ingest_plans_dir(self, plans_dir: str | Path) -> dict:
        """Ingest plan files from a directory. Returns stats."""
        plans_dir = Path(plans_dir)
        stats = {"created": 0, "updated": 0, "skipped": 0}

        if not plans_dir.exists():
            return stats

        for plan_file in sorted(plans_dir.glob("*.yaml")) + sorted(plans_dir.glob("*.yml")):
            try:
                data = yaml.safe_load(plan_file.read_text())
                if not data or not isinstance(data, dict):
                    stats["skipped"] += 1
                    continue
                self._ingest_plan_file(data, plan_file.stem, stats)
            except Exception:
                stats["skipped"] += 1

        return stats

    def _ingest_plan_file(self, data: dict, file_stem: str, stats: dict) -> None:
        """Ingest a single plan YAML file."""
        # Use file stem as plan id for idempotent re-ingestion
        plan_id = data.get("id", file_stem)
        title = data.get("title", file_stem)
        description = data.get("description", "")
        status = data.get("status", "draft")
        author = data.get("author", "")
        targets = data.get("targets", [])
        depends_on = data.get("depends_on", [])
        intents = data.get("intents", [])

        # Check if plan already exists
        existing = self.store.query_one(
            "MATCH (p:Plan {id: $id}) RETURN p.id", {"id": plan_id}
        )

        if existing:
            self.update_plan(plan_id, title=title, description=description, status=status)
            stats["updated"] += 1
        else:
            now = _now()
            self.store.execute_write(
                """CREATE (p:Plan {
                    id: $id, title: $title, description: $descr,
                    status: $status, created_at: $now, updated_at: $now, author: $author
                })""",
                {"id": plan_id, "title": title, "descr": description,
                 "status": status, "now": now, "author": author},
            )
            stats["created"] += 1

            if targets:
                self._link_targets(plan_id, targets)

            if depends_on:
                for dep_id in depends_on:
                    try:
                        self.store.create_edge("DEPENDS_ON_PLAN", "Plan", plan_id, "Plan", dep_id)
                    except Exception:
                        pass

            for intent_data in intents:
                if isinstance(intent_data, dict):
                    self.create_intent(
                        plan_id,
                        description=intent_data.get("description", ""),
                        rationale=intent_data.get("rationale", ""),
                        status=intent_data.get("status", "draft"),
                    )
