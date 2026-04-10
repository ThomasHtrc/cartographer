"""Plan indexer: Layer 3 — ingest plan files from .graph-context/plans/."""

from __future__ import annotations

from pathlib import Path

from ..storage.store import GraphStore
from ..plans.manager import PlanManager


class PlanIndexer:
    """Indexes plan files into the graph (Layer 3)."""

    def __init__(self, store: GraphStore, repo_path: str | Path) -> None:
        self.store = store
        self.repo_path = Path(repo_path).resolve()
        self.manager = PlanManager(store)

    def index(self) -> dict:
        """Ingest all plan YAML files from .graph-context/plans/."""
        self.store.ensure_schema(layers=("structure", "planning"))
        plans_dir = self.repo_path / ".graph-context" / "plans"
        return self.manager.ingest_plans_dir(plans_dir)
