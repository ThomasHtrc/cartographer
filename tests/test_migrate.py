"""Tests for the `cartographer migrate` CLI command.

Covers detection, dry-run, full execution, idempotency, sibling-MCP-server
preservation, and the both-dirs-exist refusal case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cartographer.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_old_style_install(repo: Path, *, with_data: bool = True,
                            with_mcp: bool = True, with_claude: bool = True,
                            extra_mcp_server: bool = False) -> None:
    """Materialize an old-style graph-context install in `repo`."""
    if with_data:
        (repo / ".graph-context" / "db").mkdir(parents=True)
        (repo / ".graph-context" / "meta.json").write_text(
            '{"initialized": true, "last_commit": null}\n'
        )

    if with_mcp:
        servers = {
            "graph-context": {
                "command": "graph-context-mcp",
                "args": [],
                "env": {"GRAPH_CONTEXT_REPO": "."},
            }
        }
        if extra_mcp_server:
            servers["other-server"] = {"command": "other-mcp", "args": []}
        (repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}, indent=2) + "\n"
        )

    if with_claude:
        (repo / "CLAUDE.md").write_text(
            "# graph-context\n"
            "\n"
            "Use `graph-context index` to refresh. Data lives in `.graph-context/db/`.\n"
            "GRAPH_CONTEXT_REPO points at the repo. The MCP binary is graph-context-mcp.\n"
            "Set GRAPH_CONTEXT_MCP_AUTOWATCH=0 to disable the watcher.\n"
        )


def _invoke_migrate(repo: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(cli, ["--repo", str(repo), "migrate", *args])


# ---------------------------------------------------------------------------
# Detection / no-op
# ---------------------------------------------------------------------------

class TestMigrateDetection:
    def test_clean_repo_reports_already_migrated(self, tmp_path):
        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0
        assert "Already migrated" in result.output

    def test_already_migrated_repo_is_noop(self, tmp_path):
        # Brand-new style install
        (tmp_path / ".cartographer" / "db").mkdir(parents=True)
        (tmp_path / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "cartographer": {
                    "command": "cartographer-mcp",
                    "env": {"CARTOGRAPHER_REPO": "."},
                }
            }
        }, indent=2))
        (tmp_path / "CLAUDE.md").write_text("# cartographer\n")

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0
        assert "Already migrated" in result.output


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestMigrateDryRun:
    def test_dry_run_writes_nothing(self, tmp_path):
        _make_old_style_install(tmp_path)
        before_data = (tmp_path / ".graph-context").exists()
        before_mcp = (tmp_path / ".mcp.json").read_text()
        before_claude = (tmp_path / "CLAUDE.md").read_text()

        result = _invoke_migrate(tmp_path, "--dry-run")

        assert result.exit_code == 0
        assert "Dry run" in result.output
        assert "Migration plan" in result.output
        # Nothing changed on disk
        assert (tmp_path / ".graph-context").exists() == before_data
        assert not (tmp_path / ".cartographer").exists()
        assert (tmp_path / ".mcp.json").read_text() == before_mcp
        assert (tmp_path / "CLAUDE.md").read_text() == before_claude


# ---------------------------------------------------------------------------
# Real execution
# ---------------------------------------------------------------------------

class TestMigrateExecution:
    def test_renames_data_directory(self, tmp_path):
        _make_old_style_install(tmp_path, with_mcp=False, with_claude=False)

        result = _invoke_migrate(tmp_path)

        assert result.exit_code == 0
        assert not (tmp_path / ".graph-context").exists()
        assert (tmp_path / ".cartographer" / "db").is_dir()
        assert (tmp_path / ".cartographer" / "meta.json").read_text().startswith("{")

    def test_rewrites_mcp_json(self, tmp_path):
        _make_old_style_install(tmp_path, with_data=False, with_claude=False)

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0

        data = json.loads((tmp_path / ".mcp.json").read_text())
        servers = data["mcpServers"]
        assert "graph-context" not in servers
        assert "cartographer" in servers
        entry = servers["cartographer"]
        assert entry["command"] == "cartographer-mcp"
        assert entry["env"] == {"CARTOGRAPHER_REPO": "."}

    def test_preserves_sibling_mcp_servers(self, tmp_path):
        _make_old_style_install(tmp_path, with_data=False, with_claude=False,
                                extra_mcp_server=True)

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0

        data = json.loads((tmp_path / ".mcp.json").read_text())
        servers = data["mcpServers"]
        assert "cartographer" in servers
        assert "other-server" in servers
        assert servers["other-server"] == {"command": "other-mcp", "args": []}

    def test_rewrites_claude_md_in_place(self, tmp_path):
        _make_old_style_install(tmp_path, with_data=False, with_mcp=False)

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0

        text = (tmp_path / "CLAUDE.md").read_text()
        assert "graph-context" not in text
        assert ".graph-context" not in text
        assert "GRAPH_CONTEXT_REPO" not in text
        assert "GRAPH_CONTEXT_MCP_AUTOWATCH" not in text
        # And the new tokens are present
        assert "cartographer" in text
        assert ".cartographer/db/" in text
        assert "CARTOGRAPHER_REPO" in text
        assert "CARTOGRAPHER_MCP_AUTOWATCH" in text
        assert "cartographer-mcp" in text

    def test_full_install_end_to_end(self, tmp_path):
        _make_old_style_install(tmp_path, extra_mcp_server=True)

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0
        assert "Migration complete" in result.output
        # Surfaces follow-up commands
        assert "claude mcp remove graph-context" in result.output
        assert "claude mcp add cartographer -- cartographer-mcp" in result.output

        # Disk state
        assert not (tmp_path / ".graph-context").exists()
        assert (tmp_path / ".cartographer" / "db").is_dir()
        data = json.loads((tmp_path / ".mcp.json").read_text())
        assert "cartographer" in data["mcpServers"]
        assert "other-server" in data["mcpServers"]
        assert "graph-context" not in (tmp_path / "CLAUDE.md").read_text()


# ---------------------------------------------------------------------------
# Idempotency & error cases
# ---------------------------------------------------------------------------

class TestMigrateIdempotencyAndErrors:
    def test_second_run_is_noop(self, tmp_path):
        _make_old_style_install(tmp_path, extra_mcp_server=True)

        first = _invoke_migrate(tmp_path)
        assert first.exit_code == 0
        assert "Migration complete" in first.output

        second = _invoke_migrate(tmp_path)
        assert second.exit_code == 0
        assert "Already migrated" in second.output

    def test_refuses_when_both_data_dirs_exist(self, tmp_path):
        (tmp_path / ".graph-context" / "db").mkdir(parents=True)
        (tmp_path / ".cartographer" / "db").mkdir(parents=True)

        result = _invoke_migrate(tmp_path)

        assert result.exit_code != 0
        assert "both .graph-context/ and .cartographer/ exist" in result.output
        # Nothing was renamed
        assert (tmp_path / ".graph-context").exists()
        assert (tmp_path / ".cartographer").exists()

    def test_invalid_mcp_json_errors_cleanly(self, tmp_path):
        (tmp_path / ".mcp.json").write_text("{ this is not valid json")

        result = _invoke_migrate(tmp_path)

        assert result.exit_code != 0
        assert "not valid JSON" in result.output

    def test_partial_install_only_data_dir(self, tmp_path):
        # Old data dir exists but no .mcp.json or CLAUDE.md
        _make_old_style_install(tmp_path, with_mcp=False, with_claude=False)

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0
        assert (tmp_path / ".cartographer").is_dir()
        # The plan only mentions the data dir step
        assert "Rename .graph-context/" in result.output
        assert ".mcp.json" not in result.output
        assert "CLAUDE.md" not in result.output

    def test_partial_install_only_claude_md(self, tmp_path):
        _make_old_style_install(tmp_path, with_data=False, with_mcp=False)

        result = _invoke_migrate(tmp_path)
        assert result.exit_code == 0
        assert "graph-context" not in (tmp_path / "CLAUDE.md").read_text()
