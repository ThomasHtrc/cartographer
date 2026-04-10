"""Structure indexer: Layer 1 — parse code, extract symbols, populate graph."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from ..storage.store import GraphStore
from .extractors.base import FileExtraction, EdgeRef, SymbolNode
from .extractors.python import PythonExtractor
from .extractors.typescript import TypeScriptExtractor, TSXExtractor, JavaScriptExtractor
from . import git_ops

# Registry of extractors by file extension
EXTRACTORS = {}

for _extractor in (PythonExtractor(), TypeScriptExtractor(), TSXExtractor(), JavaScriptExtractor()):
    for ext in _extractor.extensions:
        EXTRACTORS[ext] = _extractor


def _file_hash(path: Path) -> str:
    """Compute a quick hash of file contents."""
    return hashlib.md5(path.read_bytes()).hexdigest()


def _file_ext(path: str) -> str:
    return os.path.splitext(path)[1]


def _module_path(file_path: str) -> str:
    """Derive a module path from a file path (directory)."""
    return str(Path(file_path).parent)


class StructureIndexer:
    """Indexes codebase structure into the graph (Layer 1)."""

    def __init__(self, store: GraphStore, repo_path: str | Path) -> None:
        self.store = store
        self.repo_path = Path(repo_path).resolve()
        self._unresolved_edges: list[EdgeRef] = []

    def index_full(self) -> dict:
        """Full index: parse all supported files and populate the graph."""
        self.store.ensure_schema(layers=("structure",))

        if git_ops.is_git_repo(self.repo_path):
            files = git_ops.get_all_tracked_files(self.repo_path)
        else:
            files = self._walk_files()

        stats = {"files_indexed": 0, "nodes_created": 0, "edges_created": 0, "skipped": 0, "errors": 0}
        for rel_path in files:
            ext = _file_ext(rel_path)
            if ext not in EXTRACTORS:
                stats["skipped"] += 1
                continue
            abs_path = self.repo_path / rel_path
            if not abs_path.is_file():
                continue
            try:
                # Clear stale data from previous indexing runs
                self.store.clear_file(rel_path)
                file_stats = self._index_file(rel_path, abs_path)
                stats["files_indexed"] += 1
                stats["nodes_created"] += file_stats["nodes"]
                stats["edges_created"] += file_stats["edges"]
            except Exception as exc:
                stats["errors"] += 1

        # Build module nodes and BELONGS_TO edges
        self._build_modules(files)

        # Resolve cross-file references (CALLS, INHERITS, etc.)
        resolved = self._resolve_references()
        stats["edges_resolved"] = resolved

        return stats

    def index_incremental(self, since_hash: str | None) -> dict:
        """Incremental index: only re-index changed files."""
        self.store.ensure_schema(layers=("structure",))

        if not git_ops.is_git_repo(self.repo_path):
            return self.index_full()

        changed = git_ops.get_changed_files(self.repo_path, since_hash)
        stats = {"files_indexed": 0, "nodes_created": 0, "edges_created": 0, "skipped": 0, "errors": 0}

        for rel_path in changed:
            ext = _file_ext(rel_path)
            if ext not in EXTRACTORS:
                stats["skipped"] += 1
                continue
            abs_path = self.repo_path / rel_path
            if not abs_path.is_file():
                # File was deleted — clear it
                self.store.clear_file(rel_path)
                continue

            # Clear old data, re-index
            self.store.clear_file(rel_path)
            try:
                file_stats = self._index_file(rel_path, abs_path)
                stats["files_indexed"] += 1
                stats["nodes_created"] += file_stats["nodes"]
                stats["edges_created"] += file_stats["edges"]
            except Exception:
                stats["errors"] += 1

        return stats

    def index_files(self, rel_paths: list[str]) -> dict:
        """Re-index specific files by relative path (for file watcher use)."""
        self.store.ensure_schema(layers=("structure",))
        stats = {"files_indexed": 0, "nodes_created": 0, "edges_created": 0, "errors": 0}

        for rel_path in rel_paths:
            ext = _file_ext(rel_path)
            if ext not in EXTRACTORS:
                continue
            abs_path = self.repo_path / rel_path
            if not abs_path.is_file():
                self.store.clear_file(rel_path)
                continue

            self.store.clear_file(rel_path)
            try:
                file_stats = self._index_file(rel_path, abs_path)
                stats["files_indexed"] += 1
                stats["nodes_created"] += file_stats["nodes"]
                stats["edges_created"] += file_stats["edges"]
            except Exception:
                stats["errors"] += 1

        # Resolve cross-file references for the changed files
        resolved = self._resolve_references()
        stats["edges_resolved"] = resolved
        return stats

    def _index_file(self, rel_path: str, abs_path: Path) -> dict:
        """Index a single file: parse, extract, write to graph."""
        ext = _file_ext(rel_path)
        extractor = EXTRACTORS[ext]

        source = abs_path.read_bytes()
        extraction = extractor.extract(rel_path, source)

        # Create File node
        h = _file_hash(abs_path)
        mtime = datetime.fromtimestamp(abs_path.stat().st_mtime).isoformat()
        self.store.upsert_file(rel_path, extraction.lang, h, mtime)

        # Create symbol nodes (skip duplicates gracefully)
        for sym in extraction.nodes:
            try:
                if sym.kind == "function":
                    self.store.create_function(
                        sym.id, sym.name, sym.file_path,
                        sym.line_start, sym.line_end,
                        sym.signature, sym.visibility, sym.is_method,
                    )
                elif sym.kind == "class":
                    self.store.create_class(
                        sym.id, sym.name, sym.file_path,
                        sym.line_start, sym.line_end, sym.visibility,
                    )
                elif sym.kind == "type":
                    self.store.create_type(
                        sym.id, sym.name, sym.file_path,
                        sym.line_start, sym.line_end,
                    )
                elif sym.kind == "variable":
                    self.store.create_variable(
                        sym.id, sym.name, sym.file_path,
                        sym.line_start, sym.line_end,
                    )
            except RuntimeError:
                pass  # duplicate PK — skip

        # Create resolved edges; collect unresolved for later
        edges_created = 0
        for edge in extraction.edges:
            if edge.resolved:
                try:
                    self.store.create_edge(
                        edge.kind, edge.from_kind, edge.from_id,
                        edge.to_kind, edge.to_id, edge.props,
                    )
                    edges_created += 1
                except Exception:
                    pass
            else:
                self._unresolved_edges.append(edge)

        return {"nodes": len(extraction.nodes), "edges": edges_created}

    def _build_modules(self, files: list[str]) -> None:
        """Create Module nodes for directories and BELONGS_TO edges."""
        modules_seen: set[str] = set()
        for rel_path in files:
            ext = _file_ext(rel_path)
            if ext not in EXTRACTORS:
                continue
            mod_path = _module_path(rel_path)
            if mod_path == ".":
                mod_path = ""
            if mod_path not in modules_seen:
                mod_name = Path(mod_path).name if mod_path else "(root)"
                self.store.upsert_module(mod_path, mod_name)
                modules_seen.add(mod_path)
            try:
                self.store.create_edge("BELONGS_TO", "File", rel_path, "Module", mod_path)
            except Exception:
                pass

    def _resolve_references(self) -> int:
        """Resolve unresolved edges by matching names to known nodes.

        For CALLS edges: match callee name against Function.name in the graph.
        For INHERITS: match base class name against Class.name.
        For IMPORTS: match module name against File paths.
        For EXPECTS_*/RETURNS_*: match type name against Type.name or Class.name.
        """
        resolved = 0

        # Build lookup caches from the graph
        functions_by_name: dict[str, list[str]] = {}  # name -> [id, ...]
        classes_by_name: dict[str, list[str]] = {}
        types_by_name: dict[str, list[str]] = {}

        for row in self.store.query("MATCH (f:Function) RETURN f.name, f.id"):
            functions_by_name.setdefault(row[0], []).append(row[1])
        for row in self.store.query("MATCH (c:Class) RETURN c.name, c.id"):
            classes_by_name.setdefault(row[0], []).append(row[1])
        for row in self.store.query("MATCH (t:Type) RETURN t.name, t.id"):
            types_by_name.setdefault(row[0], []).append(row[1])

        for edge in self._unresolved_edges:
            targets: list[str] = []
            target_kind = edge.to_kind

            if edge.kind == "CALLS":
                targets = functions_by_name.get(edge.to_id, [])
                target_kind = "Function"
            elif edge.kind == "INHERITS":
                targets = classes_by_name.get(edge.to_id, [])
                target_kind = "Class"
            elif edge.kind in ("EXPECTS_TYPE", "RETURNS_TYPE", "YIELDS_TYPE", "USES_TYPE"):
                # Try Type first, then Class
                targets = types_by_name.get(edge.to_id, [])
                if not targets:
                    targets = classes_by_name.get(edge.to_id, [])
                    if targets:
                        # Switch to the Class variant of the rel
                        edge.kind = edge.kind.replace("_TYPE", "_CLASS")
                        target_kind = "Class"
                    else:
                        target_kind = "Type"
                else:
                    target_kind = "Type"
            elif edge.kind == "IMPORTS":
                # Skip import resolution for now — needs module path mapping
                continue
            else:
                continue

            for target_id in targets:
                try:
                    self.store.create_edge(
                        edge.kind, edge.from_kind, edge.from_id,
                        target_kind, target_id, edge.props,
                    )
                    resolved += 1
                except Exception:
                    pass

        self._unresolved_edges.clear()
        return resolved

    def _walk_files(self) -> list[str]:
        """Walk the repo directory for supported files (non-git fallback)."""
        files = []
        for root, dirs, filenames in os.walk(self.repo_path):
            # Skip hidden dirs and common non-code dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
                "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
                ".git", ".graph-context",
            )]
            for f in filenames:
                ext = _file_ext(f)
                if ext in EXTRACTORS:
                    abs_path = Path(root) / f
                    files.append(str(abs_path.relative_to(self.repo_path)))
        return sorted(files)
