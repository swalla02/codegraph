# codegraph v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Python CLI that answers `impact` and `effects` over a git-tracked codebase, keeping its index fresh by applying deltas rather than rebuilding.

**Architecture:** Two storage layers in one SQLite file. Layer 1 is an immutable parse cache keyed on the git blob SHA, so identical content is parsed once for the life of the repository and shared across branches. Layer 2 materializes a resolved graph for a revision from Layer 1. Every command reconciles the working tree before answering, so the index is never stale.

**Tech Stack:** Python 3.12, stdlib only at runtime (`ast`, `sqlite3`, `hashlib`, `tomllib`, `subprocess`). `uv` for env, `pytest` for tests, `ruff` for lint. Git invoked as a subprocess.

**Spec:** `docs/superpowers/specs/2026-08-29-codegraph-design.md`

## Global Constraints

- Python 3.12 floor. No runtime dependencies outside the stdlib; `pytest` and `ruff` are dev-only.
- Git is invoked as a subprocess. No libgit2, no GitPython.
- Phase 1 (parse) output MUST be path-independent — a blob may appear at several paths. Paths are applied at materialization only.
- Node ID format is `path::qualname`, e.g. `src/pay/service.py::PaymentService.charge`. The path segment stays `/`-separated so IDs remain pasteable into an editor. Line numbers are NEVER part of an ID.
- Symbol comparison for `diff` uses the normalized body hash, never the line span.
- Derived state lives in `.codegraph/` (which self-ignores via a `.gitignore` containing `*`). Hand-written config lives in `codegraph.toml` at the repository root and is tracked.
- Confidence tiers are exactly `HIGH`, `MEDIUM`, `LOW`. Edge provenance is exactly `static` in v1.
- Effect kinds are exactly: `DB_READ`, `DB_WRITE`, `NETWORK`, `FS_READ`, `FS_WRITE`, `PROCESS`, `ENV_READ`, `GLOBAL_MUTATE`, `NONDETERMINISM`.
- Every command must work when the directory is not a git repository, degrading to a filesystem walk with blake2b hashing; only `diff` is unavailable there.
- Commit after every task. Never commit a failing test suite.

## File Structure

```
src/codegraph/
  __init__.py        # __version__
  cli.py             # argparse dispatch, exit codes
  config.py          # codegraph.toml loading, source roots, effect overrides
  gitio.py           # ls-tree, cat-file --batch, status, hash-object, merge-base
  store.py           # sqlite schema, migrations, typed CRUD
  parse.py           # phase 1: blob bytes -> path-independent ParseResult
  resolve.py         # phase 2: Resolver protocol + AstResolver
  indexer.py         # orchestration: reconcile(rev) -> materialized revision
  render.py          # text and json renderers, token budgeting
  effects/
    __init__.py
    catalog.py       # builtin catalog + codegraph.toml merge
    detect.py        # direct effect tagging from refs
    propagate.py     # Tarjan SCC + transitive union + witness paths
  query/
    __init__.py
    rank.py          # scoring shared by impact and diff
    impact.py
    effects.py
    diff.py
skills/codegraph/SKILL.md
.claude-plugin/plugin.json
.claude-plugin/marketplace.json
tests/
  conftest.py        # temp git repo fixture, fixture-package builders
  fixtures/
```

Responsibilities are split so that each file is independently testable: `parse.py` never touches the filesystem or git, `gitio.py` never touches SQLite, `store.py` never parses.

## PR Grouping

- **PR 1 — indexing core:** Tasks 1–7
- **PR 2 — effects:** Tasks 8–10
- **PR 3 — queries:** Tasks 11–13
- **PR 4 — packaging:** Tasks 14–15

---

### Task 1: Project scaffold and CLI entry point

**Files:**
- Create: `pyproject.toml`, `src/codegraph/__init__.py`, `src/codegraph/cli.py`
- Test: `tests/test_cli_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `codegraph.__version__: str`; `codegraph.cli.main(argv: list[str] | None = None) -> int` returning a process exit code.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_smoke.py
import subprocess
import sys

from codegraph import __version__
from codegraph.cli import main


def test_version_constant_is_set():
    assert __version__


def test_main_version_returns_zero(capsys):
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_console_script_runs():
    proc = subprocess.run(
        [sys.executable, "-m", "codegraph", "--version"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'codegraph'`

- [ ] **Step 3: Write pyproject.toml**

```toml
[project]
name = "codegraph"
version = "0.1.0"
description = "A sidecar code graph for Python: impact and side-effect analysis"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
codegraph = "codegraph.cli:run"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/codegraph"]

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["slow: excluded from the default run"]
addopts = "-m 'not slow'"

[tool.ruff]
line-length = 100
src = ["src"]
```

- [ ] **Step 4: Write the package**

```python
# src/codegraph/__init__.py
__version__ = "0.1.0"
```

```python
# src/codegraph/cli.py
"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from codegraph import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codegraph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(handler=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.handler is None:
        parser.print_help()
        return 0
    return args.handler(args)


def run() -> None:
    sys.exit(main())
```

```python
# src/codegraph/__main__.py
from codegraph.cli import run

run()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv sync && uv run pytest tests/test_cli_smoke.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src tests
git commit -m "feat: project scaffold and CLI entry point"
```

---

### Task 2: Git subprocess layer

**Files:**
- Create: `src/codegraph/gitio.py`
- Test: `tests/test_gitio.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `is_repo(root: Path) -> bool`
  - `ls_tree(root: Path, rev: str) -> dict[str, str]` — maps repo-relative path to blob SHA, filtered to `*.py`
  - `cat_file_batch(root: Path, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]`
  - `status_paths(root: Path) -> dict[str, str]` — path to porcelain code (`M`, `A`, `D`, `??`)
  - `hash_object(root: Path, data: bytes) -> str`
  - `rev_parse(root: Path, rev: str) -> str`
  - `merge_base(root: Path, a: str, b: str) -> str`
  - `default_branch(root: Path) -> str`
  - `GitError(Exception)`

- [ ] **Step 1: Write the shared fixture**

```python
# tests/conftest.py
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
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
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_gitio.py
from codegraph import gitio
from tests.conftest import git


