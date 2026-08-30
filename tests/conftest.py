# tests/conftest.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialised git repo with one committed Python file."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


@pytest.fixture
def write(repo: Path):
    """Write a file and optionally commit it."""

    def _write(rel: str, text: str, *, commit: str | None = None) -> Path:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if commit:
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", commit)
        return path

    return _write
