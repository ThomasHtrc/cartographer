"""Git operations for change detection and history."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def is_git_repo(path: str | Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(path), capture_output=True, check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def get_head_hash(repo_path: str | Path) -> str | None:
    """Get the current HEAD commit hash, or None if no commits."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def get_changed_files(repo_path: str | Path, since_hash: str | None = None) -> list[str]:
    """Get files changed since a given commit hash.

    If since_hash is None, returns all tracked + untracked files.
    """
    results: set[str] = set()

    if since_hash:
        # Files changed in commits since that hash
        try:
            r = subprocess.run(
                ["git", "diff", "--name-only", since_hash, "HEAD"],
                cwd=str(repo_path), capture_output=True, text=True, check=True,
            )
            results.update(line for line in r.stdout.strip().splitlines() if line)
        except subprocess.CalledProcessError:
            pass

    # Uncommitted changes (staged + unstaged)
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, check=True,
        )
        results.update(line for line in r.stdout.strip().splitlines() if line)
    except subprocess.CalledProcessError:
        pass

    # Untracked files
    try:
        r = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(repo_path), capture_output=True, text=True, check=True,
        )
        results.update(line for line in r.stdout.strip().splitlines() if line)
    except subprocess.CalledProcessError:
        pass

    return sorted(results)


def get_all_tracked_files(repo_path: str | Path) -> list[str]:
    """Get all files tracked by git (plus untracked)."""
    results: set[str] = set()
    try:
        r = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_path), capture_output=True, text=True, check=True,
        )
        results.update(line for line in r.stdout.strip().splitlines() if line)
    except subprocess.CalledProcessError:
        pass

    # Also include untracked files
    try:
        r = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(repo_path), capture_output=True, text=True, check=True,
        )
        results.update(line for line in r.stdout.strip().splitlines() if line)
    except subprocess.CalledProcessError:
        pass

    return sorted(results)


# ---------------------------------------------------------------------------
# History: structured git log parsing
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    """A file-level change within a commit."""
    file_path: str
    additions: int
    deletions: int
    change_type: str  # "add", "modify", "delete", "rename"


@dataclass
class CommitInfo:
    """Parsed commit metadata + file-level changes."""
    hash: str
    message: str
    author: str
    timestamp: str  # ISO format
    parent_hashes: list[str]
    changes: list[FileChange]


def get_commit_log(
    repo_path: str | Path,
    max_commits: int = 500,
    since_hash: str | None = None,
) -> list[CommitInfo]:
    """Parse git log into structured commit objects with file-level stats.

    Returns commits in reverse chronological order (newest first).
    If since_hash is provided, only returns commits after that hash.
    """
    cmd = [
        "git", "log",
        f"--max-count={max_commits}",
        "--format=%H%x00%P%x00%aN%x00%aI%x00%s",
        "--numstat",
    ]
    if since_hash:
        cmd.append(f"{since_hash}..HEAD")

    try:
        r = subprocess.run(
            cmd, cwd=str(repo_path),
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return []

    return _parse_log_output(r.stdout)


def _parse_log_output(output: str) -> list[CommitInfo]:
    """Parse the combined format+numstat git log output."""
    commits: list[CommitInfo] = []
    lines = output.strip().split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        parts = line.split("\x00")
        if len(parts) == 5:
            hash_, parents_str, author, timestamp, message = parts
            parent_hashes = parents_str.split() if parents_str else []
            changes: list[FileChange] = []
            i += 1

            while i < len(lines):
                stat_line = lines[i].strip()
                if not stat_line:
                    i += 1
                    continue
                stat_parts = stat_line.split("\t")
                if len(stat_parts) >= 3 and (stat_parts[0].isdigit() or stat_parts[0] == "-"):
                    adds = int(stat_parts[0]) if stat_parts[0] != "-" else 0
                    dels = int(stat_parts[1]) if stat_parts[1] != "-" else 0
                    file_path = stat_parts[2]

                    if " => " in file_path:
                        file_path = _parse_rename_path(file_path)

                    change_type = "modify"
                    if adds > 0 and dels == 0:
                        change_type = "add"
                    elif adds == 0 and dels > 0:
                        change_type = "delete"

                    changes.append(FileChange(
                        file_path=file_path,
                        additions=adds,
                        deletions=dels,
                        change_type=change_type,
                    ))
                    i += 1
                else:
                    break

            commits.append(CommitInfo(
                hash=hash_,
                message=message,
                author=author,
                timestamp=timestamp,
                parent_hashes=parent_hashes,
                changes=changes,
            ))
        else:
            i += 1

    return commits


def _parse_rename_path(path: str) -> str:
    """Parse git rename notation to extract the new file path."""
    if "{" not in path:
        parts = path.split(" => ")
        return parts[-1].strip() if len(parts) == 2 else path

    match = re.match(r"^(.*)\{[^}]* => ([^}]*)\}(.*)$", path)
    if match:
        return match.group(1) + match.group(2) + match.group(3)
    return path


def get_diff_line_ranges(
    repo_path: str | Path,
    commit_hash: str,
    file_path: str,
) -> list[tuple[int, int]]:
    """Get the line ranges modified in a specific file for a commit.

    Returns a list of (start_line, end_line) tuples for added/modified lines.
    """
    try:
        r = subprocess.run(
            ["git", "diff", "-U0", f"{commit_hash}~1..{commit_hash}", "--", file_path],
            cwd=str(repo_path),
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        return []

    ranges: list[tuple[int, int]] = []
    for line in r.stdout.splitlines():
        match = re.match(r"^@@ .+ \+(\d+)(?:,(\d+))? @@", line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) else 1
            if count > 0:
                ranges.append((start, start + count - 1))

    return ranges
