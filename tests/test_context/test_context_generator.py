"""Tests for the context generator (ranker, assembler, formatter)."""

import pytest

from cartographer.storage.store import GraphStore
from cartographer.context.ranker import Ranker, RankedNode
from cartographer.context.assembler import Assembler, estimate_tokens
from cartographer.context import formatter


@pytest.fixture()
def store(tmp_path):
    """Create a store with a small graph for testing."""
    db_path = tmp_path / "test_db"
    s = GraphStore(db_path)
    s.open()
    s.ensure_schema()
    yield s
    s.close()


@pytest.fixture()
def populated_store(store):
    """Populate the store with a realistic small graph."""
    # Files
    store.upsert_file("src/auth.py", "python", "aaa", "2024-01-01")
    store.upsert_file("src/models.py", "python", "bbb", "2024-01-01")
    store.upsert_file("src/routes.py", "python", "ccc", "2024-01-01")
    store.upsert_file("src/utils.py", "python", "ddd", "2024-01-01")

    # Functions
    store.create_function("src/auth.py::authenticate", "authenticate", "src/auth.py", 1, 20,
                          signature="def authenticate(username: str, password: str) -> User:")
    store.create_function("src/auth.py::validate_token", "validate_token", "src/auth.py", 22, 35,
                          signature="def validate_token(token: str) -> bool:")
    store.create_function("src/routes.py::login_handler", "login_handler", "src/routes.py", 1, 15,
                          signature="def login_handler(request: Request) -> Response:")
    store.create_function("src/utils.py::hash_password", "hash_password", "src/utils.py", 1, 10,
                          signature="def hash_password(password: str) -> str:")

    # Classes
    store.create_class("src/models.py::User", "User", "src/models.py", 1, 30)
    store.create_class("src/models.py::Session", "Session", "src/models.py", 32, 50)

    # Edges: File -> symbols
    store.create_edge("CONTAINS_FUNC", "File", "src/auth.py", "Function", "src/auth.py::authenticate")
    store.create_edge("CONTAINS_FUNC", "File", "src/auth.py", "Function", "src/auth.py::validate_token")
    store.create_edge("CONTAINS_FUNC", "File", "src/routes.py", "Function", "src/routes.py::login_handler")
    store.create_edge("CONTAINS_FUNC", "File", "src/utils.py", "Function", "src/utils.py::hash_password")
    store.create_edge("CONTAINS_CLASS", "File", "src/models.py", "Class", "src/models.py::User")
    store.create_edge("CONTAINS_CLASS", "File", "src/models.py", "Class", "src/models.py::Session")

    # Edges: calls
    store.create_edge("CALLS", "Function", "src/routes.py::login_handler", "Function", "src/auth.py::authenticate")
    store.create_edge("CALLS", "Function", "src/auth.py::authenticate", "Function", "src/utils.py::hash_password")

    # Edges: imports
    store.create_edge("IMPORTS", "File", "src/routes.py", "File", "src/auth.py")
    store.create_edge("IMPORTS", "File", "src/auth.py", "File", "src/models.py")
    store.create_edge("IMPORTS", "File", "src/auth.py", "File", "src/utils.py")

    return store


# ---------------------------------------------------------------------------
# Ranker tests
# ---------------------------------------------------------------------------

class TestRanker:
    def test_rank_from_file_focal(self, populated_store):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["src/auth.py"])
        assert len(ranked) > 0
        # Auth file and its symbols should be ranked highest
        names = [r.name for r in ranked[:5]]
        assert "authenticate" in names or "src/auth.py" in names

    def test_rank_from_symbol_focal(self, populated_store):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["authenticate"])
        assert len(ranked) > 0
        # The focal function itself should have high rank
        top_names = [r.name for r in ranked[:3]]
        assert "authenticate" in top_names

    def test_rank_propagates_to_neighbors(self, populated_store):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["src/auth.py"])
        names = {r.name for r in ranked if r.score > 0}
        # Imported files and callers should appear
        assert "hash_password" in names or "src/utils.py" in names

    def test_rank_empty_focal(self, populated_store):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["nonexistent_symbol"])
        assert ranked == []

    def test_rank_max_results(self, populated_store):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["src/auth.py"], max_results=3)
        assert len(ranked) <= 3

    def test_rank_multiple_focal_points(self, populated_store):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["src/auth.py", "src/routes.py"])
        assert len(ranked) > 0
        top_files = {r.file_path for r in ranked[:6]}
        assert "src/auth.py" in top_files
        assert "src/routes.py" in top_files

    def test_rank_with_plan_boost(self, populated_store):
        # Create an active plan targeting auth.py
        populated_store.execute(
            """CREATE (p:Plan {
                id: 'plan1', title: 'Auth rewrite', description: '',
                status: 'active', created_at: '', updated_at: '', author: ''
            })"""
        )
        populated_store.create_edge("TARGETS_FILE", "Plan", "plan1", "File", "src/auth.py")

        ranker = Ranker(populated_store, plan_weight=0.3)
        ranked = ranker.rank(["src/routes.py"])
        # auth.py should get a plan boost
        auth_nodes = [r for r in ranked if r.file_path == "src/auth.py"]
        assert len(auth_nodes) > 0


