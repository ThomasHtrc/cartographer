"""Unified timeline view: past commits + future plans for any codebase target.

A `target` can be a file path, module path, function name, or class name. The
module resolves the target, gathers history from Layer 2 (commits/changes/affects)
and plans from Layer 3 (plans/intents/targets), and returns a structured dict
that can be rendered as markdown (for agents), JSON (programmatic), or HTML (for
humans, via the CLI).
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

from .storage.store import GraphStore
from .plans.manager import PlanManager


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def resolve_target(store: GraphStore, target: str) -> dict | None:
    """Resolve a target string to a typed entity.

    Tries, in order: exact File, exact Module, suffix File, suffix Module,
    Function name, Class name. Returns None if nothing matches.
    """
    target = (target or "").strip()
    if not target:
        return None

    # Exact File
    if store.query("MATCH (f:File {path: $p}) RETURN f.path LIMIT 1", {"p": target}):
        return {"kind": "file", "name": target, "path": target}

    # Exact Module
    if store.query("MATCH (m:Module {path: $p}) RETURN m.path LIMIT 1", {"p": target}):
        return {"kind": "module", "name": target, "path": target}

    # Suffix File
    suffix = "/" + target.strip("/")
    rows = store.query(
        "MATCH (f:File) WHERE f.path ENDS WITH $s RETURN f.path LIMIT 5",
        {"s": suffix},
    )
    if rows:
        # Prefer the shortest path (least nesting) when multiple match
        paths = sorted([r[0] for r in rows], key=len)
        return {"kind": "file", "name": target, "path": paths[0]}

    # Suffix Module
    rows = store.query(
        "MATCH (m:Module) WHERE m.path ENDS WITH $s RETURN m.path LIMIT 5",
        {"s": suffix},
    )
    if rows:
        paths = sorted([r[0] for r in rows], key=len)
        return {"kind": "module", "name": target, "path": paths[0]}

    # Function name
    rows = store.query(
        "MATCH (f:Function) WHERE f.name = $n RETURN f.id, f.file_path LIMIT 1",
        {"n": target},
    )
    if rows:
        return {"kind": "function", "name": target, "path": rows[0][1]}

    # Class name
    rows = store.query(
        "MATCH (c:Class) WHERE c.name = $n RETURN c.id, c.file_path LIMIT 1",
        {"n": target},
    )
    if rows:
        return {"kind": "class", "name": target, "path": rows[0][1]}

    return None


# ---------------------------------------------------------------------------
# Past events (history)
# ---------------------------------------------------------------------------

def _past_for_file(store: GraphStore, path: str, limit: int) -> list[dict]:
    rows = store.query(
        """MATCH (c:Commit)-[:INCLUDES]->(ch:Change)
           WHERE ch.file_path = $p
           RETURN c.hash, c.timestamp, c.author, c.message,
                  ch.additions, ch.deletions, ch.change_type
           ORDER BY c.timestamp DESC
           LIMIT $lim""",
        {"p": path, "lim": limit},
    )
    return [
        {
            "type": "commit",
            "hash": r[0],
            "timestamp": r[1],
            "author": r[2],
            "message": r[3],
            "additions": r[4] or 0,
            "deletions": r[5] or 0,
            "change_type": r[6] or "M",
            "files": [path],
        }
        for r in rows
    ]


def _past_for_module(store: GraphStore, prefix: str, limit: int) -> list[dict]:
    """Aggregate per-file Change rows into per-commit entries.

    LadybugDB has a quirk where sum() returned 0 when combined with collect()
    in the same WITH clause, so aggregation is done in Python instead.
    """
    norm = prefix.rstrip("/") + "/"
    rows = store.query(
        """MATCH (c:Commit)-[:INCLUDES]->(ch:Change)
           WHERE ch.file_path STARTS WITH $prefix
           RETURN c.hash, c.timestamp, c.author, c.message,
                  ch.file_path, ch.additions, ch.deletions
           ORDER BY c.timestamp DESC""",
        {"prefix": norm},
    )
    by_commit: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        h = r[0]
        if h not in by_commit:
            by_commit[h] = {
                "type": "commit",
                "hash": h,
                "timestamp": r[1],
                "author": r[2],
                "message": r[3],
                "additions": 0,
                "deletions": 0,
                "files": [],
            }
            order.append(h)
        entry = by_commit[h]
        entry["additions"] += int(r[5] or 0)
        entry["deletions"] += int(r[6] or 0)
        if r[4] and r[4] not in entry["files"]:
            entry["files"].append(r[4])
    return [by_commit[h] for h in order[:limit]]


def _past_for_symbol(
    store: GraphStore, name: str, kind: str, limit: int
) -> list[dict]:
    rel = "AFFECTS_FUNC" if kind == "function" else "AFFECTS_CLASS"
    label = "Function" if kind == "function" else "Class"
    rows = store.query(
        f"""MATCH (c:Commit)-[:INCLUDES]->(ch:Change)-[:{rel}]->(sym:{label})
            WHERE sym.name = $n
            RETURN c.hash, c.timestamp, c.author, c.message,
                   ch.file_path, ch.additions, ch.deletions
            ORDER BY c.timestamp DESC
            LIMIT $lim""",
        {"n": name, "lim": limit},
    )
    # Collapse to one row per commit, gathering all files affected
    by_commit: dict[str, dict] = {}
    for r in rows:
        h = r[0]
        if h not in by_commit:
            by_commit[h] = {
                "type": "commit",
                "hash": r[0],
                "timestamp": r[1],
                "author": r[2],
                "message": r[3],
                "additions": 0,
                "deletions": 0,
                "files": [],
            }
        entry = by_commit[h]
        entry["additions"] += r[5] or 0
        entry["deletions"] += r[6] or 0
        if r[4] and r[4] not in entry["files"]:
            entry["files"].append(r[4])
    return list(by_commit.values())


def _symbol_file(store: GraphStore, name: str, kind: str) -> str | None:
    """Find the file path for the first symbol with this name."""
    label = "Function" if kind == "function" else "Class"
    rows = store.query(
        f"MATCH (s:{label}) WHERE s.name = $n RETURN s.file_path LIMIT 1",
        {"n": name},
    )
    return rows[0][0] if rows else None


# ---------------------------------------------------------------------------
# Future events (plans)
# ---------------------------------------------------------------------------

ACTIVE_STATUSES = ("draft", "active", "in_progress")


def _plan_ids_for_file(store: GraphStore, path: str) -> list[str]:
    rows = store.query(
        """MATCH (p:Plan)-[:TARGETS_FILE]->(f:File {path: $p})
           WHERE p.status IN $statuses
           RETURN DISTINCT p.id""",
        {"p": path, "statuses": list(ACTIVE_STATUSES)},
    )
    return [r[0] for r in rows]


def _plan_ids_for_module(store: GraphStore, prefix: str) -> list[str]:
    norm = prefix.rstrip("/") + "/"
    bare = prefix.rstrip("/")
    direct = store.query(
        """MATCH (p:Plan)-[:TARGETS_MODULE]->(m:Module {path: $p})
           WHERE p.status IN $statuses
           RETURN DISTINCT p.id""",
        {"p": bare, "statuses": list(ACTIVE_STATUSES)},
    )
    via_files = store.query(
        """MATCH (p:Plan)-[:TARGETS_FILE]->(f:File)
           WHERE (f.path STARTS WITH $prefix OR f.path = $bare)
             AND p.status IN $statuses
           RETURN DISTINCT p.id""",
        {"prefix": norm, "bare": bare, "statuses": list(ACTIVE_STATUSES)},
    )
    seen: list[str] = []
    for r in list(direct) + list(via_files):
        if r[0] not in seen:
            seen.append(r[0])
    return seen


def _plan_ids_for_symbol(store: GraphStore, name: str, kind: str) -> list[str]:
    rel = "TARGETS_FUNC" if kind == "function" else "TARGETS_CLASS"
    label = "Function" if kind == "function" else "Class"
    direct = store.query(
        f"""MATCH (p:Plan)-[:{rel}]->(s:{label})
            WHERE s.name = $n AND p.status IN $statuses
            RETURN DISTINCT p.id""",
        {"n": name, "statuses": list(ACTIVE_STATUSES)},
    )
    file_path = _symbol_file(store, name, kind)
    via_file: list[list[Any]] = []
    if file_path:
        via_file = store.query(
            """MATCH (p:Plan)-[:TARGETS_FILE]->(f:File {path: $p})
               WHERE p.status IN $statuses
               RETURN DISTINCT p.id""",
            {"p": file_path, "statuses": list(ACTIVE_STATUSES)},
        )
    seen: list[str] = []
    for r in list(direct) + list(via_file):
        if r[0] not in seen:
            seen.append(r[0])
    return seen


def _enrich_plans(store: GraphStore, plan_ids: list[str]) -> list[dict]:
    if not plan_ids:
        return []
    mgr = PlanManager(store)
    plans: list[dict] = []
    for pid in plan_ids:
        plan = mgr.get_plan(pid)
        if plan is None:
            continue
        plan["type"] = "plan"
        plans.append(plan)
    return plans


# ---------------------------------------------------------------------------
# Neighbors
# ---------------------------------------------------------------------------

def _co_changes_for_path(store: GraphStore, path: str, limit: int = 5) -> list[dict]:
    rows = store.query(
        """MATCH (f:File {path: $p})-[r:CO_CHANGES_WITH]->(other:File)
           RETURN other.path, r.count
           ORDER BY r.count DESC
           LIMIT $lim""",
        {"p": path, "lim": limit},
    )
    return [{"file": r[0], "count": r[1]} for r in rows]


def _co_changes_for_module(store: GraphStore, prefix: str, limit: int = 5) -> list[dict]:
    norm = prefix.rstrip("/") + "/"
    rows = store.query(
        """MATCH (f:File)-[r:CO_CHANGES_WITH]->(other:File)
           WHERE f.path STARTS WITH $prefix
             AND NOT (other.path STARTS WITH $prefix)
           RETURN other.path, sum(r.count) AS total
           ORDER BY total DESC
           LIMIT $lim""",
        {"prefix": norm, "lim": limit},
    )
    return [{"file": r[0], "count": r[1]} for r in rows]


def _callers_for_symbol(store: GraphStore, name: str, depth: int = 4, limit: int = 10) -> list[dict]:
    rows = store.query(
        f"""MATCH (target:Function {{name: $n}})<-[:CALLS*1..{depth}]-(caller:Function)
            RETURN DISTINCT caller.name, caller.file_path
            LIMIT $lim""",
        {"n": name, "lim": limit},
    )
    return [{"name": r[0], "file": r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def get_timeline(
    store: GraphStore,
    target: str,
    *,
    limit: int = 20,
    include_neighbors: bool = True,
) -> dict:
    """Build the unified timeline structure for a target.

    Returns a dict with keys: target, summary, past, future, co_changes, callers.
    If the target can't be resolved, target is None and all lists are empty.
    """
    resolved = resolve_target(store, target)
    if resolved is None:
        return {
            "target": None,
            "query": target,
            "summary": {},
            "past": [],
            "future": [],
            "co_changes": [],
            "callers": [],
        }

    kind = resolved["kind"]
    fallback: str | None = None

    # ---- Past ----
    if kind == "file":
        past = _past_for_file(store, resolved["path"], limit)
    elif kind == "module":
        past = _past_for_module(store, resolved["path"], limit)
    else:
        past = _past_for_symbol(store, resolved["name"], kind, limit)
        if not past:
            file_path = _symbol_file(store, resolved["name"], kind)
            if file_path:
                past = _past_for_file(store, file_path, limit)
                if past:
                    fallback = "file"

    # ---- Future ----
    if kind == "file":
        plan_ids = _plan_ids_for_file(store, resolved["path"])
    elif kind == "module":
        plan_ids = _plan_ids_for_module(store, resolved["path"])
    else:
        plan_ids = _plan_ids_for_symbol(store, resolved["name"], kind)
    future = _enrich_plans(store, plan_ids)

    # ---- Neighbors ----
    co_changes: list[dict] = []
    callers: list[dict] = []
    if include_neighbors:
        if kind == "file":
            co_changes = _co_changes_for_path(store, resolved["path"])
        elif kind == "module":
            co_changes = _co_changes_for_module(store, resolved["path"])
        else:
            file_path = _symbol_file(store, resolved["name"], kind)
            if file_path:
                co_changes = _co_changes_for_path(store, file_path)
            if kind == "function":
                callers = _callers_for_symbol(store, resolved["name"])

    pending = sum(
        1
        for p in future
        for i in p.get("intents") or []
        if i.get("status") in ("draft", "in_progress", "active")
    )

    return {
        "target": {**resolved, "fallback": fallback},
        "query": target,
        "summary": {
            "past_commits": len(past),
            "active_plans": len(future),
            "pending_intents": pending,
            "co_changes": len(co_changes),
            "callers": len(callers),
        },
        "past": past,
        "future": future,
        "co_changes": co_changes,
        "callers": callers,
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_json(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def _progress_bar(pct: int, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _short_date(ts: str | None) -> str:
    if not ts:
        return ""
    # Most timestamps are ISO 8601; take the date portion
    return str(ts).split("T")[0].split(" ")[0]


def format_markdown(data: dict) -> str:
    target = data.get("target")
    if target is None:
        return f"(no timeline data for '{data.get('query', '')}' — target not found)"

    out: list[str] = []
    name = target["name"]
    kind = target["kind"].capitalize()
    summary = data["summary"]

    out.append(f"# Timeline: {name}")
    bits = [kind]
    bits.append(f"{summary['past_commits']} past commits")
    bits.append(f"{summary['active_plans']} active plans")
    if summary["pending_intents"]:
        bits.append(f"{summary['pending_intents']} pending intents")
    out.append("**" + " · ".join(bits) + "**")
    if target.get("fallback"):
        out.append(f"_(symbol history empty — falling back to {target['fallback']}-level view)_")
    out.append("")

    # Future
    if data["future"]:
        out.append("## Future")
        for plan in data["future"]:
            progress = plan.get("progress") or {}
            pct = progress.get("pct", 0)
            done = progress.get("completed", 0)
            total = progress.get("total", 0)
            bar = _progress_bar(pct) if total else ""
            header = f"### {plan['title']} — {plan['status']}"
            if total:
                header += f" · {done}/{total} ({pct}%) {bar}"
            out.append(header)
            if plan.get("description"):
                out.append(plan["description"].strip())
            next_id = (plan.get("next_intent") or {}).get("id")
            for intent in plan.get("intents") or []:
                status = intent.get("status", "draft")
                check = "x" if status == "completed" else " "
                marker = " ← next" if intent.get("id") == next_id else ""
                out.append(f"- [{check}] {intent.get('description', '')}{marker}")
            if plan.get("blocked"):
                blockers = ", ".join(
                    d.get("title", d.get("id", "?"))
                    for d in plan.get("depends_on") or []
                    if d.get("status") != "completed"
                )
                if blockers:
                    out.append(f"_blocked by: {blockers}_")
            out.append("")

    # Past
    past = data["past"]
    if past:
        n_shown = len(past)
        total_known = summary["past_commits"]
        header = "## Past"
        if n_shown < total_known:
            header += f" (showing {n_shown} of {total_known})"
        out.append(header)
        for c in past:
            short_hash = (c.get("hash") or "")[:7]
            date = _short_date(c.get("timestamp"))
            author = c.get("author") or ""
            msg = (c.get("message") or "").strip().splitlines()[0] if c.get("message") else ""
            adds = c.get("additions", 0)
            dels = c.get("deletions", 0)
            stats = f"+{adds} -{dels}" if adds or dels else ""
            line = f"- `{short_hash}` {date} {author} — {msg}"
            if stats:
                line += f" ({stats})"
            out.append(line)
        out.append("")

    # Co-change neighbors
    if data["co_changes"]:
        out.append("## Co-change neighbors")
        for n in data["co_changes"]:
            out.append(f"- {n['file']} ({n['count']}×)")
        out.append("")

    # Callers
    if data["callers"]:
        out.append("## Callers (blast radius)")
        for c in data["callers"]:
            out.append(f"- {c['name']} ({c['file']})")
        out.append("")

    if not data["future"] and not past:
        out.append("_(no past commits or active plans for this target)_")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_HTML_CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #0f172a;
  color: #e2e8f0;
  margin: 0;
  padding: 2rem;
  line-height: 1.5;
}
.wrap { max-width: 880px; margin: 0 auto; }
header h1 { margin: 0 0 .25rem; font-size: 1.6rem; }
header .meta { color: #94a3b8; font-size: .9rem; margin-bottom: 2rem; }
header .meta .chip {
  display: inline-block;
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 4px;
  padding: 1px 8px;
  margin-right: 6px;
}
section { margin-bottom: 2rem; }
h2 {
  font-size: 1rem;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: #94a3b8;
  border-bottom: 1px solid #1e293b;
  padding-bottom: .35rem;
}
.now-line {
  height: 1px;
  background: linear-gradient(to right, transparent, #475569, transparent);
  margin: 2rem 0;
  position: relative;
  text-align: center;
}
.now-line span {
  position: relative;
  top: -.7rem;
  background: #0f172a;
  padding: 0 12px;
  color: #94a3b8;
  font-size: .75rem;
  letter-spacing: .15em;
  text-transform: uppercase;
}
.plan {
  border-left: 3px solid #2563eb;
  background: #1e293b;
  padding: .75rem 1rem;
  border-radius: 0 6px 6px 0;
  margin-bottom: 1rem;
}
.plan.draft { border-left-color: #9ca3af; }
.plan.completed { border-left-color: #16a34a; }
.plan.abandoned { border-left-color: #6b7280; opacity: .55; }
.plan h3 { margin: 0 0 .5rem; font-size: 1.05rem; }
.plan .badge {
  font-size: .7rem;
  text-transform: uppercase;
  letter-spacing: .05em;
  background: #2563eb;
  color: white;
  padding: 1px 8px;
  border-radius: 999px;
  margin-left: .5rem;
  vertical-align: middle;
}
.plan.draft .badge { background: #475569; }
.plan.completed .badge { background: #16a34a; }
.plan .progress {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .85rem;
  color: #94a3b8;
  margin-bottom: .5rem;
}
.plan .description {
  font-size: .9rem;
  color: #cbd5e1;
  margin-bottom: .5rem;
}
.intents { list-style: none; padding: 0; margin: 0; }
.intents li {
  font-size: .9rem;
  padding: 2px 0;
  color: #cbd5e1;
}
.intents li.done { color: #6b7280; text-decoration: line-through; }
.intents li.next { color: #fbbf24; font-weight: 600; }
.intents li::before {
  display: inline-block;
  width: 1.4rem;
  text-align: center;
  margin-right: .25rem;
  color: #475569;
}
.intents li.done::before { content: "✓"; color: #16a34a; }
.intents li.todo::before { content: "○"; }
.intents li.next::before { content: "→"; color: #fbbf24; }
.commit {
  display: grid;
  grid-template-columns: 4.5rem 6rem 1fr auto;
  gap: .75rem;
  align-items: baseline;
  padding: .35rem 0;
  border-bottom: 1px solid #1e293b;
  font-size: .9rem;
}
.commit:last-child { border-bottom: none; }
.commit .hash {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .8rem;
  color: #94a3b8;
}
.commit .date { color: #94a3b8; font-size: .8rem; }
.commit .author {
  font-size: .75rem;
  color: #94a3b8;
}
.commit .msg { color: #e2e8f0; }
.commit .stats {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .75rem;
  white-space: nowrap;
}
.commit .add { color: #16a34a; }
.commit .del { color: #dc2626; }
.neighbors { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }
.neighbors h2 { margin-top: 0; }
.neighbor-list { list-style: none; padding: 0; margin: 0; font-size: .85rem; }
.neighbor-list li {
  padding: 3px 0;
  color: #cbd5e1;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.neighbor-list .meta { color: #94a3b8; margin-left: .5rem; font-size: .75rem; }
.empty { color: #64748b; font-style: italic; padding: 1rem 0; }
"""


