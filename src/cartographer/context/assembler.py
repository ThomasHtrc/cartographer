"""Token-budget-aware context assembler.

Takes ranked nodes from the Ranker and assembles a context payload that fits
within a token budget. Prioritizes signatures over full implementations to
maximize information density.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ranker import RankedNode

from ..storage.store import GraphStore


# -- Token estimation ---------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Estimate token count using chars/4 heuristic.

    Good enough for budget management — within ~10% of tiktoken for code.
    """
    return max(1, len(text) // 4)


# -- Data types ---------------------------------------------------------------

@dataclass
class ContextItem:
    """A single piece of context to include in the LLM prompt."""
    kind: str               # "file_header", "signature", "code_block", "plan"
    file_path: str
    name: str
    content: str            # The actual text to include
    tokens: int             # Estimated token count
    score: float            # Relevance score from ranker
    line_start: int = 0
    line_end: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssembledContext:
    """The complete assembled context, ready for formatting."""
    items: list[ContextItem]
    total_tokens: int
    budget: int
    focal_points: list[str]
    files_included: int
    symbols_included: int


# -- Assembler ----------------------------------------------------------------

class Assembler:
    """Assembles context from ranked nodes within a token budget."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        signature_only: bool = True,
        store: GraphStore | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.signature_only = signature_only
        self.store = store

    def assemble(
        self,
        ranked_nodes: list[RankedNode],
        budget: int,
        focal_points: list[str] | None = None,
    ) -> AssembledContext:
        """Assemble context items from ranked nodes within the token budget.

        Strategy:
          1. Group ranked nodes by file
          2. For each file (in rank order), emit a file header
          3. For each symbol in that file, emit signature or code block
          4. Stop when budget is exhausted
          5. Append active plan annotations if budget remains
        """
        items: list[ContextItem] = []
        used_tokens = 0
        files_seen: set[str] = set()
        symbols_count = 0

        # Expand File nodes: if a file is ranked but has no symbol children
        # in the ranked list, pull its symbols from the graph
        ranked_nodes = self._expand_file_nodes(ranked_nodes)

        # Group nodes by file, preserving rank order for file priority
        file_order: list[str] = []
        file_nodes: dict[str, list[RankedNode]] = {}
        for node in ranked_nodes:
            if node.kind == "Module":
                continue  # Modules are directories, not useful in context
            fp = node.file_path
            if fp not in file_nodes:
                file_order.append(fp)
                file_nodes[fp] = []
            file_nodes[fp].append(node)

        for fp in file_order:
            nodes = file_nodes[fp]

            # File header
            if fp not in files_seen:
                header = f"# {fp}"
                header_tokens = estimate_tokens(header)
                if used_tokens + header_tokens > budget:
                    break
                items.append(ContextItem(
                    kind="file_header",
                    file_path=fp,
                    name=fp,
                    content=header,
                    tokens=header_tokens,
                    score=nodes[0].score,
                ))
                used_tokens += header_tokens
                files_seen.add(fp)

            # Symbols in this file
            for node in nodes:
                if node.kind in ("File", "Module"):
                    continue  # File handled by header; Module not useful in context

                content = self._render_node(node)
                tokens = estimate_tokens(content)

                if used_tokens + tokens > budget:
                    # Try signature-only fallback if we were including code
                    if not self.signature_only and node.signature:
                        content = self._render_signature(node)
                        tokens = estimate_tokens(content)
                        if used_tokens + tokens > budget:
                            continue
                    else:
                        continue

                items.append(ContextItem(
                    kind="signature" if self.signature_only else "code_block",
                    file_path=fp,
                    name=node.name,
                    content=content,
                    tokens=tokens,
                    score=node.score,
                    line_start=node.line_start,
                    line_end=node.line_end,
                ))
                used_tokens += tokens
                symbols_count += 1

        # Append active plan annotations if budget remains
        if self.store and focal_points:
            plan_items = self._get_plan_annotations(focal_points, budget - used_tokens)
            for item in plan_items:
                if used_tokens + item.tokens <= budget:
                    items.append(item)
                    used_tokens += item.tokens

        return AssembledContext(
            items=items,
            total_tokens=used_tokens,
            budget=budget,
            focal_points=focal_points or [],
            files_included=len(files_seen),
            symbols_included=symbols_count,
        )

    def _get_plan_annotations(self, focal_points: list[str], remaining_budget: int) -> list[ContextItem]:
        """Find active plans targeting focal files and render as context items."""
        items: list[ContextItem] = []
        seen_plans: set[str] = set()

        for fp in focal_points:
            # Find active plans targeting this file (or files matching it)
            rows = self.store.query(
                """MATCH (p:Plan)-[:TARGETS_FILE]->(f:File)
                WHERE p.status = 'active'
                  AND (f.path = $fp OR f.path ENDS WITH $suffix)
                RETURN DISTINCT p.id, p.title, p.description""",
                {"fp": fp, "suffix": "/" + fp.strip("/")},
            )
            # Also check module targets
            mod_rows = self.store.query(
                """MATCH (p:Plan)-[:TARGETS_MODULE]->(m:Module)
                WHERE p.status = 'active'
                  AND (m.path = $fp OR m.path ENDS WITH $suffix)
                RETURN DISTINCT p.id, p.title, p.description""",
                {"fp": fp, "suffix": "/" + fp.strip("/")},
            )
            for r in rows + mod_rows:
                plan_id, title, description = r[0], r[1], r[2]
                if plan_id in seen_plans:
                    continue
                seen_plans.add(plan_id)

                # Get pending intents
                intent_rows = self.store.query(
                    "MATCH (i:Intent)-[:IMPLEMENTS]->(p:Plan {id: $id}) "
                    "WHERE i.status IN ['draft', 'in_progress', 'active'] "
                    "RETURN i.description, i.status",
                    {"id": plan_id},
                )
                text = f"## Active Plan: {title}\n{description}"
                if intent_rows:
                    text += "\nPending intents:"
                    for ir in intent_rows:
                        text += f"\n  - [{ir[1]}] {ir[0]}"

                tokens = estimate_tokens(text)
                if tokens <= remaining_budget:
                    items.append(ContextItem(
                        kind="plan", file_path="", name=title,
                        content=text, tokens=tokens, score=1.0,
                        metadata={"plan_id": plan_id},
                    ))
                    remaining_budget -= tokens

        return items

    def _expand_file_nodes(self, ranked_nodes: list[RankedNode]) -> list[RankedNode]:
        """Expand File nodes that have no symbol children in the ranked list.

        When a File is ranked but none of its symbols made the cut, query the
        graph for its functions/classes and inject them so the context output
        isn't just an empty file header.
        """
        if not self.store:
            return ranked_nodes

        # Find files that appear only as File nodes (no symbols from that file)
        files_with_symbols: set[str] = set()
        file_nodes_by_path: dict[str, RankedNode] = {}
        for node in ranked_nodes:
            if node.kind == "File":
                file_nodes_by_path[node.file_path] = node
            elif node.kind not in ("Module",):
                files_with_symbols.add(node.file_path)

        files_needing_expansion = set(file_nodes_by_path) - files_with_symbols
        if not files_needing_expansion:
            return ranked_nodes

        expanded = list(ranked_nodes)
        for fp in files_needing_expansion:
            parent = file_nodes_by_path[fp]
            # Pull functions
            rows = self.store.query(
                "MATCH (f:File {path: $fp})-[:CONTAINS_FUNC]->(fn:Function) "
                "RETURN fn.name, fn.line_start, fn.line_end, fn.signature",
                {"fp": fp},
            )
            for r in rows:
                expanded.append(RankedNode(
                    kind="Function", id=f"{fp}::{r[0]}", name=r[0],
                    file_path=fp, score=parent.score * 0.9,
                    line_start=r[1], line_end=r[2], signature=r[3],
                ))
            # Pull classes
            rows = self.store.query(
                "MATCH (f:File {path: $fp})-[:CONTAINS_CLASS]->(c:Class) "
                "RETURN c.name, c.line_start, c.line_end",
                {"fp": fp},
            )
            for r in rows:
                expanded.append(RankedNode(
                    kind="Class", id=f"{fp}::{r[0]}", name=r[0],
                    file_path=fp, score=parent.score * 0.9,
                    line_start=r[1], line_end=r[2], signature="",
                ))

        return expanded

    def _render_node(self, node: RankedNode) -> str:
        """Render a node as context text."""
        if self.signature_only:
            return self._render_signature(node)
        return self._render_code_block(node)

    def _render_signature(self, node: RankedNode) -> str:
        """Render just the signature/declaration line."""
        if node.signature:
            return f"  {node.signature}"
        if node.kind == "Class":
            return f"  class {node.name}:"
        if node.kind == "Function":
            return f"  def {node.name}(...):"
        if node.kind in ("Type", "Variable"):
            return f"  {node.name}"
        return f"  {node.name}"

    def _render_code_block(self, node: RankedNode) -> str:
        """Render the full source code for a node by reading from disk."""
        if not node.line_start or not node.line_end:
            return self._render_signature(node)

        source_path = self.repo_path / node.file_path
        if not source_path.exists():
            return self._render_signature(node)

        try:
            lines = source_path.read_text().splitlines()
            start = max(0, node.line_start - 1)
            end = min(len(lines), node.line_end)
            code = "\n".join(lines[start:end])
            return f"  {code}"
        except Exception:
            return self._render_signature(node)
