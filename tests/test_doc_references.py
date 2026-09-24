"""Keep the prose honest: the references it writes must still point at
something that exists.

This repository explains itself in prose, and that prose navigates by
citation -- `rank.fan_in`, `Ambiguity.caller_count`, `effects/propagate.py`.
Those citations are load-bearing and nothing checked them, so they rotted
silently: a rename leaves a comment pointing at a symbol that is gone, and
the reader who follows it loses more time than the comment ever saved.
`test_packaging.py` already pins the README's command table to the CLI; this
is the same idea carried into the explanatory prose.

What gets checked, and why the scope is this narrow
---------------------------------------------------

The whole difficulty is false positives. Prose here is full of dotted things
that are not references to this codebase: `codegraph.toml`, `item.save()` and
`app.db.save()` (invented examples), `requests.get` and `flask.Flask` (the
benchmark corpora), `typing.Protocol` (the standard library), `path::qualname`
(a node id), `e.g`. A check that fires on those gets deleted by the second
person who hits it, so this one is built to stay quiet:

* **Only inside backticks.** This repository writes every code reference as a
  code span, so the convention costs nothing and it drops prose words,
  abbreviations and URLs in one stroke.
* **Only names anchored to something we own.** A dotted name is checked only
  when its first segment names a module of this package, a class defined in
  it, or -- since prose says `catalog.fingerprint()` for a variable annotated
  `catalog: Catalog` -- the lower-cased form of such a class. Everything else
  is somebody else's name and is left alone.
* **Only when no reading works.** A head can name several things at once
  (`Report` is both `render.Report` and `bench/score.py`'s); the reference is
  accepted if any of them resolves it. Instance attributes count, read out of
  the class's own source, because `Ambiguity.by_name` is as real a reference
  as `Ambiguity.candidates` even though only one of them survives `getattr`.

The numbers are the argument for that narrowness. Run over the repository
untuned, "every dotted token in a comment or docstring" produces 600 hits, of
which 87 name anything this package defines: an 86% false-positive rate, and
a test nobody would keep. The rules above check 84 references -- 53 distinct
names -- and as of this writing every one of them is a real citation.

`docs/` and `.superpowers/` are excluded. They hold dated specs, plans and
completion reports: records of what was true on a particular day, which is
the one kind of prose that is *supposed* to keep citing the old name.

Why there are no line numbers left
----------------------------------

A `file:line` citation is the fastest-rotting reference of all -- correct
when written, wrong after any edit above it, and wrong silently. Three ways
to handle that were open (#58):

1. Check the file exists and stop writing line numbers.
2. Anchor to a symbol name instead of a line.
3. Check the cited line falls inside the cited symbol's span, which our own
   `nodes` table stores.

This takes 1 and 2 together: the file must exist, and a line number is a
failure. Option 3 is tempting because we are a codebase that indexes
codebases, but it is a circular dependency, not an elegance. The test would
be asserting that our documentation is correct *by way of* the indexer the
documentation describes, so an indexer regression would surface as a
documentation failure, and the documentation check could never be evidence
about the indexer while depending on it. It also cannot do the job: the
option verifies a `(symbol, line)` pair, and prose writes the file and the
line and no symbol at all, so the pairing would have to be guessed. Naming
the symbol -- which is what a reader wanted anyway, and what the scanner
above already checks -- is both cheaper and more durable.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import io
import pkgutil
import re
import textwrap
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Where prose is read from. `docs/` and `.superpowers/` are deliberately
#: absent -- see the module docstring.
PROSE_ROOTS = ("src/codegraph", "tests", "bench", "skills")
PROSE_FILES = ("README.md", "AGENTS.md")

#: Packages whose contents count as "ours" for the purpose of anchoring a
#: reference. `tests` is scanned for prose but is not an index source: its
#: fixtures define throwaway classes that would only add collisions.
INDEX_PACKAGES = ("codegraph", "bench")

#: A dotted name ending in one of these is a filename, not a symbol.
#:
#: `css` and `js` are here for the same reason the rest are, and they
#: earned their place the hard way: `view.css` and `view.js` are the two
#: files `viz/render.py` inlines into a generated page, and `view` is also
#: the lower-cased form of a class this package defines, so without this
#: the scanner read two filenames as attribute accesses that do not exist.
FILE_SUFFIXES = frozenset(
    {
        "py",
        "md",
        "toml",
        "json",
        "txt",
        "cfg",
        "ini",
        "yaml",
        "yml",
        "lock",
        "db",
        "sh",
        "sql",
        "css",
        "js",
        "html",
    }
)

#: Path prefixes a citation can be checked against. A path under `tests/` is
#: almost always an illustration of somebody else's repository layout --
#: `tests/support.py` as the archetypal test helper, `tests/model_fields/`
#: from django -- so `tests/` is not among them.
PATH_PREFIXES = ("src/codegraph/", "bench/", "skills/", ".claude-plugin/")

#: Directories inside the package that prose names package-relatively, as in
#: `effects/propagate.py` or `query/islands.py`.
PACKAGE_RELATIVE = ("effects/", "query/", "viz/")

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
# `jsonl` before `json`, or the alternation matches the shorter one and leaves
# a trailing `l` behind, turning a file that exists into one that does not.
# `jsonl` before `json` before `js`, for the same reason: the alternation
# takes the first branch that matches, and a shorter one first would cut a
# real filename in half.
_PATH = re.compile(r"[\w./-]+\.(?:py|md|toml|jsonl|json|js|css|html|sql|txt)(?::\d+)?")

_MISSING = object()


# -- reading the prose -------------------------------------------------------


def _prose(path: Path) -> list[tuple[int, str]]:
    """Every comment and docstring in a Python file, or every line of a
    Markdown one, as `(line number, text)`.

    Python source is read through `tokenize` and `ast` rather than line by
    line so that string *literals* -- the fixture sources tests are built
    from, which are full of invented module names -- are never mistaken for
    prose about this repository.
    """
    if path.suffix == ".md":
        return list(enumerate(path.read_text().splitlines(), 1))
    source = path.read_text()
    chunks = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                chunks.append((node.body[0].lineno, docstring))
    return chunks


@functools.cache
def _prose_files() -> list[Path]:
    paths = [ROOT / name for name in PROSE_FILES]
    for root in PROSE_ROOTS:
        paths.extend(sorted((ROOT / root).rglob("*.py")))
        paths.extend(sorted((ROOT / root).rglob("*.md")))
    return [path for path in paths if path.exists()]


def _code_spans(text: str) -> list[str]:
    return [match.group(1) for match in _CODE_SPAN.finditer(text)]


@functools.cache
def _citations(pattern: re.Pattern[str]) -> list[tuple[str, int, str]]:
    """Every backticked match of `pattern` in the prose, as
    `(path relative to the root, line, text)`."""
    found = []
    for path in _prose_files():
        relative = path.relative_to(ROOT).as_posix()
        for line, text in _prose(path):
            for span in _code_spans(text):
                found.extend((relative, line, m.group(0)) for m in pattern.finditer(span))
    return found


# -- what this package actually defines --------------------------------------


@functools.cache
def _index() -> tuple[dict[str, list[object]], dict[str, list[object]]]:
    """`(modules, classes)`, each mapping a name to every object it could
    mean. Built by import, so a re-export is as real as a definition."""
    modules: dict[str, list[object]] = {}
    classes: dict[str, list[object]] = {}
    for package_name in INDEX_PACKAGES:
        package = importlib.import_module(package_name)
        found = [package]
        for info in pkgutil.walk_packages(package.__path__, f"{package_name}."):
            if info.name.rpartition(".")[2].startswith("__"):
                continue  # `__main__` runs the CLI on import.
            found.append(importlib.import_module(info.name))
        for module in found:
            modules.setdefault(module.__name__.rpartition(".")[2], []).append(module)
            for name, value in vars(module).items():
                owner = getattr(value, "__module__", "") or ""
                if isinstance(value, type) and owner.partition(".")[0] in INDEX_PACKAGES:
                    classes.setdefault(name, []).append(value)
    return modules, classes


_MODULES, _CLASSES = _index()


@functools.cache
def _instance_attributes(cls: type) -> frozenset[str]:
    """Names the class assigns to `self` anywhere in its own body.

    `Ambiguity.by_name` is set in `__init__` and so is invisible to both
    `getattr` and `__annotations__`, but it is exactly the kind of name prose
    cites. Read it back out of the source.
    """
    names: set[str] = set()
    for base in getattr(cls, "__mro__", [cls]):
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(base)))
        except (OSError, TypeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
            )
            names.update(
                target.attr
                for target in targets
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            )
    return frozenset(names)


def _attribute(obj: object, name: str) -> tuple[bool, object]:
    """`(exists, value)`, where `value` is `_MISSING` for a name that is real
    but has no object behind it to walk any further into -- a dataclass field
    with no default, or an attribute only ever assigned on `self`.

    `exists` is a separate flag rather than a sentinel return because an
    attribute is allowed to *be* `None`: `Rule.confidence` defaults to it.
    """
    value = getattr(obj, name, _MISSING)
    if value is not _MISSING:
        return True, value
    if name in getattr(obj, "__annotations__", {}):
        return True, _MISSING
    if isinstance(obj, type) and name in _instance_attributes(obj):
        return True, _MISSING
    return False, _MISSING


def _heads(name: str) -> list[object]:
    """Everything the first segment of a dotted name could denote here."""
    return [
        *_MODULES.get(name, ()),
        *_CLASSES.get(name, ()),
        # `catalog.fingerprint()` where `catalog: Catalog`.
        *_CLASSES.get(name[:1].upper() + name[1:], ()),
    ]


def _resolve_symbol(dotted: str) -> tuple[bool, str]:
    """`(checked, failure)`. `checked` is False when the name is not anchored
    to anything this package defines; `failure` is empty when it resolves."""
    head, *rest = dotted.split(".")
    if rest[-1] in FILE_SUFFIXES:
        return False, ""
    candidates = _heads(head)
    if not candidates:
        return False, ""
    failure = ""
    for candidate in candidates:
        obj, walked, broke = candidate, head, ""
        for part in rest:
            exists, value = _attribute(obj, part)
            if not exists:
                broke = f"`{walked}` has no `{part}`"
                break
            walked, obj = f"{walked}.{part}", value
            if value is _MISSING:
                # Real, but opaque: stop here and accept what was verified
                # rather than inventing a failure we cannot substantiate.
                break
        if not broke:
            return True, ""
        failure = failure or broke
    return True, failure


# -- the checks --------------------------------------------------------------


def test_every_symbol_named_in_prose_exists():
    """A dotted name in a comment or docstring must resolve to something
    real, so that following a citation never dead-ends.

    If this fails on a name that is an invented example rather than a
    reference, the example has collided with one of our own module or class
    names -- rename the example.
    """
    stale = [
        f"{path}:{line}: `{name}` -- {failure}"
        for path, line, name in _citations(_DOTTED)
        for checked, failure in [_resolve_symbol(name)]
        if checked and failure
    ]
    assert not stale, "prose names symbols that do not exist:\n  " + "\n  ".join(stale)


def test_every_repository_path_named_in_prose_exists():
    """A cited path must name a file that is still there.

    Only paths inside the shipped package, the benchmark harness and the
    plugin are checked -- see `PATH_PREFIXES`.
    """
    missing = [
        f"{path}:{line}: `{cited}`"
        for path, line, cited in _citations(_PATH)
        for resolved in [_repository_path(cited.partition(":")[0])]
        if resolved is not None and not resolved.exists()
    ]
    assert not missing, "prose names files that do not exist:\n  " + "\n  ".join(missing)


def test_prose_cites_symbols_rather_than_line_numbers():
    """`file:line` is banned outright -- see the module docstring.

    A line number is correct when written and silently wrong after the next
    edit above it. Name the symbol instead: `resolve.py`'s `_module_local`
    survives everything except the rename that a reader needs to know about
    anyway, and which the symbol check above catches.
    """
    numbered = [
        f"{path}:{line}: `{cited}`"
        for path, line, cited in _citations(_PATH)
        if ":" in cited and _repository_path(cited.partition(":")[0]) is not None
    ]
    assert not numbered, (
        "prose cites line numbers, which rot; name the symbol instead:\n  " + "\n  ".join(numbered)
    )


def _repository_path(cited: str) -> Path | None:
    """The file a cited path claims to be, or None if the citation is not a
    claim about this repository at all.

    `src/flask/__init__.py` is not one -- it names a benchmark corpus that
    lives nowhere near this tree. Nor is an invented `pkg/c.py`. A bare
    basename is a claim only when exactly one file in the package answers to
    it: `propagate.py` does and `m.py`, the fixture module half these tests
    are written over, does not.
    """
    if cited.startswith(PATH_PREFIXES):
        return ROOT / cited
    if cited.startswith(PACKAGE_RELATIVE):
        return ROOT / "src" / "codegraph" / cited
    if "/" not in cited:
        matches = _by_basename().get(cited, ())
        return matches[0] if len(matches) == 1 else None
    return None


@functools.cache
def _by_basename() -> dict[str, tuple[Path, ...]]:
    found: dict[str, list[Path]] = {}
    for prefix in PATH_PREFIXES:
        for path in sorted((ROOT / prefix).rglob("*")):
            if path.is_file():
                found.setdefault(path.name, []).append(path)
    return {name: tuple(paths) for name, paths in found.items()}


# -- the scanner's own guard rails -------------------------------------------


def test_the_scanner_resolves_the_kinds_of_reference_this_repository_writes():
    """Pins what the scanner can and cannot see.

    A checker that quietly stops checking passes forever, and this one is a
    pile of judgement calls about which names are ours. These are the calls.
    """
    # Module attributes, class attributes, and attributes set on `self`.
    for real in ["rank.fan_in", "Ambiguity.candidates", "Ambiguity.by_name"]:
        assert _resolve_symbol(real) == (True, ""), real
    # A rename leaves this behind, and it is the whole point of the test.
    assert _resolve_symbol("rank.fan_out")[1]
    assert _resolve_symbol("Ambiguity.call_sites")[1]
    # Not ours, and never to be complained about.
    for foreign in ["requests.get", "item.save", "app.db.save", "codegraph.toml", "self.source"]:
        assert _resolve_symbol(foreign) == (False, ""), foreign


def test_the_scanner_tells_our_paths_apart_from_the_ones_prose_invents():
    """The other half of the judgement: which cited paths are claims about
    this repository at all."""
    for ours in ["src/codegraph/cli.py", "query/islands.py", "bench/score.py", "propagate.py"]:
        resolved = _repository_path(ours)
        assert resolved is not None and resolved.exists(), ours
    # A corpus, an invented package, and the fixture module the tests are
    # written over -- none of them say anything about this tree.
    for theirs in ["src/flask/__init__.py", "pkg/c.py", "m.py", "tests/support.py"]:
        assert _repository_path(theirs) is None, theirs


def test_the_scanner_still_has_prose_to_check():
    """A floor on coverage: if a refactor makes the anchoring stop matching,
    the symbol test goes green by checking nothing. It should go red here
    instead."""
    checked = [name for _, _, name in _citations(_DOTTED) if _resolve_symbol(name)[0]]
    assert len(checked) > 50, f"only {len(checked)} references are being checked"
    paths = [c for _, _, c in _citations(_PATH) if _repository_path(c.partition(":")[0])]
    assert len(paths) > 10, f"only {len(paths)} paths are being checked"
