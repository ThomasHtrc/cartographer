"""History indexer: Layer 2 — git commits, changes, co-change patterns."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from pathlib import Path

from ..storage.store import GraphStore
from . import git_ops


class HistoryIndexer:
    """Indexes git history into the graph (Layer 2)."""

    def __init__(self, store: GraphStore, repo_path: str | Path) -> None:
        self.store = store
        self.repo_path = Path(repo_path).resolve()

    def index(
        self,
        max_commits: int = 500,
        since_hash: str | None = None,
        co_change_threshold: int = 2,
    ) -> dict:
        """Index git history: commits, changes, AFFECTS, and co-change patterns.

        Args:
            max_commits: Maximum number of commits to process.
            since_hash: Only process commits newer than this hash (incremental).
            co_change_threshold: Minimum co-occurrences to create a CO_CHANGES_WITH edge.
        """
        self.store.ensure_schema(layers=("structure", "history"))

        commits = git_ops.get_commit_log(self.repo_path, max_commits, since_hash)
        if not commits:
            return {"commits": 0, "changes": 0, "affects": 0, "co_changes": 0}

        stats = {"commits": 0, "changes": 0, "affects": 0, "co_changes": 0}

        # Build a lookup of known symbols by file_path for AFFECTS edges
        symbols_by_file = self._load_symbols_by_file()

        # Track file co-occurrences for CO_CHANGES_WITH
        co_occurrence: dict[tuple[str, str], int] = defaultdict(int)

        for commit in commits:
            self._index_commit(commit, symbols_by_file, co_occurrence, stats)

        # Build CO_CHANGES_WITH edges
        stats["co_changes"] = self._build_co_changes(co_occurrence, co_change_threshold)

        return stats

    def _index_commit(
        self,
        commit: git_ops.CommitInfo,
        symbols_by_file: dict[str, list[dict]],
        co_occurrence: dict[tuple[str, str], int],
        stats: dict,
    ) -> None:
        """Index a single commit: create Commit node, Change nodes, and edges."""
        # Create or update Commit node
        self.store.execute_write(
            """MERGE (c:Commit {hash: $hash})
               SET c.message = $msg, c.author = $author, c.timestamp = $ts""",
            {"hash": commit.hash, "msg": commit.message, "author": commit.author, "ts": commit.timestamp},
        )
        stats["commits"] += 1

        # Parent edges
        for parent_hash in commit.parent_hashes:
            # Only create if parent exists in the graph
            exists = self.store.query_one(
                "MATCH (p:Commit {hash: $h}) RETURN p.hash", {"h": parent_hash}
            )
            if exists:
                self.store.create_edge("PARENT", "Commit", commit.hash, "Commit", parent_hash)

        # Track file paths in this commit for co-change computation
        changed_paths = [ch.file_path for ch in commit.changes]
        for a, b in combinations(sorted(set(changed_paths)), 2):
            co_occurrence[(a, b)] += 1

        for change in commit.changes:
            change_id = f"{commit.hash}::{change.file_path}"

            # Create or update Change node
            self.store.execute_write(
                """MERGE (ch:Change {id: $id})
                   SET ch.file_path = $fp, ch.additions = $adds,
                       ch.deletions = $dels, ch.change_type = $ct""",
                {
                    "id": change_id, "fp": change.file_path,
                    "adds": change.additions, "dels": change.deletions,
                    "ct": change.change_type,
                },
            )
            stats["changes"] += 1

            # Commit -[:INCLUDES]-> Change
            self.store.create_edge("INCLUDES", "Commit", commit.hash, "Change", change_id)

            # File -[:CHANGED_IN]-> Commit (only if File node exists)
            file_exists = self.store.query_one(
                "MATCH (f:File {path: $fp}) RETURN f.path", {"fp": change.file_path}
            )
            if file_exists:
                self.store.create_edge("CHANGED_IN", "File", change.file_path, "Commit", commit.hash)

            # Change -[:AFFECTS]-> Function/Class (line-range overlap)
            if change.file_path in symbols_by_file:
                line_ranges = git_ops.get_diff_line_ranges(
                    self.repo_path, commit.hash, change.file_path
                )
                if line_ranges:
                    affected = self._find_affected_symbols(
                        symbols_by_file[change.file_path], line_ranges
                    )
                    for sym in affected:
                        edge_type = "AFFECTS_FUNC" if sym["kind"] == "function" else "AFFECTS_CLASS"
                        try:
                            self.store.create_edge(
                                edge_type, "Change", change_id, sym["kind"].capitalize(), sym["id"]
                            )
                            stats["affects"] += 1
                        except Exception:
                            pass

    def _load_symbols_by_file(self) -> dict[str, list[dict]]:
        """Load all function and class symbols grouped by file_path."""
        result: dict[str, list[dict]] = defaultdict(list)

        for row in self.store.query(
            "MATCH (f:Function) RETURN f.id, f.file_path, f.line_start, f.line_end"
        ):
            result[row[1]].append({
                "id": row[0], "kind": "function",
                "line_start": row[2], "line_end": row[3],
            })

        for row in self.store.query(
            "MATCH (c:Class) RETURN c.id, c.file_path, c.line_start, c.line_end"
        ):
            result[row[1]].append({
                "id": row[0], "kind": "class",
                "line_start": row[2], "line_end": row[3],
            })

        return dict(result)

    def _find_affected_symbols(
        self,
        symbols: list[dict],
        line_ranges: list[tuple[int, int]],
    ) -> list[dict]:
        """Find symbols whose line ranges overlap with the changed line ranges."""
        affected = []
        for sym in symbols:
            sym_start, sym_end = sym["line_start"], sym["line_end"]
            for change_start, change_end in line_ranges:
                if sym_start <= change_end and change_start <= sym_end:
                    affected.append(sym)
                    break
        return affected

    def _build_co_changes(
        self,
        co_occurrence: dict[tuple[str, str], int],
        threshold: int,
    ) -> int:
        """Create CO_CHANGES_WITH edges for file pairs that frequently co-change."""
        count = 0
        for (file_a, file_b), times in co_occurrence.items():
            if times < threshold:
                continue
            # Only create if both File nodes exist
            a_exists = self.store.query_one(
                "MATCH (f:File {path: $fp}) RETURN f.path", {"fp": file_a}
            )
            b_exists = self.store.query_one(
                "MATCH (f:File {path: $fp}) RETURN f.path", {"fp": file_b}
            )
            if a_exists and b_exists:
                try:
                    self.store.create_edge(
                        "CO_CHANGES_WITH", "File", file_a, "File", file_b,
                        props={"count": times, "correlation": 0.0},
                    )
                    count += 1
                except Exception:
                    pass
        return count
