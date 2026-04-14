"""Output formatters for assembled context.

Produces:
  - Markdown repo map (file paths + indented signatures)
  - JSON structured output
  - Annotated context with metadata
"""

from __future__ import annotations

import json
from typing import Any

from .assembler import AssembledContext


def format_markdown(ctx: AssembledContext) -> str:
    """Format as a markdown repo map.

    Output looks like:
        # src/auth/login.py
          def authenticate(username: str, password: str) -> User:
          class AuthService:
            def validate_token(self, token: str) -> bool:
        # src/models/user.py
          class User:
          class UserRole:
    """
    lines: list[str] = []
    current_file = ""

    for item in ctx.items:
        if item.kind == "file_header":
            current_file = item.file_path
            lines.append(item.content)
        else:
            lines.append(item.content)

    if lines:
        lines.append("")
        lines.append(
            f"# ({ctx.files_included} files, {ctx.symbols_included} symbols, "
            f"~{ctx.total_tokens}/{ctx.budget} tokens)"
        )

    return "\n".join(lines)


def format_json(ctx: AssembledContext) -> str:
    """Format as structured JSON."""
    data: dict[str, Any] = {
        "focal_points": ctx.focal_points,
        "budget": ctx.budget,
        "total_tokens": ctx.total_tokens,
        "files_included": ctx.files_included,
        "symbols_included": ctx.symbols_included,
        "items": [],
    }

    for item in ctx.items:
        entry: dict[str, Any] = {
            "kind": item.kind,
            "file_path": item.file_path,
            "name": item.name,
            "content": item.content,
            "score": round(item.score, 4),
        }
        if item.line_start:
            entry["line_start"] = item.line_start
            entry["line_end"] = item.line_end
        if item.metadata:
            entry["metadata"] = item.metadata
        data["items"].append(entry)

    return json.dumps(data, indent=2)


def format_annotated(ctx: AssembledContext) -> str:
    """Format with metadata annotations (score, line range).

    Output looks like:
        # src/auth/login.py
          def authenticate(username: str, password: str) -> User:  [score=0.82, L10-25]
          class AuthService:  [score=0.65, L30-120]
    """
    lines: list[str] = []

    for item in ctx.items:
        if item.kind in ("file_header", "plan"):
            lines.append(item.content)
        else:
            annotation = f"[score={item.score:.2f}"
            if item.line_start:
                annotation += f", L{item.line_start}-{item.line_end}"
            annotation += "]"
            lines.append(f"{item.content}  {annotation}")

    if lines:
        lines.append("")
        lines.append(
            f"# ({ctx.files_included} files, {ctx.symbols_included} symbols, "
            f"~{ctx.total_tokens}/{ctx.budget} tokens)"
        )

    return "\n".join(lines)
