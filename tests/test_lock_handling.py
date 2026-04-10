"""Tests for the LadybugDB lock handling: friendly errors and write serialization."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from cartographer.storage.store import GraphStore, DatabaseLockedError


def _spawn_lock_holder(db_path) -> subprocess.Popen:
    """Spawn a subprocess that opens the DB and waits for stdin to close."""
    code = textwrap.dedent(
        f"""
        import sys
        import real_ladybug as lbug
        db = lbug.Database({str(db_path)!r})
        conn = lbug.Connection(db)
        sys.stdout.write("READY\\n")
        sys.stdout.flush()
        sys.stdin.read()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "READY", f"holder did not become ready: {line!r}"
    return proc


# ---------------------------------------------------------------------------
# Friendly DatabaseLockedError
# ---------------------------------------------------------------------------

class TestDatabaseLockedError:
    def test_open_while_other_process_holds_lock_raises_friendly_error(self, tmp_path):
        db_path = tmp_path / "lockdb"
        # First open from this process to materialize the directory
        warmup = GraphStore(db_path)
        warmup.open()
        warmup.close()

        holder = _spawn_lock_holder(db_path)
        try:
            store = GraphStore(db_path)
            with pytest.raises(DatabaseLockedError) as exc:
                store.open()
            msg = str(exc.value)
            # Friendly content checks
            assert str(db_path) in msg
            assert "cartographer-mcp" in msg
            assert "MCP" in msg
        finally:
            holder.stdin.close()
            holder.wait(timeout=5.0)

    def test_open_succeeds_after_holder_releases(self, tmp_path):
        db_path = tmp_path / "lockdb2"
        warmup = GraphStore(db_path)
        warmup.open()
        warmup.close()

        holder = _spawn_lock_holder(db_path)
        holder.stdin.close()
        holder.wait(timeout=5.0)

        store = GraphStore(db_path)
        store.open()
        store.close()


# ---------------------------------------------------------------------------
# Write lock serializes concurrent threads
# ---------------------------------------------------------------------------

class TestWriteLockSerializesThreads:
    def test_typed_helpers_serialize_under_concurrent_writes(self, tmp_path):
        """Without the write lock, two threads writing via typed helpers
        would fail with 'Only one write transaction at a time'.
        With the lock they all succeed."""
        s = GraphStore(tmp_path / "writedb")
        s.open()
        s.ensure_schema(layers=("structure",))

        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                for i in range(10):
                    s.upsert_file(
                        f"src/t{idx}_{i}.py", "py", f"h{idx}{i}", "2026-01-01"
                    )
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        rows = s.query("MATCH (f:File) RETURN count(f)")
        assert rows[0][0] == 5 * 10
        s.close()

    def test_execute_write_serializes_raw_writes(self, tmp_path):
        """PlanManager-style raw writes via execute_write must also serialize."""
        s = GraphStore(tmp_path / "writedb2")
        s.open()
        # Planning rel tables reference File/Module/Class/Function — need
        # the structure layer in place for the schema to load.
        s.ensure_schema(layers=("structure", "planning"))

        errors: list[Exception] = []

        def writer(idx: int) -> None:
            try:
                for i in range(10):
                    s.execute_write(
                        """CREATE (p:Plan {
                            id: $id, title: $t, description: '', status: 'draft',
                            created_at: '', updated_at: '', author: ''
                        })""",
                        {"id": f"p{idx}_{i}", "t": f"plan {idx}/{i}"},
                    )
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        rows = s.query("MATCH (p:Plan) RETURN count(p)")
        assert rows[0][0] == 5 * 10
        s.close()


# ---------------------------------------------------------------------------
# Reads do not block on the write lock
# ---------------------------------------------------------------------------

class TestReadsDoNotBlockOnLock:
    def test_read_proceeds_while_write_lock_held(self, tmp_path):
        """Holding the write lock in one thread must not prevent another
        thread from running a query() call."""
        s = GraphStore(tmp_path / "readdb")
        s.open()
        s.ensure_schema(layers=("structure",))
        s.upsert_file("src/a.py", "py", "h", "2026-01-01")

        holding = threading.Event()
        release = threading.Event()
        read_done = threading.Event()
        read_count: list[int] = []

        def hold_lock() -> None:
            with s.write_lock:
                holding.set()
                # Wait until reader confirms it got through
                release.wait(timeout=2.0)

        def reader() -> None:
            holding.wait(timeout=2.0)
            rows = s.query("MATCH (f:File) RETURN count(f)")
            read_count.append(rows[0][0])
            read_done.set()
            release.set()

        t1 = threading.Thread(target=hold_lock)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()

        # Reader should complete promptly even though writer is holding the lock
        assert read_done.wait(timeout=3.0), "reader was blocked by write lock"
        assert read_count == [1]

        t1.join()
        t2.join()
        s.close()