# ---------------------------------------------------------------------------
# Assembler tests
# ---------------------------------------------------------------------------

class TestAssembler:
    def test_assemble_within_budget(self, tmp_path):
        nodes = [
            RankedNode("Function", "a.py::foo", "foo", "a.py", 0.9, 1, 10, "def foo():"),
            RankedNode("Function", "a.py::bar", "bar", "a.py", 0.7, 11, 20, "def bar():"),
            RankedNode("Function", "b.py::baz", "baz", "b.py", 0.5, 1, 10, "def baz():"),
        ]
        asm = Assembler(tmp_path)
        result = asm.assemble(nodes, budget=500)
        assert result.total_tokens <= 500
        assert result.files_included >= 1
        assert result.symbols_included >= 1

    def test_assemble_respects_budget(self, tmp_path):
        nodes = [
            RankedNode("Function", "a.py::foo", "foo", "a.py", 0.9, 1, 10, "def foo(a, b, c, d, e, f, g):"),
            RankedNode("Function", "a.py::bar", "bar", "a.py", 0.7, 11, 20, "def bar(a, b, c, d, e, f, g):"),
        ]
        asm = Assembler(tmp_path)
        # Very tight budget — should fit at most file header + 1 symbol
        result = asm.assemble(nodes, budget=10)
        assert result.total_tokens <= 10

    def test_assemble_empty_nodes(self, tmp_path):
        asm = Assembler(tmp_path)
        result = asm.assemble([], budget=4000)
        assert result.total_tokens == 0
        assert result.files_included == 0

    def test_assemble_groups_by_file(self, tmp_path):
        nodes = [
            RankedNode("Function", "a.py::foo", "foo", "a.py", 0.9, 1, 10, "def foo():"),
            RankedNode("Function", "b.py::bar", "bar", "b.py", 0.8, 1, 10, "def bar():"),
            RankedNode("Function", "a.py::baz", "baz", "a.py", 0.7, 11, 20, "def baz():"),
        ]
        asm = Assembler(tmp_path)
        result = asm.assemble(nodes, budget=2000)
        # a.py symbols should be grouped together
        file_order = []
        for item in result.items:
            if item.kind == "file_header" and item.file_path not in file_order:
                file_order.append(item.file_path)
        assert file_order == ["a.py", "b.py"]


class TestEstimateTokens:
    def test_basic_estimation(self):
        assert estimate_tokens("hello") >= 1
        assert estimate_tokens("a" * 100) == 25

    def test_empty_string(self):
        assert estimate_tokens("") == 1


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------

class TestFormatMarkdown:
    def _make_context(self):
        from cartographer.context.assembler import ContextItem, AssembledContext
        items = [
            ContextItem("file_header", "src/auth.py", "src/auth.py", "# src/auth.py", 5, 0.9),
            ContextItem("signature", "src/auth.py", "authenticate",
                        "  def authenticate(username: str) -> User:", 12, 0.85, 1, 20),
            ContextItem("file_header", "src/models.py", "src/models.py", "# src/models.py", 5, 0.6),
            ContextItem("signature", "src/models.py", "User", "  class User:", 4, 0.55, 1, 30),
        ]
        return AssembledContext(
            items=items, total_tokens=26, budget=4000,
            focal_points=["src/auth.py"], files_included=2, symbols_included=2,
        )

    def test_markdown_output(self):
        ctx = self._make_context()
        output = formatter.format_markdown(ctx)
        assert "# src/auth.py" in output
        assert "def authenticate" in output
        assert "class User:" in output
        assert "2 files" in output
        assert "2 symbols" in output

    def test_json_output(self):
        ctx = self._make_context()
        output = formatter.format_json(ctx)
        import json
        data = json.loads(output)
        assert data["budget"] == 4000
        assert data["files_included"] == 2
        assert len(data["items"]) == 4

    def test_annotated_output(self):
        ctx = self._make_context()
        output = formatter.format_annotated(ctx)
        assert "[score=0.85" in output
        assert "L1-20" in output


# ---------------------------------------------------------------------------
# Integration: Ranker + Assembler + Formatter
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_full_pipeline(self, populated_store, tmp_path):
        ranker = Ranker(populated_store)
        ranked = ranker.rank(["src/auth.py"])
        assert len(ranked) > 0

        asm = Assembler(tmp_path)
        assembled = asm.assemble(ranked, budget=4000, focal_points=["src/auth.py"])
        assert assembled.total_tokens > 0
        assert assembled.files_included >= 1

        md = formatter.format_markdown(assembled)
        assert "src/auth.py" in md

        js = formatter.format_json(assembled)
        import json
        data = json.loads(js)
        assert data["focal_points"] == ["src/auth.py"]

        ann = formatter.format_annotated(assembled)
        assert "[score=" in ann
