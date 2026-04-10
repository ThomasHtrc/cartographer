"""Multi-signal relevance ranker for context generation.

Computes a relevance score for every node in the graph relative to a set of
focal points (files or symbols the user is working on). Combines:

  1. Personalized PageRank over the structural graph (Layer 1)
  2. Recency boost — recently modified files score higher (Layer 2)
  3. Co-change boost — files that change with focal files (Layer 2)
  4. Plan boost — nodes targeted by active plans (Layer 3)
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..storage.store import GraphStore


# -- Data types ---------------------------------------------------------------

class RankedNode:
    """A graph node with a relevance score."""

    __slots__ = ("kind", "id", "name", "file_path", "score", "line_start", "line_end", "signature")

    def __init__(
        self,
        kind: str,
        id: str,
        name: str,
        file_path: str,
        score: float,
        line_start: int = 0,
        line_end: int = 0,
        signature: str = "",
    ) -> None:
        self.kind = kind
        self.id = id
        self.name = name
        self.file_path = file_path
        self.score = score
        self.line_start = line_start
        self.line_end = line_end
        self.signature = signature

    def __repr__(self) -> str:
        return f"RankedNode({self.kind}, {self.name!r}, score={self.score:.4f})"


# -- Ranker -------------------------------------------------------------------

class Ranker:
    """Computes multi-signal relevance scores from focal nodes."""

    def __init__(
        self,
        store: GraphStore,
        *,
        damping: float = 0.85,
        iterations: int = 20,
        recency_weight: float = 0.15,
        co_change_weight: float = 0.10,
        plan_weight: float = 0.10,
    ) -> None:
        self.store = store
        self.damping = damping
        self.iterations = iterations
        self.recency_weight = recency_weight
        self.co_change_weight = co_change_weight
        self.plan_weight = plan_weight

    def rank(self, focal_points: list[str], max_results: int = 100) -> list[RankedNode]:
        """Rank nodes by relevance to the given focal points.

        focal_points: list of file paths, symbol names, or qualified ids
        Returns: ranked list of RankedNode, highest score first.
        """
        # Step 1: Resolve focal points to node identifiers
        focal_ids = self._resolve_focal_points(focal_points)
        if not focal_ids:
            return []

        # Step 2: Build adjacency graph and compute personalized PageRank
        nodes, adj = self._build_adjacency()
        if not nodes:
            return []

        pr_scores = self._personalized_pagerank(nodes, adj, focal_ids)

        # Step 3: Compute boost signals
        focal_files = self._get_focal_files(focal_ids)
        recency_scores = self._recency_signal(focal_files)
        co_change_scores = self._co_change_signal(focal_files)
        plan_scores = self._plan_signal()

        # Step 4: Combine signals
        combined: dict[str, float] = {}
        pr_weight = 1.0 - self.recency_weight - self.co_change_weight - self.plan_weight
        for node_id in nodes:
            score = pr_weight * pr_scores.get(node_id, 0.0)
            # Map node to its file for file-level signals
            file_path = nodes[node_id].get("file_path", node_id)
            score += self.recency_weight * recency_scores.get(file_path, 0.0)
            score += self.co_change_weight * co_change_scores.get(file_path, 0.0)
            score += self.plan_weight * plan_scores.get(node_id, 0.0)
            combined[node_id] = score

        # Step 5: Build ranked results
        ranked = sorted(combined.items(), key=lambda x: x[1], reverse=True)
        results: list[RankedNode] = []
        for node_id, score in ranked[:max_results]:
            if score <= 0:
                break
            info = nodes[node_id]
            results.append(RankedNode(
                kind=info["kind"],
                id=node_id,
                name=info.get("name", ""),
                file_path=info.get("file_path", node_id),
                score=score,
                line_start=info.get("line_start", 0),
                line_end=info.get("line_end", 0),
                signature=info.get("signature", ""),
            ))
        return results

    # -- Step 1: Resolve focal points -----------------------------------------

    def _resolve_focal_points(self, focal_points: list[str]) -> set[str]:
        """Resolve user-provided focal points to graph node IDs."""
        resolved: set[str] = set()
        for fp in focal_points:
            # Try as file path
            row = self.store.query_one(
                "MATCH (f:File {path: $p}) RETURN f.path", {"p": fp}
            )
            if row:
                resolved.add(row[0])
                continue

            # Try as qualified id (e.g., "file.py::ClassName")
            if "::" in fp:
                for table in ("Function", "Class"):
                    row = self.store.query_one(
                        f"MATCH (n:{table} {{id: $id}}) RETURN n.id", {"id": fp}
                    )
                    if row:
                        resolved.add(row[0])
                        break
                continue

            # Try as symbol name
            for table in ("Function", "Class", "Type", "Variable"):
                rows = self.store.query(
                    f"MATCH (n:{table}) WHERE n.name = $name RETURN n.id",
                    {"name": fp},
                )
                for r in rows:
                    resolved.add(r[0])
                if rows:
                    break

            # Try as module path
            if not any(r for r in resolved if fp in r):
                row = self.store.query_one(
                    "MATCH (m:Module {path: $p}) RETURN m.path", {"p": fp}
                )
                if row:
                    resolved.add(row[0])
                    continue

            # Fallback: suffix match on file paths (for partial paths like "src/api/routes.py")
            if not any(r for r in resolved if fp in r):
                suffix = "/" + fp.strip("/")
                rows = self.store.query(
                    "MATCH (f:File) WHERE f.path ENDS WITH $s RETURN f.path",
                    {"s": suffix},
                )
                for r in rows:
                    resolved.add(r[0])
                # Also try module suffix
                if not rows:
                    rows = self.store.query(
                        "MATCH (m:Module) WHERE m.path ENDS WITH $s RETURN m.path",
                        {"s": suffix},
                    )
                    for r in rows:
                        resolved.add(r[0])

        return resolved

    # -- Step 2: Build adjacency & PageRank -----------------------------------

    def _build_adjacency(self) -> tuple[dict[str, dict], dict[str, list[str]]]:
        """Build node info dict and adjacency list from the structural graph.

        Returns (nodes, adj) where:
          nodes: {node_id: {kind, name, file_path, line_start, line_end, signature}}
          adj: {from_id: [to_id, ...]}
        """
        nodes: dict[str, dict[str, Any]] = {}
        adj: dict[str, list[str]] = defaultdict(list)

        # Files
        for row in self.store.query(
            "MATCH (f:File) RETURN f.path, f.lang"
        ):
            fid = row[0]
            nodes[fid] = {"kind": "File", "name": fid, "file_path": fid}

        # Modules
        for row in self.store.query(
            "MATCH (m:Module) RETURN m.path, m.name"
        ):
            mid = row[0]
            nodes[mid] = {"kind": "Module", "name": row[1], "file_path": mid}

        # Functions
        for row in self.store.query(
            "MATCH (fn:Function) RETURN fn.id, fn.name, fn.file_path, fn.line_start, fn.line_end, fn.signature"
        ):
            nodes[row[0]] = {
                "kind": "Function", "name": row[1], "file_path": row[2],
                "line_start": row[3], "line_end": row[4], "signature": row[5] or "",
            }

        # Classes
        for row in self.store.query(
            "MATCH (c:Class) RETURN c.id, c.name, c.file_path, c.line_start, c.line_end"
        ):
            nodes[row[0]] = {
                "kind": "Class", "name": row[1], "file_path": row[2],
                "line_start": row[3], "line_end": row[4],
            }

        # Types
        for row in self.store.query(
            "MATCH (t:Type) RETURN t.id, t.name, t.file_path, t.line_start, t.line_end"
        ):
            nodes[row[0]] = {
                "kind": "Type", "name": row[1], "file_path": row[2],
                "line_start": row[3], "line_end": row[4],
            }

        # Variables
        for row in self.store.query(
            "MATCH (v:Variable) RETURN v.id, v.name, v.file_path, v.line_start, v.line_end"
        ):
            nodes[row[0]] = {
                "kind": "Variable", "name": row[1], "file_path": row[2],
                "line_start": row[3], "line_end": row[4],
            }

        # Edges — structural relationships that indicate relevance propagation
        edge_queries = [
            # File contains symbols
            ("MATCH (f:File)-[:CONTAINS_FUNC]->(fn:Function) RETURN f.path, fn.id", None),
            ("MATCH (f:File)-[:CONTAINS_CLASS]->(c:Class) RETURN f.path, c.id", None),
            ("MATCH (f:File)-[:CONTAINS_TYPE]->(t:Type) RETURN f.path, t.id", None),
            ("MATCH (f:File)-[:CONTAINS_VAR]->(v:Variable) RETURN f.path, v.id", None),
            # File imports
            ("MATCH (a:File)-[:IMPORTS]->(b:File) RETURN a.path, b.path", None),
            # Class methods
            ("MATCH (c:Class)-[:HAS_METHOD]->(fn:Function) RETURN c.id, fn.id", None),
            # Calls
            ("MATCH (a:Function)-[:CALLS]->(b:Function) RETURN a.id, b.id", None),
            # Inheritance
            ("MATCH (a:Class)-[:INHERITS]->(b:Class) RETURN a.id, b.id", None),
            # Type references
            ("MATCH (fn:Function)-[:EXPECTS_TYPE]->(t:Type) RETURN fn.id, t.id", None),
            ("MATCH (fn:Function)-[:EXPECTS_CLASS]->(c:Class) RETURN fn.id, c.id", None),
            ("MATCH (fn:Function)-[:RETURNS_TYPE]->(t:Type) RETURN fn.id, t.id", None),
            ("MATCH (fn:Function)-[:RETURNS_CLASS]->(c:Class) RETURN fn.id, c.id", None),
            # Module membership
            ("MATCH (f:File)-[:BELONGS_TO]->(m:Module) RETURN f.path, m.path", None),
        ]

        for cypher, params in edge_queries:
            for row in self.store.query(cypher, params):
                src, dst = str(row[0]), str(row[1])
                if src in nodes and dst in nodes:
                    adj[src].append(dst)
                    # Bidirectional for relevance propagation
                    adj[dst].append(src)

        return nodes, dict(adj)

    def _personalized_pagerank(
        self,
        nodes: dict[str, dict],
        adj: dict[str, list[str]],
        focal_ids: set[str],
    ) -> dict[str, float]:
        """Compute personalized PageRank seeded from focal nodes."""
        n = len(nodes)
        if n == 0:
            return {}

        node_list = list(nodes.keys())
        idx = {nid: i for i, nid in enumerate(node_list)}

        # Personalization vector: uniform over focal nodes
        personalization = [0.0] * n
        if focal_ids:
            weight = 1.0 / len(focal_ids)
            for fid in focal_ids:
                if fid in idx:
                    personalization[idx[fid]] = weight

        # Initialize scores
        scores = list(personalization) if any(personalization) else [1.0 / n] * n

        # Iterate
        d = self.damping
        for _ in range(self.iterations):
            new_scores = [0.0] * n
            for i, nid in enumerate(node_list):
                neighbors = adj.get(nid, [])
                if neighbors:
                    share = scores[i] / len(neighbors)
                    for neighbor in neighbors:
                        if neighbor in idx:
                            new_scores[idx[neighbor]] += d * share
            # Add personalization (teleport)
            for i in range(n):
                new_scores[i] += (1 - d) * personalization[i]
            scores = new_scores

        return {node_list[i]: scores[i] for i in range(n)}

    # -- Step 3: Boost signals ------------------------------------------------

    def _get_focal_files(self, focal_ids: set[str]) -> set[str]:
        """Extract file paths from focal node IDs."""
        files: set[str] = set()
        for fid in focal_ids:
            # If it's a file path directly
            row = self.store.query_one(
                "MATCH (f:File {path: $p}) RETURN f.path", {"p": fid}
            )
            if row:
                files.add(row[0])
                continue
            # If it's a symbol, get its file
            for table in ("Function", "Class", "Type", "Variable"):
                row = self.store.query_one(
                    f"MATCH (n:{table} {{id: $id}}) RETURN n.file_path", {"id": fid}
                )
                if row:
                    files.add(row[0])
                    break
        return files

    def _recency_signal(self, focal_files: set[str]) -> dict[str, float]:
        """Score files by how recently they were modified.

        Files with recent commits get higher scores. Normalized to [0, 1].
        """
        if not focal_files:
            return {}

        # Get all files with commit counts, ordered by most recent
        rows = self.store.query(
            """MATCH (f:File)-[:CHANGED_IN]->(c:Commit)
            RETURN f.path, count(c) AS changes, max(c.timestamp) AS latest
            ORDER BY latest DESC"""
        )
        if not rows:
            return {}

        # Assign scores by rank position (most recent = 1.0, decaying)
        scores: dict[str, float] = {}
        for rank, row in enumerate(rows):
            scores[row[0]] = 1.0 / (1 + rank)
        return scores

    def _co_change_signal(self, focal_files: set[str]) -> dict[str, float]:
        """Score files by co-change correlation with focal files."""
        if not focal_files:
            return {}

        scores: dict[str, float] = defaultdict(float)
        for fp in focal_files:
            rows = self.store.query(
                """MATCH (f:File {path: $fp})-[r:CO_CHANGES_WITH]->(other:File)
                RETURN other.path, r.count""",
                {"fp": fp},
            )
            for row in rows:
                scores[row[0]] += float(row[1])

        # Normalize to [0, 1]
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                scores = {k: v / max_score for k, v in scores.items()}

        return dict(scores)

    def _plan_signal(self) -> dict[str, float]:
        """Score nodes targeted by active plans."""
        scores: dict[str, float] = {}

        for rel, table, pk in [
            ("TARGETS_FILE", "File", "path"),
            ("TARGETS_MODULE", "Module", "path"),
            ("TARGETS_FUNC", "Function", "id"),
            ("TARGETS_CLASS", "Class", "id"),
        ]:
            rows = self.store.query(
                f"MATCH (p:Plan)-[:{rel}]->(t:{table}) "
                f"WHERE p.status = 'active' "
                f"RETURN t.{pk}"
            )
            for row in rows:
                scores[row[0]] = 1.0

        return scores