def _intent_class(intent: dict, next_id: str | None) -> str:
    status = intent.get("status", "draft")
    if status == "completed":
        return "done"
    if intent.get("id") == next_id:
        return "next"
    return "todo"


def render_html(data: dict) -> str:
    target = data.get("target")
    if target is None:
        return _html_doc(
            "Timeline (not found)",
            f"<p class='empty'>No timeline data for <code>{escape(data.get('query', ''))}</code> — target not found.</p>",
        )

    name = escape(target["name"])
    kind = escape(target["kind"].capitalize())
    summary = data["summary"]

    parts: list[str] = []

    # Header
    chips = [
        f"<span class='chip'>{kind}</span>",
        f"<span class='chip'>{summary['past_commits']} past</span>",
        f"<span class='chip'>{summary['active_plans']} active plans</span>",
    ]
    if summary["pending_intents"]:
        chips.append(f"<span class='chip'>{summary['pending_intents']} pending intents</span>")
    fallback_note = ""
    if target.get("fallback"):
        fb = escape(target["fallback"])
        fallback_note = f"<div class='meta'>Symbol history empty — showing {fb}-level view.</div>"

    parts.append(
        f"<header><h1>Timeline: {name}</h1>"
        f"<div class='meta'>{''.join(chips)}</div>{fallback_note}</header>"
    )

    # Future section
    parts.append("<section><h2>Future</h2>")
    if data["future"]:
        for plan in data["future"]:
            status = escape(plan.get("status", "draft"))
            title = escape(plan.get("title", ""))
            description = escape((plan.get("description") or "").strip())
            progress = plan.get("progress") or {}
            pct = progress.get("pct", 0)
            done = progress.get("completed", 0)
            total = progress.get("total", 0)
            bar = _progress_bar(pct) if total else ""
            progress_html = (
                f"<div class='progress'>{done}/{total} ({pct}%) {bar}</div>"
                if total
                else ""
            )
            next_id = (plan.get("next_intent") or {}).get("id")
            intents_html_parts: list[str] = []
            for intent in plan.get("intents") or []:
                cls = _intent_class(intent, next_id)
                desc = escape(intent.get("description", ""))
                intents_html_parts.append(f"<li class='{cls}'>{desc}</li>")
            intents_html = (
                "<ul class='intents'>" + "".join(intents_html_parts) + "</ul>"
                if intents_html_parts
                else ""
            )
            desc_html = (
                f"<div class='description'>{description}</div>" if description else ""
            )
            parts.append(
                f"<div class='plan {status}'>"
                f"<h3>{title}<span class='badge'>{status}</span></h3>"
                f"{progress_html}{desc_html}{intents_html}"
                f"</div>"
            )
    else:
        parts.append("<p class='empty'>No active plans for this target.</p>")
    parts.append("</section>")

    # Now line
    parts.append("<div class='now-line'><span>now</span></div>")

    # Past section
    parts.append("<section><h2>Past</h2>")
    if data["past"]:
        for c in data["past"]:
            short_hash = escape((c.get("hash") or "")[:7])
            date = escape(_short_date(c.get("timestamp")))
            author = escape(c.get("author") or "")
            msg_first = ""
            if c.get("message"):
                msg_first = escape(c["message"].strip().splitlines()[0])
            adds = c.get("additions", 0)
            dels = c.get("deletions", 0)
            stats_html = ""
            if adds or dels:
                stats_html = (
                    f"<span class='stats'>"
                    f"<span class='add'>+{adds}</span> "
                    f"<span class='del'>-{dels}</span>"
                    f"</span>"
                )
            else:
                stats_html = "<span class='stats'></span>"
            parts.append(
                f"<div class='commit'>"
                f"<span class='hash'>{short_hash}</span>"
                f"<span class='date'>{date}</span>"
                f"<span class='msg'>{msg_first} <span class='author'>· {author}</span></span>"
                f"{stats_html}"
                f"</div>"
            )
    else:
        parts.append("<p class='empty'>No past commits for this target.</p>")
    parts.append("</section>")

    # Neighbors
    if data["co_changes"] or data["callers"]:
        parts.append("<section class='neighbors'>")
        if data["co_changes"]:
            parts.append("<div><h2>Co-change neighbors</h2><ul class='neighbor-list'>")
            for n in data["co_changes"]:
                parts.append(
                    f"<li>{escape(n['file'])}<span class='meta'>{n['count']}×</span></li>"
                )
            parts.append("</ul></div>")
        if data["callers"]:
            parts.append("<div><h2>Callers (blast radius)</h2><ul class='neighbor-list'>")
            for c in data["callers"]:
                parts.append(
                    f"<li>{escape(c['name'])}<span class='meta'>{escape(c['file'])}</span></li>"
                )
            parts.append("</ul></div>")
        parts.append("</section>")

    return _html_doc(f"Timeline: {target['name']}", "".join(parts))


def _html_doc(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        "<html lang='en'>\n<head>\n"
        "<meta charset='utf-8'>\n"
        f"<title>{escape(title)}</title>\n"
        f"<style>{_HTML_CSS}</style>\n"
        "</head>\n<body>\n"
        f"<div class='wrap'>{body}</div>\n"
        "</body>\n</html>\n"
    )
