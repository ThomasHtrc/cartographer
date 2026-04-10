"""Base extractor interface for language-level symbol extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SymbolNode:
    """A code symbol extracted from a source file."""
    kind: str          # "function", "class", "type", "variable"
    id: str            # unique: "file_path::QualifiedName"
    name: str          # short name
    file_path: str
    line_start: int
    line_end: int
    signature: str = ""
    visibility: str = "public"
    is_method: bool = False
    parent_id: str | None = None  # for methods: the owning class id


@dataclass
class EdgeRef:
    """A relationship extracted from source code."""
    kind: str          # edge type: "CALLS", "IMPORTS", "INHERITS", etc.
    from_kind: str     # node table: "Function", "File", "Class", ...
    from_id: str       # source node id or path
    to_kind: str       # target node table
    to_id: str         # target node id, path, or unresolved name
    resolved: bool = True  # False if to_id is an unresolved name needing cross-file resolution
    props: dict = field(default_factory=dict)


@dataclass
class FileExtraction:
    """Complete extraction result for a single file."""
    file_path: str
    lang: str
    nodes: list[SymbolNode]
    edges: list[EdgeRef]


class BaseExtractor(Protocol):
    """Protocol for language-specific extractors."""

    lang: str
    extensions: tuple[str, ...]

    def extract(self, file_path: str, source: bytes) -> FileExtraction:
        """Extract all symbols and relationships from a source file."""
        ...