def test_is_repo(repo, tmp_path):
    assert gitio.is_repo(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not gitio.is_repo(plain)


def test_ls_tree_returns_python_blobs(repo, write):
    write("pkg/b.py", "def beta():\n    return 2\n", commit="add b")
    write("notes.md", "# hi\n", commit="add md")
    tree = gitio.ls_tree(repo, "HEAD")
    assert set(tree) == {"a.py", "pkg/b.py"}
    assert all(len(sha) == 40 for sha in tree.values())


def test_cat_file_batch_roundtrips_content(repo):
    tree = gitio.ls_tree(repo, "HEAD")
    sha = tree["a.py"]
    got = dict(gitio.cat_file_batch(repo, [sha]))
    assert got[sha] == b"def alpha():\n    return 1\n"


def test_cat_file_batch_handles_many_blobs(repo, write):
    for i in range(20):
        write(f"m{i}.py", f"def f{i}():\n    return {i}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "many")
    tree = gitio.ls_tree(repo, "HEAD")
    got = dict(gitio.cat_file_batch(repo, tree.values()))
    assert len(got) == len(tree)


def test_status_paths_reports_dirty_files(repo, write):
    write("a.py", "def alpha():\n    return 99\n")
    write("new.py", "def gamma():\n    pass\n")
    status = gitio.status_paths(repo)
    assert status["a.py"] == "M"
    assert status["new.py"] == "??"


def test_hash_object_matches_git(repo):
    sha = gitio.hash_object(repo, b"def alpha():\n    return 1\n")
    assert sha == gitio.ls_tree(repo, "HEAD")["a.py"]


def test_merge_base_and_default_branch(repo, write):
    base = gitio.rev_parse(repo, "HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("c.py", "def gamma():\n    pass\n", commit="feature work")
    assert gitio.merge_base(repo, "main", "feature") == base
    assert gitio.default_branch(repo) == "main"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_gitio.py -v`
Expected: FAIL — `ImportError: cannot import name 'gitio'`

- [ ] **Step 4: Implement gitio**

```python
# src/codegraph/gitio.py
"""Thin wrapper over the git CLI. Never touches SQLite."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path


class GitError(Exception):
    """A git subprocess failed."""


def _run(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=root, input=stdin, capture_output=True
    )
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def is_repo(root: Path) -> bool:
    try:
        _run(root, "rev-parse", "--git-dir")
    except (GitError, FileNotFoundError):
        return False
    return True


def ls_tree(root: Path, rev: str) -> dict[str, str]:
    out = _run(root, "ls-tree", "-r", "-z", "--format=%(objectname) %(path)", rev)
    tree: dict[str, str] = {}
    for entry in out.split(b"\0"):
        if not entry:
            continue
        sha, _, path = entry.decode().partition(" ")
        if path.endswith(".py"):
            tree[path] = sha
    return tree


def cat_file_batch(root: Path, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
    """Stream blob contents through a single `git cat-file --batch` process."""
    wanted = list(shas)
    if not wanted:
        return
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    try:
        proc.stdin.write(("\n".join(wanted) + "\n").encode())
        proc.stdin.flush()
        proc.stdin.close()
        for _ in wanted:
            header = proc.stdout.readline().decode().strip()
            if not header or header.endswith("missing"):
                continue
            sha, _kind, size_text = header.split(" ")
            payload = proc.stdout.read(int(size_text))
            proc.stdout.read(1)  # trailing newline
            yield sha, payload
    finally:
        proc.stdout.close()
        proc.wait()


def status_paths(root: Path) -> dict[str, str]:
    out = _run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    status: dict[str, str] = {}
    fields = [f for f in out.decode().split("\0") if f]
    index = 0
    while index < len(fields):
        field = fields[index]
        code, path = field[:2].strip() or field[:2], field[3:]
        if code.startswith("R"):
            index += 1  # rename source follows; skip it
        if path.endswith(".py"):
            status[path] = code
        index += 1
    return status


def hash_object(root: Path, data: bytes) -> str:
    return _run(root, "hash-object", "-t", "blob", "--stdin", stdin=data).decode().strip()


def rev_parse(root: Path, rev: str) -> str:
    return _run(root, "rev-parse", rev).decode().strip()


def merge_base(root: Path, a: str, b: str) -> str:
    return _run(root, "merge-base", a, b).decode().strip()


def default_branch(root: Path) -> str:
    for candidate in ("origin/HEAD", "main", "master"):
        try:
            resolved = _run(root, "rev-parse", "--abbrev-ref", candidate).decode().strip()
        except GitError:
            continue
        return resolved.removeprefix("origin/")
    raise GitError("no default branch found")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_gitio.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/codegraph/gitio.py tests/conftest.py tests/test_gitio.py
git commit -m "feat: git subprocess layer"
```

---

### Task 3: SQLite store and schema

**Files:**
- Create: `src/codegraph/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SCHEMA_VERSION: int`
  - `Store.open(root: Path) -> Store` — creates `.codegraph/` with a self-ignoring `.gitignore`
  - `Store.connection: sqlite3.Connection`
  - `Store.get_meta(key) -> str | None`, `Store.set_meta(key, value) -> None`
  - `Store.close() -> None`
  - Layer 1 tables: `blobs`, `blob_nodes`, `blob_refs`, `blob_imports`
  - Layer 2 tables: `revisions`, `tree`, `nodes`, `edges`, `effects`, `imports`, `unresolved`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import sqlite3

import pytest

from codegraph.store import SCHEMA_VERSION, Store


def test_open_creates_self_ignoring_directory(tmp_path):
    store = Store.open(tmp_path)
    ignore = tmp_path / ".codegraph" / ".gitignore"
    assert ignore.read_text().strip() == "*"
    assert (tmp_path / ".codegraph" / "graph.db").exists()
    store.close()


def test_schema_version_recorded(tmp_path):
    store = Store.open(tmp_path)
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.close()


def test_expected_tables_exist(tmp_path):
    store = Store.open(tmp_path)
    rows = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {row[0] for row in rows}
    assert {
        "meta", "blobs", "blob_nodes", "blob_refs", "blob_imports",
        "revisions", "tree", "nodes", "edges", "effects", "imports", "unresolved",
    } <= names
    store.close()


def test_reopen_preserves_data(tmp_path):
    store = Store.open(tmp_path)
    store.set_meta("probe", "value")
    store.close()
    reopened = Store.open(tmp_path)
    assert reopened.get_meta("probe") == "value"
    reopened.close()


def test_schema_mismatch_triggers_rebuild(tmp_path):
    store = Store.open(tmp_path)
    store.set_meta("probe", "value")
    store.set_meta("schema_version", "-1")
    store.close()
    reopened = Store.open(tmp_path)
    assert reopened.get_meta("probe") is None
    assert reopened.get_meta("schema_version") == str(SCHEMA_VERSION)
    reopened.close()


def test_edges_have_reverse_index(tmp_path):
    store = Store.open(tmp_path)
    plan = store.connection.execute(
        "EXPLAIN QUERY PLAN SELECT src FROM edges WHERE rev=? AND dst=?", ("r", "n")
    ).fetchall()
    assert any("idx_edges_dst" in str(row) for row in plan)
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'codegraph.store'`

- [ ] **Step 3: Implement the store**

```python
# src/codegraph/store.py
"""SQLite persistence. Never parses, never shells out to git."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

WORKTREE = "WORKTREE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Layer 1: immutable, content-addressed, shared across every revision.
CREATE TABLE IF NOT EXISTS blobs (
    blob_sha TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    error TEXT,
    parser_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blob_nodes (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    qualname TEXT NOT NULL,
    kind TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    body_hash TEXT NOT NULL,
    name_binding TEXT NOT NULL,
    shadow_index INTEGER,
    conditional INTEGER NOT NULL DEFAULT 0,
    decorators TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (blob_sha, ordinal)
);
CREATE TABLE IF NOT EXISTS blob_refs (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    from_qualname TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    raw_name TEXT NOT NULL,
    dotted TEXT,
    line INTEGER NOT NULL,
    PRIMARY KEY (blob_sha, ordinal)
);
CREATE TABLE IF NOT EXISTS blob_imports (
    blob_sha TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    module TEXT NOT NULL,
    level INTEGER NOT NULL,
    name TEXT,
    alias TEXT,
    PRIMARY KEY (blob_sha, ordinal)
);

-- Layer 2: materialized per revision, evictable.
CREATE TABLE IF NOT EXISTS revisions (
    rev TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    materialized_at INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tree (
    rev TEXT NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL,
    PRIMARY KEY (rev, path)
);
CREATE TABLE IF NOT EXISTS nodes (
    rev TEXT NOT NULL,
    id TEXT NOT NULL,
    path TEXT NOT NULL,
    qualname TEXT NOT NULL,
    kind TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    body_hash TEXT NOT NULL,
    name_binding TEXT NOT NULL,
    PRIMARY KEY (rev, id)
);
CREATE TABLE IF NOT EXISTS edges (
    rev TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'static',
    callsite_path TEXT NOT NULL,
    callsite_line INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    rev TEXT NOT NULL,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    direct INTEGER NOT NULL,
    evidence_path TEXT,
    evidence_line INTEGER,
    confidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    rev TEXT NOT NULL, importer_path TEXT NOT NULL, module TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unresolved (
    rev TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL, raw_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(rev, dst);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(rev, src);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(rev, module);
CREATE INDEX IF NOT EXISTS idx_nodes_qualname ON nodes(rev, qualname);
CREATE INDEX IF NOT EXISTS idx_effects_node ON effects(rev, node_id);
"""

_IGNORE_TEXT = "*\n"


class Store:
    """Owns the SQLite connection for one repository."""

    def __init__(self, connection: sqlite3.Connection, directory: Path) -> None:
        self.connection = connection
        self.directory = directory

    @classmethod
    def open(cls, root: Path) -> Store:
        directory = root / ".codegraph"
        directory.mkdir(exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text(_IGNORE_TEXT)

        db_path = directory / "graph.db"
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")

        store = cls(connection, directory)
        store._migrate()
        return store

    def _migrate(self) -> None:
        self.connection.executescript(_SCHEMA)
        self.connection.commit()
        recorded = self.get_meta("schema_version")
        if recorded != str(SCHEMA_VERSION):
            self._drop_all()
            self.connection.executescript(_SCHEMA)
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _drop_all(self) -> None:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for row in rows:
            self.connection.execute(f"DROP TABLE IF EXISTS {row['name']}")
        self.connection.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py -v`
Expected: 6 passed

- [ ] **Step 5: Add write locking**

The spec requires that a second writer waits rather than corrupting. Add to `Store.open`, after the WAL pragma:

```python
connection.execute("PRAGMA busy_timeout=10000")
```

and wrap `Indexer.reconcile`'s writes (Task 5) in `with self.store.connection:` so each reconcile is one transaction. WAL plus a busy timeout gives one writer at a time with the others blocking, which is the required behaviour without a separate lock file.

Add this test to `tests/test_store.py`:

```python
def test_busy_timeout_is_set(tmp_path):
    store = Store.open(tmp_path)
    timeout = store.connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 10000
    store.close()
```

- [ ] **Step 6: Commit**

```bash
git add src/codegraph/store.py tests/test_store.py
git commit -m "feat: sqlite store with two-layer schema"
```

---

### Task 4: Phase 1 parser (path-independent)

**Files:**
- Create: `src/codegraph/parse.py`
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PARSER_VERSION: str`
  - `ParsedNode(ordinal, qualname, kind, line_start, line_end, body_hash, name_binding, shadow_index, conditional, decorators)` — frozen dataclass
  - `ParsedRef(ordinal, from_qualname, ref_kind, raw_name, dotted, line)` — frozen dataclass
  - `ParsedImport(ordinal, module, level, name, alias)` — frozen dataclass
  - `ParseResult(nodes, refs, imports, error)` — frozen dataclass, all tuples
  - `parse_blob(source: bytes) -> ParseResult`

**Critical constraint:** nothing in this module may reference a file path. A blob can appear at several paths, so paths are applied only at materialization (Task 5).

**Shadowing rule:** definitions sharing a qualname are numbered by source order, 1-based. The LAST one is `name_binding="live"` with `shadow_index=None`; every earlier one is `name_binding="shadowed"` with its source index. `@overload`-decorated defs and defs inside `if TYPE_CHECKING:` or `try/except ImportError` blocks get `conditional=1` so `status` can exclude them from shadow warnings.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parse.py
from codegraph.parse import parse_blob


def qualnames(result):
    return [n.qualname for n in result.nodes]


def test_module_functions_and_classes():
    result = parse_blob(b"def alpha():\n    pass\n\n\nclass Beta:\n    def gamma(self):\n        pass\n")
    assert qualnames(result) == ["alpha", "Beta", "Beta.gamma"]
    kinds = {n.qualname: n.kind for n in result.nodes}
    assert kinds == {"alpha": "function", "Beta": "class", "Beta.gamma": "method"}


def test_nested_function_uses_locals_qualname():
    result = parse_blob(b"def outer():\n    def inner():\n        pass\n")
    assert "outer.<locals>.inner" in qualnames(result)


def test_body_hash_ignores_line_position():
    top = parse_blob(b"def alpha():\n    return 1\n")
    shifted = parse_blob(b"\n\n\ndef alpha():\n    return 1\n")
    assert top.nodes[0].body_hash == shifted.nodes[0].body_hash
    assert top.nodes[0].line_start != shifted.nodes[0].line_start


def test_body_hash_changes_with_body():
    one = parse_blob(b"def alpha():\n    return 1\n")
    two = parse_blob(b"def alpha():\n    return 2\n")
    assert one.nodes[0].body_hash != two.nodes[0].body_hash


def test_shadowed_definitions_are_all_retained():
    result = parse_blob(b"def alpha():\n    return 1\n\n\ndef alpha():\n    return 2\n")
    alphas = [n for n in result.nodes if n.qualname == "alpha"]
    assert len(alphas) == 2
    assert alphas[0].name_binding == "shadowed"
    assert alphas[0].shadow_index == 1
    assert alphas[1].name_binding == "live"
    assert alphas[1].shadow_index is None


def test_overload_definitions_marked_conditional():
    source = (
        b"from typing import overload\n"
        b"@overload\n"
        b"def alpha(x: int) -> int: ...\n"
        b"def alpha(x):\n    return x\n"
    )
    result = parse_blob(source)
    alphas = [n for n in result.nodes if n.qualname == "alpha"]
    assert alphas[0].conditional == 1
    assert alphas[1].name_binding == "live"


def test_type_checking_block_marked_conditional():
    source = (
        b"from typing import TYPE_CHECKING\n"
        b"if TYPE_CHECKING:\n"
        b"    def alpha() -> None: ...\n"
        b"else:\n"
        b"    def alpha():\n        return 1\n"
    )
    result = parse_blob(source)
    assert all(n.conditional == 1 for n in result.nodes if n.qualname == "alpha")


def test_calls_recorded_with_enclosing_scope():
    source = b"import requests\n\n\ndef fetch():\n    return requests.get('u')\n"
    result = parse_blob(source)
    call = [r for r in result.refs if r.ref_kind == "call"][0]
    assert call.from_qualname == "fetch"
    assert call.raw_name == "requests.get"
    assert call.line == 5


def test_bare_call_and_self_call_recorded():
    source = (
        b"def helper():\n    pass\n\n\n"
        b"class Service:\n"
        b"    def run(self):\n        helper()\n        self.step()\n"
        b"    def step(self):\n        pass\n"
    )
    result = parse_blob(source)
    raw = {r.raw_name for r in result.refs if r.ref_kind == "call"}
    assert raw == {"helper", "self.step"}


def test_imports_recorded_with_level():
    source = b"import os\nfrom . import sibling\nfrom pay.service import charge as c\n"
    result = parse_blob(source)
    got = {(i.module, i.level, i.name, i.alias) for i in result.imports}
    assert got == {
        ("os", 0, None, None),
        ("", 1, "sibling", None),
        ("pay.service", 0, "charge", "c"),
    }


def test_class_bases_recorded_as_refs():
    result = parse_blob(b"class Child(Parent):\n    pass\n")
    base = [r for r in result.refs if r.ref_kind == "base"][0]
    assert base.from_qualname == "Child"
    assert base.raw_name == "Parent"


def test_syntax_error_returns_error_not_exception():
    result = parse_blob(b"def broken(:\n")
    assert result.error is not None
    assert result.nodes == ()


def test_parse_is_deterministic():
    source = b"class A:\n    def m(self):\n        return other()\n"
    assert parse_blob(source) == parse_blob(source)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'codegraph.parse'`

- [ ] **Step 3: Implement the parser**

```python
# src/codegraph/parse.py
"""Phase 1: blob bytes -> path-independent structure.

Nothing here may reference a filesystem path: one blob can appear at many
paths, and the parse cache is keyed on content alone.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field

PARSER_VERSION = "1"

_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class ParsedNode:
    ordinal: int
    qualname: str
    kind: str
    line_start: int
    line_end: int
    body_hash: str
    name_binding: str
    shadow_index: int | None
    conditional: int
    decorators: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ParsedRef:
    ordinal: int
    from_qualname: str
    ref_kind: str
    raw_name: str
    dotted: str | None
    line: int


@dataclass(frozen=True)
class ParsedImport:
    ordinal: int
    module: str
    level: int
    name: str | None
    alias: str | None


@dataclass(frozen=True)
class ParseResult:
    nodes: tuple[ParsedNode, ...] = ()
    refs: tuple[ParsedRef, ...] = ()
    imports: tuple[ParsedImport, ...] = ()
    error: str | None = None


def _dotted_name(node: ast.AST) -> str | None:
    """Flatten a Name/Attribute chain into 'a.b.c'; None if not flattenable."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _decorator_names(node: ast.AST) -> tuple[str, ...]:
    decorators = getattr(node, "decorator_list", [])
    names = []
    for decorator in decorators:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = _dotted_name(target)
        if name:
            names.append(name)
    return tuple(names)


def _body_hash(node: ast.AST) -> str:
    # ast.dump omits lineno/col_offset unless include_attributes=True, so this
    # hash is invariant to where the definition sits in the file.
    return hashlib.blake2b(ast.dump(node).encode(), digest_size=16).hexdigest()


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[ParsedNode] = []
        self.refs: list[ParsedRef] = []
        self.imports: list[ParsedImport] = []
        self._scope: list[str] = []
        self._in_function = False
        self._conditional_depth = 0

    # -- scope helpers -------------------------------------------------
    @property
    def _qualname_prefix(self) -> str:
        return ".".join(self._scope)

    def _qualname(self, name: str) -> str:
        return f"{self._qualname_prefix}.{name}" if self._scope else name

    @property
    def _current_owner(self) -> str:
        return self._qualname_prefix or "<module>"

    # -- conditional definitions ---------------------------------------
    def visit_If(self, node: ast.If) -> None:
        guard = _dotted_name(node.test) or ""
        conditional = guard.endswith("TYPE_CHECKING")
        self._conditional_depth += int(conditional)
        self.generic_visit(node)
        self._conditional_depth -= int(conditional)

    def visit_Try(self, node: ast.Try) -> None:
        handles_import = any(
            (_dotted_name(h.type) or "").endswith("ImportError")
            for h in node.handlers
            if h.type is not None
        )
        self._conditional_depth += int(handles_import)
        self.generic_visit(node)
        self._conditional_depth -= int(handles_import)

    # -- definitions ----------------------------------------------------
    def _visit_def(self, node: ast.AST, kind: str) -> None:
        decorators = _decorator_names(node)
        is_overload = any(d.split(".")[-1] == "overload" for d in decorators)
        self.nodes.append(
            ParsedNode(
                ordinal=len(self.nodes),
                qualname=self._qualname(node.name),
                kind=kind,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                body_hash=_body_hash(node),
                name_binding="live",
                shadow_index=None,
                conditional=int(bool(self._conditional_depth) or is_overload),
                decorators=decorators,
            )
        )
        if kind == "class":
            for base in node.bases:
                name = _dotted_name(base)
                if name:
                    self.refs.append(
                        ParsedRef(
                            ordinal=len(self.refs),
                            from_qualname=self._qualname(node.name),
                            ref_kind="base",
                            raw_name=name,
                            dotted=name if "." in name else None,
                            line=base.lineno,
                        )
                    )

        self._scope.append(node.name)
        if kind in ("function", "method"):
            self._scope.append("<locals>")
        was_in_function = self._in_function
        self._in_function = kind in ("function", "method")
        self.generic_visit(node)
        self._in_function = was_in_function
        if kind in ("function", "method"):
            self._scope.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self._scope and not self._scope[-1] == "<locals>" and self._is_class_scope() else "function"
        self._visit_def(node, kind)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_scopes.add(len(self._scope))
        self._visit_def(node, "class")
        self._class_scopes.discard(len(self._scope))

    _class_scopes: set[int] = set()

    def _is_class_scope(self) -> bool:
        return len(self._scope) in self._class_scopes

    # -- references ------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name:
            self.refs.append(
                ParsedRef(
                    ordinal=len(self.refs),
                    from_qualname=self._current_owner.removesuffix(".<locals>"),
                    ref_kind="call",
                    raw_name=name,
                    dotted=name if "." in name else None,
                    line=node.lineno,
                )
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ParsedImport(
                    ordinal=len(self.imports),
                    module=alias.name,
                    level=0,
                    name=None,
                    alias=alias.asname,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.imports.append(
                ParsedImport(
                    ordinal=len(self.imports),
                    module=node.module or "",
                    level=node.level,
                    name=alias.name,
                    alias=alias.asname,
                )
            )


def _apply_shadowing(nodes: list[ParsedNode]) -> tuple[ParsedNode, ...]:
    """Last definition of a qualname wins the name; earlier ones are numbered."""
    positions: dict[str, list[int]] = {}
    for index, node in enumerate(nodes):
        positions.setdefault(node.qualname, []).append(index)

    resolved = list(nodes)
    for indices in positions.values():
        if len(indices) == 1:
            continue
        for source_index, node_index in enumerate(indices, start=1):
            is_last = node_index == indices[-1]
            current = resolved[node_index]
            resolved[node_index] = ParsedNode(
                ordinal=current.ordinal,
                qualname=current.qualname,
                kind=current.kind,
                line_start=current.line_start,
                line_end=current.line_end,
                body_hash=current.body_hash,
                name_binding="live" if is_last else "shadowed",
                shadow_index=None if is_last else source_index,
                conditional=current.conditional,
                decorators=current.decorators,
            )
    return tuple(resolved)


def parse_blob(source: bytes) -> ParseResult:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return ParseResult(error=f"{type(exc).__name__}: {exc}")

    collector = _Collector()
    collector.visit(tree)
    return ParseResult(
        nodes=_apply_shadowing(collector.nodes),
        refs=tuple(collector.refs),
        imports=tuple(collector.imports),
        error=None,
    )
```

**Implementer note — the `_class_scopes` set above is a deliberate placeholder and MUST be replaced.** As written it is a mutable class attribute shared across every `_Collector` instance, so parses would leak state into one another. Replace it with an explicit per-instance stack of scope kinds pushed and popped alongside `_scope`:

```python
self._kinds: list[str] = ["module"]        # "module" | "class" | "function"
# on entering a def:  kind = "method" if self._kinds[-1] == "class" else "function"
```

`test_parse_is_deterministic` and the method/function split in `test_module_functions_and_classes` will fail if this is left as-is.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parse.py -v`
Expected: 13 passed

- [ ] **Step 5: Lint**

Run: `uv run ruff check src tests`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/codegraph/parse.py tests/test_parse.py
git commit -m "feat: path-independent phase 1 parser"
```

---

### Task 5: Tree sources, indexer, and `status`

**Files:**
- Create: `src/codegraph/indexer.py`, `src/codegraph/config.py`
- Modify: `src/codegraph/cli.py`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: `gitio` (Task 2), `Store` (Task 3), `parse_blob`/`PARSER_VERSION` (Task 4).
- Produces:
  - `Config.load(root: Path) -> Config` with `source_roots: tuple[str, ...]` and `effect_overrides: list[dict]`
  - `TreeSource` protocol with `tree(rev: str) -> dict[str, str]` and `read(shas) -> Iterator[tuple[str, bytes]]`
  - `GitTreeSource(root)`, `FsTreeSource(root)`
  - `Indexer(root, store, source, config: Config | None = None)` with `reconcile(rev: str = WORKTREE) -> IndexStats`
    (defaults to `Config.load(root)`; Tasks 6 and 10 read `self.config`)
  - `IndexStats(blobs_parsed, blobs_cached, paths_total, paths_dirty, parse_errors, shadowed)`
  - `cli`: `codegraph status`, `codegraph index [--rebuild]`

**Cost guarantee this task must satisfy:** `IndexStats.blobs_parsed` counts only blobs absent from Layer 1. Creating a branch, or switching to a branch whose blobs were already seen, must yield `blobs_parsed == 0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_indexer.py
from codegraph.indexer import FsTreeSource, GitTreeSource, Indexer
from codegraph.store import WORKTREE, Store
from tests.conftest import git


def build(repo):
    store = Store.open(repo)
    return store, Indexer(repo, store, GitTreeSource(repo))


def test_first_index_parses_every_blob(repo, write):
    write("b.py", "def beta():\n    pass\n", commit="add b")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.paths_total == 2
    assert stats.blobs_parsed == 2
    assert stats.blobs_cached == 0
    store.close()


def test_second_index_parses_nothing(repo):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0
    assert stats.blobs_cached == 1
    store.close()


def test_creating_a_branch_costs_zero_parses(repo):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0
    store.close()


def test_switching_back_reparses_nothing(repo, write):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("a.py", "def alpha():\n    return 42\n", commit="change alpha")
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "main")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0, "blobs seen on main were already cached"
    store.close()


def test_worktree_revision_sees_uncommitted_edits(repo, write):
    store, indexer = build(repo)
    indexer.reconcile(WORKTREE)
    write("a.py", "def alpha():\n    return 7\n\n\ndef added():\n    pass\n")
    indexer.reconcile(WORKTREE)
    rows = store.connection.execute(
        "SELECT qualname FROM nodes WHERE rev=?", (WORKTREE,)
    ).fetchall()
    assert {row["qualname"] for row in rows} == {"alpha", "added"}
    store.close()


def test_node_ids_combine_path_and_qualname(repo, write):
    write("pkg/service.py", "class Svc:\n    def charge(self):\n        pass\n", commit="svc")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    ids = {
        row["id"]
        for row in store.connection.execute("SELECT id FROM nodes WHERE rev='HEAD'")
    }
    assert "pkg/service.py::Svc.charge" in ids
    store.close()


def test_deleted_file_drops_its_nodes(repo, write):
    write("gone.py", "def temp():\n    pass\n", commit="add gone")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    (repo / "gone.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "remove gone")
    indexer.reconcile("HEAD")
    rows = store.connection.execute(
        "SELECT id FROM nodes WHERE rev='HEAD' AND path='gone.py'"
    ).fetchall()
    assert rows == []
    store.close()


def test_rename_costs_no_parsing(repo, write):
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    git(repo, "mv", "a.py", "renamed.py")
    git(repo, "commit", "-qm", "rename")
    stats = indexer.reconcile("HEAD")
    assert stats.blobs_parsed == 0
    store.close()


def test_syntax_error_is_recorded_not_raised(repo, write):
    write("bad.py", "def broken(:\n", commit="bad")
    store, indexer = build(repo)
    stats = indexer.reconcile("HEAD")
    assert stats.parse_errors == 1
    store.close()


def test_works_without_git(tmp_path):
    (tmp_path / "solo.py").write_text("def solo():\n    pass\n")
    store = Store.open(tmp_path)
    indexer = Indexer(tmp_path, store, FsTreeSource(tmp_path))
    stats = indexer.reconcile(WORKTREE)
    assert stats.paths_total == 1
    assert stats.blobs_parsed == 1
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'codegraph.indexer'`

- [ ] **Step 3: Implement config**

```python
# src/codegraph/config.py
"""Project configuration, read from a tracked codegraph.toml at the repo root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "codegraph.toml"


@dataclass(frozen=True)
class Config:
    source_roots: tuple[str, ...] = ("", "src")
    effect_overrides: tuple[dict, ...] = ()

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / CONFIG_NAME
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        return cls(
            source_roots=tuple(data.get("source_roots", ["", "src"])),
            effect_overrides=tuple(data.get("effect", [])),
        )
```

- [ ] **Step 4: Implement tree sources and the indexer**

```python
# src/codegraph/indexer.py
"""Reconciles a revision into Layer 2 from the Layer 1 parse cache."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codegraph import gitio
from codegraph.config import Config
from codegraph.parse import PARSER_VERSION, parse_blob
from codegraph.store import WORKTREE, Store


@dataclass(frozen=True)
class IndexStats:
    paths_total: int = 0
    paths_dirty: int = 0
    blobs_parsed: int = 0
    blobs_cached: int = 0
    parse_errors: int = 0
    shadowed: int = 0


class TreeSource(Protocol):
    def tree(self, rev: str) -> dict[str, str]: ...
    def read(self, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]: ...


class GitTreeSource:
    def __init__(self, root: Path) -> None:
        self.root = root

    def tree(self, rev: str) -> dict[str, str]:
        if rev != WORKTREE:
            return gitio.ls_tree(self.root, rev)
        tree = gitio.ls_tree(self.root, "HEAD")
        for path, code in gitio.status_paths(self.root).items():
            full = self.root / path
            if code == "D" or not full.exists():
                tree.pop(path, None)
            else:
                tree[path] = gitio.hash_object(self.root, full.read_bytes())
        return tree

    def read(self, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        yield from gitio.cat_file_batch(self.root, shas)


class FsTreeSource:
    """Fallback for directories that are not git repositories."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._contents: dict[str, bytes] = {}

    def tree(self, rev: str) -> dict[str, str]:
        tree: dict[str, str] = {}
        self._contents.clear()
        for path in sorted(self.root.rglob("*.py")):
            if ".codegraph" in path.parts or ".git" in path.parts:
                continue
            data = path.read_bytes()
            sha = hashlib.blake2b(data, digest_size=20).hexdigest()
            rel = path.relative_to(self.root).as_posix()
            tree[rel] = sha
            self._contents[sha] = data
        return tree

    def read(self, shas: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        for sha in shas:
            if sha in self._contents:
                yield sha, self._contents[sha]


class Indexer:
    def __init__(
        self, root: Path, store: Store, source: TreeSource, config: Config | None = None
    ) -> None:
        self.root = root
        self.store = store
        self.source = source
        self.config = config or Config.load(root)

    def reconcile(self, rev: str = WORKTREE) -> IndexStats:
        connection = self.store.connection
        tree = self.source.tree(rev)

        stored = {
            row["path"]: row["blob_sha"]
            for row in connection.execute(
                "SELECT path, blob_sha FROM tree WHERE rev=?", (rev,)
            )
        }
        dirty = {p for p, sha in tree.items() if stored.get(p) != sha}
        removed = set(stored) - set(tree)

        parsed, cached, errors = self._ensure_parsed(set(tree.values()))

        connection.execute("DELETE FROM tree WHERE rev=?", (rev,))
        connection.executemany(
            "INSERT INTO tree(rev, path, blob_sha) VALUES(?, ?, ?)",
            [(rev, path, sha) for path, sha in tree.items()],
        )
        connection.execute("DELETE FROM nodes WHERE rev=?", (rev,))
        shadowed = self._materialize_nodes(rev, tree)

        connection.execute(
            "INSERT INTO revisions(rev, kind, materialized_at) VALUES(?, ?, ?) "
            "ON CONFLICT(rev) DO UPDATE SET materialized_at=excluded.materialized_at",
            (rev, "worktree" if rev == WORKTREE else "commit", int(time.time())),
        )
        connection.commit()

        return IndexStats(
            paths_total=len(tree),
            paths_dirty=len(dirty) + len(removed),
            blobs_parsed=parsed,
            blobs_cached=cached,
            parse_errors=errors,
            shadowed=shadowed,
        )

    def _ensure_parsed(self, shas: set[str]) -> tuple[int, int, int]:
        connection = self.store.connection
        known = {
            row["blob_sha"]
            for row in connection.execute(
                "SELECT blob_sha FROM blobs WHERE parser_version=?", (PARSER_VERSION,)
            )
        }
        missing = sorted(shas - known)
        errors = 0

        for sha, data in self.source.read(missing):
            result = parse_blob(data)
            errors += int(result.error is not None)
            connection.execute(
                "INSERT OR REPLACE INTO blobs(blob_sha, status, error, parser_version) "
                "VALUES(?, ?, ?, ?)",
                (sha, "error" if result.error else "ok", result.error, PARSER_VERSION),
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_nodes(blob_sha, ordinal, qualname, kind,"
                " line_start, line_end, body_hash, name_binding, shadow_index,"
                " conditional, decorators) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        sha, n.ordinal, n.qualname, n.kind, n.line_start, n.line_end,
                        n.body_hash, n.name_binding, n.shadow_index, n.conditional,
                        ",".join(n.decorators),
                    )
                    for n in result.nodes
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_refs(blob_sha, ordinal, from_qualname,"
                " ref_kind, raw_name, dotted, line) VALUES(?,?,?,?,?,?,?)",
                [
                    (sha, r.ordinal, r.from_qualname, r.ref_kind, r.raw_name, r.dotted, r.line)
                    for r in result.refs
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO blob_imports(blob_sha, ordinal, module, level,"
                " name, alias) VALUES(?,?,?,?,?,?)",
                [
                    (sha, i.ordinal, i.module, i.level, i.name, i.alias)
                    for i in result.imports
                ],
            )
        connection.commit()
        return len(missing), len(shas) - len(missing), errors

    def _materialize_nodes(self, rev: str, tree: dict[str, str]) -> int:
        connection = self.store.connection
        shadowed = 0
        rows: list[tuple] = []
        for path, sha in tree.items():
            for node in connection.execute(
                "SELECT * FROM blob_nodes WHERE blob_sha=? ORDER BY ordinal", (sha,)
            ):
                suffix = "" if node["shadow_index"] is None else f"#{node['shadow_index']}"
                if node["shadow_index"] is not None and not node["conditional"]:
                    shadowed += 1
                rows.append(
                    (
                        rev,
                        f"{path}::{node['qualname']}{suffix}",
                        path,
                        node["qualname"],
                        node["kind"],
                        node["line_start"],
                        node["line_end"],
                        node["body_hash"],
                        node["name_binding"],
                    )
                )
        connection.executemany(
            "INSERT OR REPLACE INTO nodes(rev, id, path, qualname, kind, line_start,"
            " line_end, body_hash, name_binding) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return shadowed
```

- [ ] **Step 5: Wire `status` and `index` into the CLI**

Add to `src/codegraph/cli.py`: a `open_workspace(root)` helper returning `(store, indexer)` — choosing `GitTreeSource` when `gitio.is_repo(root)` and `FsTreeSource` otherwise — plus subparsers `status` and `index`. `status` prints paths, dirty count, blobs parsed/cached, parse errors, and a shadowing warning line when `stats.shadowed` is non-zero. `index --rebuild` deletes every row in `blobs` before reconciling.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: 10 passed

- [ ] **Step 7: Commit**

```bash
git add src/codegraph/indexer.py src/codegraph/config.py src/codegraph/cli.py tests/test_indexer.py
git commit -m "feat: tree sources, incremental indexer, status command"
```

---

### Task 6: Phase 2 resolver and `resolve` command

**Files:**
- Create: `src/codegraph/resolve.py`
- Modify: `src/codegraph/indexer.py` (call the resolver at the end of `reconcile`), `src/codegraph/cli.py`
- Test: `tests/test_resolve.py`

**Interfaces:**
- Consumes: `Store`, `Config`, materialized `nodes`/`tree` (Task 5), `blob_refs`/`blob_imports` (Task 4).
- Produces:
  - `HIGH = "HIGH"`, `MEDIUM = "MEDIUM"`, `LOW = "LOW"`
  - `ResolveContext(rev, path, module, module_to_path, qualname_index, name_index, bases)`
  - `Resolver` protocol: `resolve_call(ref, ctx) -> list[tuple[str, str]]` returning `(node_id, confidence)` pairs
  - `AstResolver` implementing it
  - `module_for_path(path: str, source_roots) -> str`
  - `resolve_revision(store, rev, config) -> ResolveStats`
  - `dependents(store, rev, modules) -> set[str]`
  - `ResolveStats(edges, unresolved)`
  - `find_symbol(store, rev, query) -> list[sqlite3.Row]` for the `resolve` command
  - `cli`: `codegraph resolve <query>`

**Resolution order** (first match wins):
1. `ref.dotted` maps to an imported module and a qualname in it → `HIGH`
2. Bare name defined in the same module with `name_binding='live'` → `HIGH`
3. `self.X` → walk the enclosing class then its bases via `INHERITS` → `HIGH`
4. Last dotted segment matches exactly one node repo-wide → `MEDIUM`
5. Last dotted segment matches several → emit an edge to each at `LOW`
6. Otherwise → record in `unresolved`

**Important:** external calls such as `requests.get` never resolve to a node. They must stay in `blob_refs` for effect detection (Task 10), which reads refs rather than edges.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve.py
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.resolve import module_for_path
from codegraph.store import Store


def build(repo):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    return store, indexer


def edges(store, rev="HEAD"):
    return {
        (row["src"], row["dst"], row["confidence"])
        for row in store.connection.execute(
            "SELECT src, dst, confidence FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
        )
    }


def test_module_for_path_handles_src_layout_and_packages():
    roots = ("", "src")
    assert module_for_path("src/pay/service.py", roots) == "pay.service"
    assert module_for_path("pay/__init__.py", roots) == "pay"
    assert module_for_path("a.py", roots) == "a"


def test_same_module_call_is_high_confidence(repo, write):
    write("m.py", "def helper():\n    pass\n\n\ndef caller():\n    helper()\n", commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("m.py::caller", "m.py::helper", "HIGH") in edges(store)
    store.close()


def test_imported_call_is_high_confidence(repo, write):
    write("pay/__init__.py", "", commit="pkg")
    write("pay/service.py", "def charge():\n    pass\n", commit="svc")
    write("handlers.py", "from pay.service import charge\n\n\ndef run():\n    charge()\n", commit="h")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("handlers.py::run", "pay/service.py::charge", "HIGH") in edges(store)
    store.close()


def test_self_call_resolves_through_class(repo, write):
    source = (
        "class Service:\n"
        "    def run(self):\n        self.step()\n"
        "    def step(self):\n        pass\n"
    )
    write("s.py", source, commit="s")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("s.py::Service.run", "s.py::Service.step", "HIGH") in edges(store)
    store.close()


def test_self_call_resolves_through_base_class(repo, write):
    write("base.py", "class Base:\n    def step(self):\n        pass\n", commit="base")
    write(
        "child.py",
        "from base import Base\n\n\nclass Child(Base):\n    def run(self):\n        self.step()\n",
        commit="child",
    )
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("child.py::Child.run", "base.py::Base.step", "HIGH") in edges(store)
    store.close()


def test_unique_method_name_is_medium_confidence(repo, write):
    write("owner.py", "class Owner:\n    def unique_op(self):\n        pass\n", commit="o")
    write("caller.py", "def go(thing):\n    thing.unique_op()\n", commit="c")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    assert ("caller.py::go", "owner.py::Owner.unique_op", "MEDIUM") in edges(store)
    store.close()


def test_ambiguous_method_name_emits_low_edges_to_every_candidate(repo, write):
    write("one.py", "class One:\n    def shared(self):\n        pass\n", commit="1")
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="2")
    write("caller.py", "def go(thing):\n    thing.shared()\n", commit="c")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    found = {(dst, conf) for src, dst, conf in edges(store) if src == "caller.py::go"}
    assert found == {("one.py::One.shared", "LOW"), ("two.py::Two.shared", "LOW")}
    store.close()


def test_shadowed_definition_does_not_win_name_lookup(repo, write):
    source = "def alpha():\n    return 1\n\n\ndef alpha():\n    return 2\n\n\ndef caller():\n    alpha()\n"
    write("m.py", source, commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    targets = {dst for src, dst, _ in edges(store) if src == "m.py::caller"}
    assert targets == {"m.py::alpha"}
    store.close()


def test_external_call_is_unresolved_not_an_edge(repo, write):
    write("m.py", "import requests\n\n\ndef fetch():\n    requests.get('u')\n", commit="m")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    rows = store.connection.execute(
        "SELECT raw_name FROM unresolved WHERE rev='HEAD'"
    ).fetchall()
    assert "requests.get" in {row["raw_name"] for row in rows}
    store.close()


def test_editing_a_module_updates_edges_in_its_importers(repo, write):
    write("dep.py", "def target():\n    pass\n", commit="dep")
    write("user.py", "from dep import target\n\n\ndef go():\n    target()\n", commit="user")
    store, indexer = build(repo)
    indexer.reconcile("HEAD")
    write("dep.py", "def renamed():\n    pass\n", commit="rename target")
    indexer.reconcile("HEAD")
    assert not [e for e in edges(store) if e[1] == "dep.py::target"]
    store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolve.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'codegraph.resolve'`

- [ ] **Step 3: Implement the resolver**

Write `src/codegraph/resolve.py` containing:

```python
"""Phase 2: unresolved references -> edges, against a revision's symbol table."""

from __future__ import annotations

from dataclasses import dataclass, field

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"


def module_for_path(path: str, source_roots: tuple[str, ...]) -> str:
    """'src/pay/service.py' -> 'pay.service'; 'pay/__init__.py' -> 'pay'."""
    trimmed = path
    for root in sorted(source_roots, key=len, reverse=True):
        prefix = f"{root}/" if root else ""
        if prefix and trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix):]
            break
    trimmed = trimmed.removesuffix(".py").removesuffix("/__init__")
    return trimmed.replace("/", ".")


@dataclass
class ResolveContext:
    rev: str
    path: str
    module: str
    module_to_path: dict[str, str]
    qualname_index: dict[tuple[str, str], str]   # (path, qualname) -> node id, live only
    name_index: dict[str, list[str]]             # bare name -> node ids, live only
    import_map: dict[str, str]                   # local alias -> dotted target
    bases: dict[str, list[str]]                  # class node id -> base class node ids
    enclosing_class: dict[str, str] = field(default_factory=dict)  # node id -> class node id
```

`AstResolver.resolve_call(ref, ctx)` implements the six-step order above. `resolve_revision(store, rev, config)`:

1. Build `module_to_path`, `qualname_index` (live bindings only), and `name_index` (keyed on the final `.`-separated segment of each qualname) from the `nodes` table for `rev`.
2. Resolve `base` refs first and write `INHERITS` edges, so `bases` is populated before any `self.X` lookup runs.
3. Populate the `imports` table from `blob_imports`, expanding relative imports using the importing file's package (`level=1` means the current package, `level=2` its parent).
4. For each `call` ref, run the resolver and write `CALLS` edges, or a row in `unresolved`.
5. Delete and rewrite `edges`, `imports`, and `unresolved` for `rev` inside one transaction.

Also provide:

```python
def dependents(store, rev: str, modules: set[str]) -> set[str]:
    """Paths whose imports name any of these modules — the re-resolve set."""
    if not modules:
        return set()
    placeholders = ",".join("?" * len(modules))
    rows = store.connection.execute(
        f"SELECT DISTINCT importer_path FROM imports WHERE rev=? AND module IN ({placeholders})",
        (rev, *modules),
    )
    return {row["importer_path"] for row in rows}


def find_symbol(store, rev: str, query: str) -> list[sqlite3.Row]:
    """Fuzzy lookup: exact id, then exact qualname, then suffix match."""
```

- [ ] **Step 4: Call the resolver from `Indexer.reconcile`**

At the end of `reconcile`, after nodes are materialized, call `resolve_revision(self.store, rev, self.config)` and fold `ResolveStats` into `IndexStats`. v1 re-resolves the whole revision; the `dependents()` narrowing is a later optimisation and is covered by the cost test only for parsing, not resolution.

- [ ] **Step 5: Add the `resolve` CLI command**

`codegraph resolve <query>` prints matching node ids. On multiple matches it prints all candidates and returns exit code 2; on no match, exit code 1.

Extend `codegraph status` to print the `unresolved` row count for the revision. The spec names a rising unresolved count as the primary signal that resolution is degrading, so it must be visible, not merely stored.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolve.py tests/test_indexer.py -v`
Expected: all passed

- [ ] **Step 7: Commit**

```bash
git add src/codegraph/resolve.py src/codegraph/indexer.py src/codegraph/cli.py tests/test_resolve.py
git commit -m "feat: phase 2 resolver and resolve command"
```

---

### Task 7: The invariant and cost-guarantee tests

**Files:**
- Create: `tests/test_invariants.py`
- Test: itself

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: `dump_graph(store, rev) -> dict` — a canonical, order-independent snapshot used by every later task's regression tests.

This is the task the whole project's trustworthiness rests on. **Incremental result must equal a cold rebuild**, through real git operations. If it ever drifts, every answer becomes irreproducible.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_invariants.py
import random

import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import WORKTREE, Store
from tests.conftest import git


def dump_graph(store, rev):
    """Canonical, order-independent snapshot of a materialized revision."""
    connection = store.connection
    return {
        "nodes": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT id, path, qualname, kind, body_hash, name_binding"
                " FROM nodes WHERE rev=?", (rev,)
            )
        ),
        "edges": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT src, dst, kind, confidence FROM edges WHERE rev=?", (rev,)
            )
        ),
        "unresolved": sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT path, raw_name FROM unresolved WHERE rev=?", (rev,)
            )
        ),
    }


def cold_dump(repo, rev):
    """Index the same state from scratch in a throwaway database."""
    fresh = repo / ".codegraph" / "graph.db"
    if fresh.exists():
        fresh.unlink()
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile(rev)
    dump = dump_graph(store, rev)
    store.close()
    return dump


MUTATIONS = ["edit", "add", "delete", "rename", "branch", "switch", "merge"]


def apply_mutation(repo, kind, counter):
    files = sorted(p.name for p in repo.glob("*.py"))
    if kind == "edit" and files:
        target = repo / random.choice(files)
        target.write_text(f"def f{counter}():\n    return {counter}\n")
        git(repo, "add", "-A"); git(repo, "commit", "-qm", f"edit {counter}")
    elif kind == "add":
        (repo / f"m{counter}.py").write_text(
            f"def g{counter}():\n    return f{counter}()\n"
        )
        git(repo, "add", "-A"); git(repo, "commit", "-qm", f"add {counter}")
    elif kind == "delete" and len(files) > 1:
        (repo / random.choice(files)).unlink()
        git(repo, "add", "-A"); git(repo, "commit", "-qm", f"delete {counter}")
    elif kind == "rename" and files:
        git(repo, "mv", random.choice(files), f"r{counter}.py")
        git(repo, "commit", "-qm", f"rename {counter}")
    elif kind == "branch":
        git(repo, "checkout", "-q", "-b", f"b{counter}")
    elif kind == "switch":
        git(repo, "checkout", "-q", "main")
    elif kind == "merge":
        current = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != "main":
            git(repo, "checkout", "-q", "main")
            git(repo, "merge", "-q", "--no-edit", current)


@pytest.mark.parametrize("seed", range(8))
def test_incremental_equals_cold_rebuild(repo, seed):
    random.seed(seed)
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")

    for counter in range(12):
        apply_mutation(repo, random.choice(MUTATIONS), counter)
        indexer.reconcile("HEAD")

    incremental = dump_graph(store, "HEAD")
    store.close()
    assert incremental == cold_dump(repo, "HEAD")


def test_worktree_incremental_equals_cold_rebuild(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile(WORKTREE)
    write("a.py", "def alpha():\n    return 5\n")
    write("added.py", "def added():\n    alpha()\n")
    indexer.reconcile(WORKTREE)
    incremental = dump_graph(store, WORKTREE)
    store.close()
    assert incremental == cold_dump(repo, WORKTREE)


# -- cost guarantees --------------------------------------------------------

def test_branch_creation_parses_zero_blobs(repo):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    assert indexer.reconcile("HEAD").blobs_parsed == 0
    store.close()


def test_round_trip_switch_parses_zero_on_return(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "feature")
    write("a.py", "def alpha():\n    return 2\n", commit="edit on feature")
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "main")
    assert indexer.reconcile("HEAD").blobs_parsed == 0
    store.close()


def test_switch_parses_at_most_the_changed_files(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    for i in range(12):
        write(f"m{i}.py", f"def f{i}():\n    pass\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "many")
    indexer.reconcile("HEAD")

    git(repo, "checkout", "-q", "-b", "feature")
    for i in range(3):
        write(f"m{i}.py", f"def f{i}():\n    return {i}\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "touch three")
    assert indexer.reconcile("HEAD").blobs_parsed == 3
    store.close()
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_invariants.py -v`
Expected: initially FAIL. Every failure here is a real bug in Tasks 4–6, not a test problem. Common causes: iteration order leaking into stored rows, stale `edges`/`unresolved` rows not deleted for the revision before rewriting, and deleted paths leaving orphaned nodes.

- [ ] **Step 3: Fix the underlying bugs until green**

Do not weaken the assertions. If `dump_graph` differs, find why the incremental path produced different data.

- [ ] **Step 4: Commit**

```bash
git add tests/test_invariants.py
git commit -m "test: incremental-equals-rebuild invariant and cost guarantees"
```

- [ ] **Step 5: Open PR 1**

```bash
git push -u origin feat/indexing-core
gh pr create --title "Indexing core: two-layer store, parser, resolver" \
  --body "Tasks 1-7. Layer 1 parse cache keyed on blob SHA, Layer 2 materialized per revision. Includes the incremental-equals-cold-rebuild invariant test and the cost guarantees (branch creation parses zero blobs; A->B->A reparses zero on return)."
```

---

### Task 8: Renderers

**Files:**
- Create: `src/codegraph/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: nothing (pure formatting).
- Produces:
  - `Report(summary: dict, groups: list[Group], truncated: bool)` and `Group(title: str, rows: list[Row])`
  - `Row(id: str, location: str, detail: str, score: float)`
  - `render_text(report: Report) -> str`
  - `render_json(report: Report) -> str`
  - `budget(rows: list[Row], limit: int) -> tuple[list[Row], bool]`

Summary comes first in both renderers. JSON always carries an explicit `truncated` boolean so an agent knows whether to ask for more.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render.py
import json

from codegraph.render import Group, Report, Row, budget, render_json, render_text


def sample():
    return Report(
        summary={"symbols": 47, "modules": 12, "entry_points": 9, "low_confidence_hidden": 31},
        groups=[
            Group("hop 1", [Row("a.py::one", "a.py:10", "HIGH", 9.0)]),
            Group("tests", [Row("tests/test_a.py::test_one", "tests/test_a.py:3", "HIGH", 1.0)]),
        ],
        truncated=True,
    )


def test_budget_truncates_and_flags():
    rows = [Row(f"m.py::f{i}", "m.py:1", "HIGH", float(i)) for i in range(10)]
    kept, truncated = budget(rows, 4)
    assert len(kept) == 4
    assert truncated is True


def test_budget_keeps_highest_scores_first():
    rows = [Row(f"m.py::f{i}", "m.py:1", "HIGH", float(i)) for i in range(5)]
    kept, _ = budget(rows, 2)
    assert [r.score for r in kept] == [4.0, 3.0]


def test_text_leads_with_summary():
    text = render_text(sample())
    first = text.splitlines()[0]
    assert "47" in first and "12" in first


def test_text_keeps_tests_in_their_own_group():
    text = render_text(sample())
    assert "tests" in text
    assert text.index("hop 1") < text.index("tests")


def test_json_is_machine_readable_and_flags_truncation():
    payload = json.loads(render_json(sample()))
    assert payload["truncated"] is True
    assert payload["summary"]["symbols"] == 47
    assert payload["groups"][0]["rows"][0]["id"] == "a.py::one"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'codegraph.render'`

- [ ] **Step 3: Implement `render.py`**

Frozen dataclasses for `Row`, `Group`, `Report`. `budget` sorts by `score` descending, slices to `limit`, and returns `(kept, len(rows) > limit)`. `render_text` prints a single summary line built from `report.summary` items joined with ` · `, then each group as a heading followed by `  {id}  {location}  {detail}`. `render_json` dumps `dataclasses.asdict(report)` with `indent=2`.

- [ ] **Step 4: Run tests, then commit**

```bash
uv run pytest tests/test_render.py -v
git add src/codegraph/render.py tests/test_render.py
git commit -m "feat: text and json renderers with token budgeting"
```

---

### Task 9: Effect catalog

**Files:**
- Create: `src/codegraph/effects/__init__.py`, `src/codegraph/effects/catalog.py`, `src/codegraph/effects/builtin.toml`
- Test: `tests/test_effect_catalog.py`

**Interfaces:**
- Consumes: `Config.effect_overrides` (Task 5).
- Produces:
  - `EFFECT_KINDS: tuple[str, ...]` — exactly the nine kinds from Global Constraints
  - `Rule(match: str, kind: str)`
  - `Catalog.load(config: Config) -> Catalog`
  - `Catalog.match(dotted: str) -> str | None`
  - `Catalog.fingerprint() -> str` — hash of all rules, part of the cache key

Patterns are dotted globs: `requests.*` matches `requests.get`; `*.session.commit` matches `db.session.commit`. Longest literal prefix wins so overrides beat built-ins.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_effect_catalog.py
from codegraph.config import Config
from codegraph.effects.catalog import EFFECT_KINDS, Catalog


def test_nine_effect_kinds_exactly():
    assert set(EFFECT_KINDS) == {
        "DB_READ", "DB_WRITE", "NETWORK", "FS_READ", "FS_WRITE",
        "PROCESS", "ENV_READ", "GLOBAL_MUTATE", "NONDETERMINISM",
    }


def test_builtin_rules_cover_common_libraries():
    catalog = Catalog.load(Config())
    assert catalog.match("requests.get") == "NETWORK"
    assert catalog.match("httpx.post") == "NETWORK"
    assert catalog.match("subprocess.run") == "PROCESS"
    assert catalog.match("os.environ.get") == "ENV_READ"
    assert catalog.match("open") == "FS_READ"


def test_unknown_name_matches_nothing():
    assert Catalog.load(Config()).match("my.own.helper") is None


def test_project_override_adds_house_abstractions():
    config = Config(effect_overrides=({"match": "app.db.*", "kind": "DB_WRITE"},))
    assert Catalog.load(config).match("app.db.save") == "DB_WRITE"


def test_override_beats_builtin_on_longer_prefix():
    config = Config(effect_overrides=({"match": "requests.get", "kind": "DB_READ"},))
    catalog = Catalog.load(config)
    assert catalog.match("requests.get") == "DB_READ"
    assert catalog.match("requests.post") == "NETWORK"


def test_fingerprint_changes_with_overrides():
    base = Catalog.load(Config()).fingerprint()
    other = Catalog.load(Config(effect_overrides=({"match": "x.*", "kind": "NETWORK"},))).fingerprint()
    assert base != other
```

- [ ] **Step 2: Run to verify it fails, then implement**

`builtin.toml` ships `[[effect]]` entries covering at minimum: `requests.*`, `httpx.*`, `urllib.request.*`, `socket.*` → `NETWORK`; `subprocess.*`, `os.system`, `os.exec*` → `PROCESS`; `os.environ*` → `ENV_READ`; `open`, `pathlib.Path.read*` → `FS_READ`; `pathlib.Path.write*`, `shutil.*` → `FS_WRITE`; `*.session.commit`, `*.session.add`, `*.execute`, `psycopg*`, `boto3.*` → `DB_WRITE`/`DB_READ` as appropriate; `random.*`, `time.time`, `uuid.uuid4`, `datetime.datetime.now` → `NONDETERMINISM`.

`Catalog.match` compiles each pattern with `fnmatch.translate` and, on multiple matches, prefers the rule with the longest leading literal segment.

- [ ] **Step 3: Run tests, then commit**

```bash
uv run pytest tests/test_effect_catalog.py -v
git add src/codegraph/effects tests/test_effect_catalog.py
git commit -m "feat: effect catalog with project overrides"
```

---

### Task 10: Effect detection, propagation, and the `effects` command

**Files:**
- Create: `src/codegraph/effects/detect.py`, `src/codegraph/effects/propagate.py`, `src/codegraph/query/effects.py`, `src/codegraph/query/__init__.py`
- Modify: `src/codegraph/indexer.py`, `src/codegraph/cli.py`
- Test: `tests/test_effects.py`

**Interfaces:**
- Consumes: `Catalog` (Task 9), `nodes`/`edges`/`blob_refs` (Tasks 4–6), `Report`/`Row`/`Group` (Task 8).
- Produces:
  - `detect_direct(store, rev, catalog) -> int` — writes rows with `direct=1`
  - `propagate(store, rev) -> int` — writes rows with `direct=0`
  - `witness_path(store, rev, node_id, kind) -> list[str]` — shortest chain of node ids ending at the direct cause
  - `effects_report(store, rev, node_id) -> Report`
  - `cli`: `codegraph effects <sym> [--json]`

**Detection runs on refs, not edges** — `requests.get` never becomes a node, so an edge-only pass would miss every external effect.

**Propagation** condenses strongly-connected components with Tarjan so recursion cycles share one effect union, and carries confidence as the minimum along the path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_effects.py
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.effects import effects_report
from codegraph.store import Store


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def kinds(report):
    return {row.detail.split()[0] for group in report.groups for row in group.rows}


def test_direct_network_effect_detected(repo, write):
    write("m.py", "import requests\n\n\ndef fetch():\n    requests.get('u')\n", commit="m")
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "m.py::fetch"))
    store.close()


def test_effect_propagates_through_call_chain(repo, write):
    source = (
        "import requests\n\n\n"
        "def low():\n    requests.get('u')\n\n\n"
        "def mid():\n    low()\n\n\n"
        "def high():\n    mid()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "m.py::high"))
    store.close()


def test_pure_function_reports_no_effects(repo, write):
    write("m.py", "def pure(x):\n    return x + 1\n", commit="m")
    store = build(repo)
    assert effects_report(store, "HEAD", "m.py::pure").groups == []
    store.close()


def test_recursion_cycle_does_not_hang(repo, write):
    source = (
        "import requests\n\n\n"
        "def ping(n):\n    requests.get('u')\n    return pong(n)\n\n\n"
        "def pong(n):\n    return ping(n)\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    assert "NETWORK" in kinds(effects_report(store, "HEAD", "m.py::pong"))
    store.close()


def test_witness_path_reaches_the_causing_callsite(repo, write):
    source = (
        "import requests\n\n\n"
        "def low():\n    requests.get('u')\n\n\n"
        "def high():\n    low()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    report = effects_report(store, "HEAD", "m.py::high")
    row = report.groups[0].rows[0]
    assert "m.py::low" in row.detail
    assert row.location.startswith("m.py:")
    store.close()


def test_global_mutation_detected_syntactically(repo, write):
    write("m.py", "COUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT += 1\n", commit="m")
    store = build(repo)
    assert "GLOBAL_MUTATE" in kinds(effects_report(store, "HEAD", "m.py::bump"))
    store.close()


def test_project_override_tags_house_abstraction(repo, write):
    write("codegraph.toml", '[[effect]]\nmatch = "app.db.*"\nkind = "DB_WRITE"\n')
    write("app/__init__.py", "", commit="pkg")
    write("app/db.py", "def save():\n    pass\n", commit="db")
    write("svc.py", "from app import db\n\n\ndef run():\n    db.save()\n", commit="svc")
    store = build(repo)
    assert "DB_WRITE" in kinds(effects_report(store, "HEAD", "svc.py::run"))
    store.close()
```

- [ ] **Step 2: Run to verify it fails, then implement**

`detect_direct` joins `tree` → `blob_refs` for the revision, runs `Catalog.match` on each ref's `raw_name`, and inserts an `effects` row with `direct=1`, `evidence_path`, and `evidence_line`. `GLOBAL_MUTATE` needs a `global`/`nonlocal` ref kind emitted by `parse.py` — add `ref_kind="global"` in Task 4's collector via `visit_Global`/`visit_Nonlocal` if it is not already there, and detect it here.

`propagate` loads `CALLS` edges for the revision, runs Tarjan SCC, condenses, then unions effects in reverse topological order, writing rows with `direct=0`.

`effects_report` groups by effect kind, sorts by severity (`DB_WRITE`, `NETWORK`, `PROCESS`, `FS_WRITE`, `GLOBAL_MUTATE`, `DB_READ`, `FS_READ`, `ENV_READ`, `NONDETERMINISM`) then confidence.

**`Row.detail` format is fixed and parsed by tests in this task and Task 11:**

```
f"{kind} {confidence} via {' -> '.join(witness_path)}"
```

The effect kind MUST be the first whitespace-separated token. `Row.location` is `f"{evidence_path}:{evidence_line}"`.

Call `detect_direct` and `propagate` at the end of `Indexer.reconcile`.

- [ ] **Step 3: Run tests, then commit and open PR 2**

```bash
uv run pytest -v
git add src/codegraph tests/test_effects.py
git commit -m "feat: effect detection, propagation, and effects command"
git push -u origin feat/effects
gh pr create --title "Effects: catalog, detection, propagation" --body "Tasks 8-10."
```

---

### Task 11: Ranking and the `impact` command

**Files:**
- Create: `src/codegraph/query/rank.py`, `src/codegraph/query/impact.py`
- Modify: `src/codegraph/cli.py`
- Test: `tests/test_impact.py`

**Interfaces:**
- Consumes: `edges` reverse index (Task 6), `effects` (Task 10), `Report` (Task 8).
- Produces:
  - `score(hop: int, confidence: str, salience: float) -> float`
  - `salience(store, rev, node_id) -> float`
  - `impact_report(store, rev, node_id, max_hops=3, limit=40, include_low=False) -> Report`
  - `cli`: `codegraph impact <sym> [--hops N] [--all] [--json]`

Defaults: 3 hops, 40 rows. `LOW`-confidence hits are **counted in the summary but not listed** unless `--all`. Tests go in their own group, never mixed into the ranked dependents.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_impact.py
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.query.rank import score
from codegraph.store import Store


def build(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    return store


def listed(report):
    return {row.id for group in report.groups for row in group.rows}


def test_closer_hops_score_higher():
    assert score(1, "HIGH", 1.0) > score(3, "HIGH", 1.0)


def test_higher_confidence_scores_higher():
    assert score(1, "HIGH", 1.0) > score(1, "LOW", 1.0)


def test_direct_and_transitive_callers_are_found(repo, write):
    source = (
        "def target():\n    pass\n\n\n"
        "def direct():\n    target()\n\n\n"
        "def indirect():\n    direct()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    found = listed(impact_report(store, "HEAD", "m.py::target"))
    assert {"m.py::direct", "m.py::indirect"} <= found
    store.close()


def test_hop_limit_is_respected(repo, write):
    source = (
        "def target():\n    pass\n\n\n"
        "def h1():\n    target()\n\n\n"
        "def h2():\n    h1()\n\n\n"
        "def h3():\n    h2()\n\n\n"
        "def h4():\n    h3()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    found = listed(impact_report(store, "HEAD", "m.py::target", max_hops=2))
    assert "m.py::h2" in found
    assert "m.py::h4" not in found
    store.close()


def test_tests_are_bucketed_separately(repo, write):
    write("m.py", "def target():\n    pass\n", commit="m")
    write("tests/__init__.py", "", commit="pkg")
    write("tests/test_m.py", "from m import target\n\n\ndef test_target():\n    target()\n", commit="t")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target")
    test_groups = [g for g in report.groups if g.title == "tests"]
    assert test_groups and test_groups[0].rows
    other = {row.id for g in report.groups if g.title != "tests" for row in g.rows}
    assert "tests/test_m.py::test_target" not in other
    store.close()


def test_low_confidence_counted_but_not_listed(repo, write):
    write("one.py", "class One:\n    def shared(self):\n        pass\n", commit="1")
    write("two.py", "class Two:\n    def shared(self):\n        pass\n", commit="2")
    write("caller.py", "def go(thing):\n    thing.shared()\n", commit="c")
    store = build(repo)
    report = impact_report(store, "HEAD", "one.py::One.shared")
    assert "caller.py::go" not in listed(report)
    assert report.summary["low_confidence_hidden"] >= 1
    with_low = impact_report(store, "HEAD", "one.py::One.shared", include_low=True)
    assert "caller.py::go" in listed(with_low)
    store.close()


def test_summary_reports_reachable_effects(repo, write):
    source = (
        "import requests\n\n\n"
        "def target():\n    requests.get('u')\n\n\n"
        "def caller():\n    target()\n"
    )
    write("m.py", source, commit="m")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target")
    assert "NETWORK" in str(report.summary)
    store.close()


def test_limit_sets_truncated_flag(repo, write):
    lines = ["def target():\n    pass\n"]
    lines += [f"def c{i}():\n    target()\n" for i in range(60)]
    write("m.py", "\n\n".join(lines), commit="m")
    store = build(repo)
    report = impact_report(store, "HEAD", "m.py::target", limit=10)
    assert report.truncated is True
    store.close()
```

- [ ] **Step 2: Run to verify it fails, then implement**

`score(hop, confidence, salience) = (1.0 / hop) * {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.25}[confidence] * (1.0 + salience)`.

`salience` adds 0.5 when the node has no callers (an entry point), 0.3 when its qualname's last segment does not start with `_`, and `min(fan_in, 10) / 20` for fan-in.

`impact_report` runs a reverse BFS over `edges(rev, dst)` tracking hop and minimum path confidence, splits rows whose path starts with `tests/` or whose qualname starts with `test_` into a `tests` group, drops `LOW` rows into a hidden count unless `include_low`, then applies `budget(rows, limit)`. The summary carries `symbols`, `modules`, `entry_points`, `low_confidence_hidden`, and `effects_reachable`.

- [ ] **Step 3: Run tests, then commit**

```bash
uv run pytest tests/test_impact.py -v
git add src/codegraph/query tests/test_impact.py
git commit -m "feat: ranking and impact command"
```

---

### Task 12: The `diff` command

**Files:**
- Create: `src/codegraph/query/diff.py`
- Modify: `src/codegraph/cli.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes: `Indexer.reconcile` for an arbitrary revision (Task 5), `impact_report` (Task 11), effects tables (Task 10).
- Produces:
  - `diff_report(store, indexer, base: str, head: str) -> Report`
  - `cli`: `codegraph diff [<base>..<head>] [--json]`, defaulting base to `merge_base(default_branch, HEAD)` and head to `WORKTREE`

**Comparison is on `body_hash`, never on line span.** Inserting a blank line at the top of a file must produce an empty diff.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diff.py
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.diff import diff_report
from codegraph.store import Store
from tests.conftest import git


def build(repo):
    store = Store.open(repo)
    return store, Indexer(repo, store, GitTreeSource(repo))


def rows(report, title):
    return {r.id for g in report.groups if g.title == title for r in g.rows}


def test_blank_line_insertion_produces_empty_diff(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "\n\n\ndef alpha():\n    return 1\n", commit="shift lines")
    report = diff_report(store, indexer, base, "HEAD")
    assert report.groups == []
    store.close()


def test_added_symbol_reported(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "def alpha():\n    return 1\n\n\ndef added():\n    pass\n", commit="add")
    report = diff_report(store, indexer, base, "HEAD")
    assert "a.py::added" in rows(report, "added")
    store.close()


def test_removed_symbol_reported(repo, write):
    write("b.py", "def beta():\n    pass\n", commit="b")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "b.py").unlink()
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "remove b")
    report = diff_report(store, indexer, base, "HEAD")
    assert "b.py::beta" in rows(report, "removed")
    store.close()


def test_changed_body_reported(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "def alpha():\n    return 999\n", commit="change body")
    report = diff_report(store, indexer, base, "HEAD")
    assert "a.py::alpha" in rows(report, "changed")
    store.close()


def test_newly_reachable_effect_is_headlined(repo, write):
    write("m.py", "def charge():\n    pass\n\n\ndef checkout():\n    charge()\n", commit="m")
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write(
        "m.py",
        "import requests\n\n\ndef charge():\n    requests.post('u')\n\n\ndef checkout():\n    charge()\n",
        commit="add network call",
    )
    report = diff_report(store, indexer, base, "HEAD")
    assert "NETWORK" in str(report.summary)
    store.close()


def test_base_revision_is_not_checked_out(repo, write):
    store, indexer = build(repo)
    base = git(repo, "rev-parse", "HEAD").strip()
    write("a.py", "def alpha():\n    return 2\n", commit="edit")
    before = (repo / "a.py").read_text()
    diff_report(store, indexer, base, "HEAD")
    assert (repo / "a.py").read_text() == before
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    store.close()
```

- [ ] **Step 2: Run to verify it fails, then implement**

If the base revision cannot be resolved — a shallow clone missing it, a detached HEAD with no default branch, or a non-git directory — `diff` reports the missing revision by name and exits 1. It never guesses a different base.

`diff_report` reconciles both revisions (the base is read through `git cat-file`, never checked out), then compares the `nodes` table for each: ids present only in head are `added`, only in base are `removed`, present in both with differing `body_hash` are `changed`. Edge sets are compared on `(src, dst, kind)`. `effects_reachable` for changed symbols in head minus the same set in base gives the newly-reachable effects that lead the summary.

- [ ] **Step 3: Run tests, then commit and open PR 3**

```bash
uv run pytest -v
git add src/codegraph/query/diff.py src/codegraph/cli.py tests/test_diff.py
git commit -m "feat: diff command comparing body hashes"
```

(PR 3 opens at the end of Task 13, which is the last task in this group.)

---

### Task 13: `gc` and `install-hooks`

**Files:**
- Create: `src/codegraph/maintenance.py`
- Modify: `src/codegraph/cli.py`
- Test: `tests/test_maintenance.py`

**Interfaces:**
- Consumes: `Store`, `gitio`.
- Produces:
  - `gc(store, keep_revs: set[str]) -> int` — deletes Layer 1 rows for blobs no retained revision references, returns the count removed
  - `install_hooks(root: Path) -> list[Path]` — writes `post-commit`, `post-checkout`, `post-merge`
  - `cli`: `codegraph gc`, `codegraph install-hooks`

**Hooks warm the cache only.** Each hook runs `codegraph index --quiet &` in the background. If a hook never fires, results are identical — only slower. A hook must never be required for correctness, and must never block the git operation.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_maintenance.py
import os
import stat

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.maintenance import gc, install_hooks
from codegraph.store import Store
from tests.conftest import git


def test_gc_removes_unreferenced_blobs(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    write("a.py", "def alpha():\n    return 2\n", commit="edit")
    indexer.reconcile("HEAD")
    before = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    removed = gc(store, {"HEAD"})
    after = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert removed >= 1
    assert after < before
    store.close()


def test_gc_keeps_blobs_of_retained_revisions(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    gc(store, {"HEAD"})
    rows = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert rows == 1
    store.close()


def test_install_hooks_writes_executable_hooks(repo):
    written = install_hooks(repo)
    assert {p.name for p in written} == {"post-commit", "post-checkout", "post-merge"}
    for path in written:
        assert os.stat(path).st_mode & stat.S_IXUSR
        assert "codegraph index" in path.read_text()


def test_hooks_do_not_block_the_git_operation(repo, write):
    install_hooks(repo)
    write("c.py", "def gamma():\n    pass\n", commit="with hooks installed")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
```

- [ ] **Step 2: Run to verify it fails, then implement, then commit**

```bash
uv run pytest tests/test_maintenance.py -v
git add src/codegraph/maintenance.py src/codegraph/cli.py tests/test_maintenance.py
git commit -m "feat: gc and warming-only git hooks"
git push -u origin feat/queries
gh pr create --title "Queries: impact, ranking, diff, maintenance" --body "Tasks 11-13."
```

---

### Task 14: Plugin packaging and SKILL.md

**Files:**
- Create: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `skills/codegraph/SKILL.md`
- Test: `tests/test_packaging.py`

**Interfaces:**
- Consumes: the finished CLI.
- Produces: an installable plugin — `/plugin marketplace add swalla02/codegraph`.

**Before writing `.mcp.json` or any plugin-root path variable, check the current Claude Code plugin documentation.** v1 ships no MCP server, so no `.mcp.json` is needed; `SKILL.md` teaches the agent to invoke the CLI directly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_packaging.py
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_is_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "codegraph"
    assert data["version"]
    assert data["description"]


def test_marketplace_lists_the_plugin():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    names = {p["name"] for p in data["plugins"]}
    assert "codegraph" in names
    assert data["plugins"][0]["source"] == "./"


def test_skill_has_frontmatter_and_covers_triggers():
    text = (ROOT / "skills" / "codegraph" / "SKILL.md").read_text()
    assert text.startswith("---")
    assert "name: codegraph" in text
    assert "description:" in text
    lowered = text.lower()
    for trigger in ["what breaks", "safe to change", "grep"]:
        assert trigger in lowered


def test_skill_documents_every_shipped_command():
    text = (ROOT / "skills" / "codegraph" / "SKILL.md").read_text()
    for command in ["codegraph resolve", "codegraph impact", "codegraph effects", "codegraph diff"]:
        assert command in text
```

- [ ] **Step 2: Write the manifests and skill**

`SKILL.md` frontmatter is `name: codegraph` plus a `description:` beginning "Use when…". Body must cover, in this order:

1. **Trigger** — before modifying any function; when asked "what breaks if…", "what does this affect", "is this safe to change", "what did this branch change".
2. **Workflow** — `codegraph resolve <name>` to get an id, then `codegraph impact <id>` and `codegraph effects <id>`, then read only the top-ranked hits.
3. **The anti-pattern it displaces** — do not grep for callers: grep misses dynamically dispatched calls and gives no way to know when you are done.
4. **Reading the output** — `LOW`-confidence hits are counted but hidden; pass `--all` to see them. Tests are bucketed separately on purpose.

- [ ] **Step 3: Run tests, then commit**

```bash
uv run pytest tests/test_packaging.py -v
git add .claude-plugin skills tests/test_packaging.py
git commit -m "feat: plugin packaging and SKILL.md"
```

---

### Task 15: Perf smoke and accuracy harness

**Files:**
- Create: `tests/test_perf.py`, `tests/test_accuracy.py`, `tests/fixtures/labelled_calls.json`
- Test: itself

**Interfaces:**
- Consumes: everything.
- Produces: `measure_accuracy(store, rev, labels) -> tuple[float, float]` returning `(precision, recall)`.

Both are marked `slow` and excluded from the default run by the `addopts` set in Task 1.

- [ ] **Step 1: Write the tests**

```python
# tests/test_perf.py
import time

import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.query.impact import impact_report
from codegraph.store import Store
from tests.conftest import git


@pytest.mark.slow
def test_cold_index_and_warm_query_are_fast(tmp_path):
    repo = tmp_path / "flask"
    git(tmp_path, "clone", "-q", "--depth", "50", "https://github.com/pallets/flask", str(repo))

    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))

    started = time.perf_counter()
    stats = indexer.reconcile("HEAD")
    cold = time.perf_counter() - started
    assert stats.paths_total > 50
    assert cold < 60.0, f"cold index took {cold:.1f}s"

    node = store.connection.execute(
        "SELECT id FROM nodes WHERE rev='HEAD' AND kind='function' LIMIT 1"
    ).fetchone()["id"]

    started = time.perf_counter()
    impact_report(store, "HEAD", node)
    warm = time.perf_counter() - started
    assert warm < 0.3, f"warm query took {warm * 1000:.0f}ms"
    store.close()


@pytest.mark.slow
def test_branch_switch_is_under_a_second(tmp_path):
    repo = tmp_path / "flask"
    git(tmp_path, "clone", "-q", "--depth", "50", "https://github.com/pallets/flask", str(repo))
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    git(repo, "checkout", "-q", "-b", "probe")

    started = time.perf_counter()
    stats = indexer.reconcile("HEAD")
    elapsed = time.perf_counter() - started
    assert stats.blobs_parsed == 0
    assert elapsed < 1.0, f"branch switch took {elapsed:.2f}s"
    store.close()
```

```python
# tests/test_accuracy.py
import json
from pathlib import Path

import pytest

from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import Store

LABELS = Path(__file__).parent / "fixtures" / "labelled_calls.json"


def measure_accuracy(store, rev, labels):
    """labels: [{"src": node_id, "expected": [node_id, ...]}]"""
    true_positive = predicted = actual = 0
    for label in labels:
        got = {
            row["dst"]
            for row in store.connection.execute(
                "SELECT dst FROM edges WHERE rev=? AND src=? AND kind='CALLS'",
                (rev, label["src"]),
            )
        }
        expected = set(label["expected"])
        true_positive += len(got & expected)
        predicted += len(got)
        actual += len(expected)
    precision = true_positive / predicted if predicted else 1.0
    recall = true_positive / actual if actual else 1.0
    return precision, recall


@pytest.mark.slow
def test_resolution_accuracy_meets_floor(repo, write):
    labels = json.loads(LABELS.read_text())
    for name, source in labels["files"].items():
        write(name, source)
    from tests.conftest import git
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "accuracy fixture")

    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    precision, recall = measure_accuracy(store, "HEAD", labels["calls"])
    print(f"precision={precision:.2f} recall={recall:.2f}")
    assert recall >= 0.90, "over-approximation is the design bias; recall must stay high"
    assert precision >= 0.60
    store.close()
```

- [ ] **Step 2: Build the labelled fixture**

`tests/fixtures/labelled_calls.json` holds a `files` map of at least 6 small modules exercising: plain imports, `from` imports with aliases, relative imports, `self` calls, inherited method calls, a duck-typed call with a unique name, and a duck-typed call with an ambiguous name. `calls` lists each call site with its expected target ids. Recall is the assertion that matters — the design deliberately over-approximates, so precision is reported and floored low.

- [ ] **Step 3: Run and commit**

```bash
uv run pytest -m slow -v
git add tests/test_perf.py tests/test_accuracy.py tests/fixtures
git commit -m "test: perf smoke and resolution accuracy harness"
git push -u origin feat/packaging
gh pr create --title "Packaging, perf smoke, accuracy harness" --body "Tasks 14-15."
```
