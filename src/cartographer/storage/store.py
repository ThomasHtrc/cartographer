"""Graph store: connection management and core operations over LadybugDB."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import real_ladybug as lbug

from . import schema


class DatabaseLockedError(RuntimeError):
    """Raised when the LadybugDB database is locked by another process.

    LadybugDB enforces a single-process exclusive lock on the database
    directory. When this error is raised, another graph-context process
    (typically the MCP server) holds the lock.
    """


def _format_lock_error(db_path: Path) -> str:
    return (
        f"The graph-context database at {db_path} is locked by another process.\n"
        f"\n"
        f"This usually means a graph-context-mcp server is running for this repo.\n"
        f"  - Use the MCP tools to query/modify the graph (they share the lock).\n"
        f"  - Or stop the MCP server first, then re-run this command.\n"
        f"\n"
        f"If no MCP server is running, check for a stale lock file at {db_path}/.lock."
    )


class GraphStore:
    """Manages a LadybugDB graph database for a project."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db: lbug.Database | None = None
        self._conn: lbug.Connection | None = None
        # Serializes write transactions across threads sharing this store.
        # LadybugDB allows only one write transaction at a time per Database;
        # writes from multiple threads (e.g. MCP request handlers + the
        # in-process watcher) must be serialized to avoid
        # "Only one write transaction at a time is allowed" errors.
        self._write_lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------------

    def open(self) -> None:
        """Open (or create) the database.

        Raises:
            DatabaseLockedError: if another process holds the LadybugDB lock.
        """
        try:
            self._db = lbug.Database(str(self._db_path))
            self._conn = lbug.Connection(self._db)
        except RuntimeError as e:
            msg = str(e)
            if "Could not set lock on file" in msg or "lock" in msg.lower():
                raise DatabaseLockedError(_format_lock_error(self._db_path)) from e
            raise

    def close(self) -> None:
        """Close the database connection."""
        self._conn = None
        self._db = None

    def __enter__(self) -> GraphStore:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> lbug.Connection:
        if self._conn is None:
            raise RuntimeError("Store not open — call .open() or use as context manager")
        return self._conn

    # -- schema ---------------------------------------------------------------

    def ensure_schema(self, layers: tuple[str, ...] = ("structure", "history", "planning")) -> None:
        """Create all tables for the requested layers (idempotent)."""
        stmts: list[str] = []
        if "structure" in layers:
            stmts += schema.STRUCTURE_NODE_TABLES + schema.STRUCTURE_REL_TABLES
        if "history" in layers:
            stmts += schema.HISTORY_NODE_TABLES + schema.HISTORY_REL_TABLES
        if "planning" in layers:
            stmts += schema.PLANNING_NODE_TABLES + schema.PLANNING_REL_TABLES
        for stmt in stmts:
            self.conn.execute(stmt)

    # -- query helpers --------------------------------------------------------

    def execute(self, cypher: str, params: dict[str, Any] | None = None) -> lbug.QueryResult:
        """Execute a Cypher statement and return the raw QueryResult.

        NOTE: this method does NOT acquire the write lock — use it for reads,
        or wrap write statements in a `with store.write_lock:` block /
        call `execute_write()` instead so that concurrent writers are
        serialized correctly.
        """
        if params:
            return self.conn.execute(cypher, params)
        return self.conn.execute(cypher)

    def execute_write(self, cypher: str, params: dict[str, Any] | None = None) -> lbug.QueryResult:
        """Execute a write Cypher statement under the store's write lock.

        Use this for any raw CREATE/MERGE/SET/DELETE statement issued
        from outside the typed write helpers (e.g. PlanManager).
        """
        with self._write_lock:
            return self.execute(cypher, params)

    @property
    def write_lock(self) -> threading.RLock:
        """Re-entrant lock guarding all write transactions on this store.

        Acquire when issuing multiple raw write statements that must run
        as a single logical operation.
        """
        return self._write_lock

    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        """Execute a Cypher query and return all rows as lists."""
        result = self.execute(cypher, params)
        rows: list[list[Any]] = []
        while result.has_next():
            rows.append(result.get_next())
        return rows

    def query_one(self, cypher: str, params: dict[str, Any] | None = None) -> list[Any] | None:
        """Execute a query and return the first row, or None."""
        result = self.execute(cypher, params)
        if result.has_next():
            return result.get_next()
        return None

    # -- bulk write helpers ---------------------------------------------------

    def clear_history(self) -> None:
        """Remove all history nodes (Commit, Change) and their edges.

        Used for clean re-indexing of git history (Layer 2).
        """
        with self._write_lock:
            self.conn.execute("MATCH (ch:Change) DETACH DELETE ch")
            self.conn.execute("MATCH (c:Commit) DETACH DELETE c")

    def clear_file(self, file_path: str) -> None:
        """Remove all nodes and edges originating from a given source file.

        This is used for incremental re-indexing: delete everything from the
        changed file, then re-extract and re-insert.
        """
        with self._write_lock:
            # Delete edges first (referencing nodes from this file), then nodes.
            # We delete symbols whose id starts with the file path.
            for node_table in ("Function", "Class", "Type", "Variable", "Endpoint", "Event", "Schema"):
                # Delete the node — LadybugDB cascades edge deletion.
                self.conn.execute(
                    f"MATCH (n:{node_table}) WHERE n.file_path = $fp DETACH DELETE n",
                    {"fp": file_path},
                )
            # The File node itself: detach-delete removes its edges too.
            self.conn.execute(
                "MATCH (f:File {path: $fp}) DETACH DELETE f",
                {"fp": file_path},
            )

    def upsert_file(self, path: str, lang: str, hash_: str, last_modified: str) -> None:
        """Create or update a File node."""
        with self._write_lock:
            self.conn.execute(
                "MERGE (f:File {path: $p}) SET f.lang = $lang, f.hash = $hash, f.last_modified = $lm",
                {"p": path, "lang": lang, "hash": hash_, "lm": last_modified},
            )

    def upsert_module(self, path: str, name: str) -> None:
        """Create or update a Module node."""
        with self._write_lock:
            self.conn.execute(
                "MERGE (m:Module {path: $p}) SET m.name = $name",
                {"p": path, "name": name},
            )

    def create_function(
        self,
        id_: str,
        name: str,
        file_path: str,
        line_start: int,
        line_end: int,
        signature: str = "",
        visibility: str = "public",
        is_method: bool = False,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """CREATE (n:Function {
                    id: $id, name: $name, file_path: $fp,
                    line_start: $ls, line_end: $le,
                    signature: $sig, visibility: $vis, is_method: $im
                })""",
                {
                    "id": id_, "name": name, "fp": file_path,
                    "ls": line_start, "le": line_end,
                    "sig": signature, "vis": visibility, "im": is_method,
                },
            )

    def create_class(
        self,
        id_: str,
        name: str,
        file_path: str,
        line_start: int,
        line_end: int,
        visibility: str = "public",
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """CREATE (n:Class {
                    id: $id, name: $name, file_path: $fp,
                    line_start: $ls, line_end: $le, visibility: $vis
                })""",
                {
                    "id": id_, "name": name, "fp": file_path,
                    "ls": line_start, "le": line_end, "vis": visibility,
                },
            )

    def create_type(
        self,
        id_: str,
        name: str,
        file_path: str,
        line_start: int,
        line_end: int,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """CREATE (n:Type {
                    id: $id, name: $name, file_path: $fp,
                    line_start: $ls, line_end: $le
                })""",
                {"id": id_, "name": name, "fp": file_path, "ls": line_start, "le": line_end},
            )

    def create_variable(
        self,
        id_: str,
        name: str,
        file_path: str,
        line_start: int,
        line_end: int,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """CREATE (n:Variable {
                    id: $id, name: $name, file_path: $fp,
                    line_start: $ls, line_end: $le
                })""",
                {"id": id_, "name": name, "fp": file_path, "ls": line_start, "le": line_end},
            )

    # -- edge creation helpers ------------------------------------------------

    def create_edge(self, rel_type: str, from_table: str, from_id: str, to_table: str, to_id: str, props: dict[str, Any] | None = None) -> None:
        """Create a relationship between two nodes by their primary keys.

        from_id/to_id are matched against the PK field:
          - File, Module: path
          - Commit: hash
          - All others: id
        """
        pk_map = {"File": "path", "Module": "path", "Commit": "hash"}
        from_pk = pk_map.get(from_table, "id")
        to_pk = pk_map.get(to_table, "id")

        prop_clause = ""
        params: dict[str, Any] = {"fid": from_id, "tid": to_id}
        if props:
            assignments = ", ".join(f"r.{k} = ${k}" for k in props)
            prop_clause = f" SET {assignments}"
            params.update(props)

        with self._write_lock:
            self.conn.execute(
                f"MATCH (a:{from_table} {{{from_pk}: $fid}}), (b:{to_table} {{{to_pk}: $tid}}) "
                f"MERGE (a)-[r:{rel_type}]->(b){prop_clause}",
                params,
            )
