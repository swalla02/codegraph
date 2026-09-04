"""The `orphans` report: functions the graph can only see being called from
the test tree.

Every other symbol-taking query needs the answer as its input. `resolve`,
`impact` and `effects` all start from a name, which means starting from a
suspicion. `islands` is the only global command, and it structurally cannot
find the bug class this one is for: a helper that is defined, tested, and
never invoked by the code that is supposed to invoke it. **A tested helper
is not a one-symbol island precisely because its test calls it** -- the
thing that lets the bug survive review is the thing that hides it from the
only view of the whole graph. (#37, found on a 948-file project where
`_get_mlx_info` was docstring'd "Surface MLX info in the doctor report",
covered by `test_doctor_has_mlx_info`, and never reached by `doctor`. The
test imported it and called it directly, so it passed either way.)

So this report asks the shape of that bug rather than its name: *which
functions have callers, all of which are tests?* On the same project that
question alone answers 280 functions -- a list nobody reads -- and three
further filters cut it to 5, of which 2 were real defects. The filters are
the report, not an option on it.

## The filters, and why each one earns its place

*Defined outside the test tree, under a source root.* A helper in
`tests/support.py` called only from tests is a fixture doing its job. Test
membership is `islands.is_test_path`, deliberately the same predicate that
decides whether a CALLER is a test, so the two halves of "test-only" cannot
disagree about what a test is.

*Every recorded caller is a test, and there is at least one.* The "at least
one" is what makes this report disjoint from `islands`: a function with no
recorded caller at all is a singleton island, `islands` already reports it,
and it is the case where a static call graph is weakest. A function whose
callers are all tests is a much stronger position -- something DOES call
it, codegraph found the call sites, and every one of them is a test.
Callers include the bare-name fan-out (`ambiguity.py`), because a
production reference the resolver could not pin down is still counter-
evidence and must not be discarded for being LOW.

*Private by name.* A public function called only by tests is very often the
package's public surface, called by code that is not in this repository at
all -- exactly the misreading of `islands`' `unexplained` bucket the README
warns about. A leading underscore is the author saying "no external caller
is supposed to exist", which is what makes the absence of an internal one
interesting. Dunders are excluded outright: `__init__` and `__delitem__`
are invoked by syntax, not by name, so nothing about their absence from the
source text is informative. `--include-public` relaxes this.

*Undecorated.* A decorator runs at definition time and can register, wrap
or replace what it decorates -- `@app.route`, `@pytest.fixture`,
`@singledispatch.register`. The call site is inside the framework and is
not in this tree at all. `--include-decorated` relaxes this.

*No bare-name reference anywhere in the source text.* This one is not
optional and there is no flag to turn it off, because without it the report
is actively harmful. A static call graph cannot see a function passed as a
VALUE: on the project above the raw query surfaced
`_run_bash_sandbox.<locals>.preexec` (handed to `subprocess`'s
`preexec_fn`) and `_handle_sigint` (registered through `signal.signal`).
Neither is dead, and a report that re-imports that hazard for every user
who runs it is worse than no report at all.

`blob_refs` cannot answer this: it records `call`, `base` and `global`
references, and a name used as a value is none of the three. `imports`
cannot either -- it would catch `from m import _helper` and miss both
examples above, which name the function from inside the file that defines
it. What IS available is the source text: the revision's tree is in `tree`,
and the same `TreeSource` the indexer reads blobs through will hand them
back. So the last filter is a deliberate, admitted grep -- `\\w+` tokens of
every non-test source file, intersected with the candidate names. It
over-matches by construction (a mention in a comment, a docstring or a
string literal counts as a reference) and over-matching is the safe
direction here: it can only remove a candidate, never invent one.

## What a row does NOT claim

Not that the function is dead, unused, or safe to delete. The graph's
knowledge is the subject of every sentence this report prints: *no caller
outside the test tree was recorded, and the name does not appear in the
source text.* Three of the five survivors on that project were deliberate
-- two documented back-compat shims and one import probe whose docstring
says test-only is the point. And a name assembled at runtime (`getattr`, a
registry keyed by string fragments, a plugin table, a `pyproject.toml`
entry point, a template) leaves nothing for either half of this report to
find. That caveat is therefore in the summary, on the page, above the rows,
rather than in prose the reader has to have already gone looking for.

What a row does claim is that a human should read the function, and that on
the repository this was built for, 2 of 5 such reads found a real defect in
a codebase with 19,960 tests.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from codegraph.ambiguity import Ambiguity
from codegraph.config import Config
from codegraph.query.islands import is_test_path
from codegraph.render import Group, Report, Row, budget
from codegraph.store import Store

#: How many of a candidate's test callers a row names before falling back to
#: a count. The reviewer's first move is to open the test and see what it
#: thinks the function is for, so the row hands over somewhere to start.
_CALLERS_PER_ROW = 2

#: Identifier-ish runs of the source text. Deliberately `\w+` rather than a
#: real tokenizer: this is a grep, it is documented as a grep, and the only
#: direction it can be wrong in is dropping a candidate. `\w` is
#: Unicode-aware for `str` patterns, so a non-ASCII identifier tokenizes the
#: way the parser reads it.
_WORD = re.compile(r"\w+")

#: What the summary says the report was computed from, in the same slot
#: `islands` puts its `basis`. Both exist so a row reads as a statement
#: about codegraph's knowledge and never as a verdict on the code.
BASIS = "recorded callers plus a name scan of the source text"

#: The blind spot, printed on every report, including an empty one. See the
#: module docstring: this is the sentence that has to survive being the only
#: line a hurried reader reads.
CAVEAT = (
    "a name resolved at runtime (getattr, a registry, an entry point) leaves"
    " no call site and no mention to find, so this is a list to review, not"
    " a list to delete"
)


class TreeSource(Protocol):
    """The one method this report needs of `indexer.TreeSource`.

    Narrowed to `read` on purpose: the revision's tree comes from the `tree`
    table, which `reconcile` has just written and which is exactly what the
    rest of the graph was built from, so re-asking the source for a tree
    would risk scanning a different one than the edges describe.
    """

    def read(self, shas: Iterable[str]) -> Iterable[tuple[str, bytes]]: ...


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _is_dunder(leaf: str) -> bool:
    """`__init__` yes, `_private` no. Invoked by syntax rather than by name,
    so its absence from the source text says nothing at all."""
    return len(leaf) > 4 and leaf.startswith("__") and leaf.endswith("__")


def _under_source_root(path: str, source_roots: tuple[str, ...]) -> bool:
    """Is this file under one of the configured source roots?

    With the default roots (`""` and `"src"`) the `""` entry matches
    everything, so on a default configuration the discriminating half of
    "defined in source" is the test-path exclusion the caller applies next.
    A project that narrows `source_roots` to `["src"]` gets a stricter
    report for free -- scripts, examples and benchmarks stop being
    candidates -- which is why this reads the config instead of hardcoding a
    prefix.
    """
    return any(not root or path.startswith(f"{root}/") for root in source_roots)


def _recorded_callers(store: Store, rev: str) -> dict[str, set[str]]:
    """dst -> every distinct node id with a recorded CALLS edge into it,
    self-calls excluded.

    One pass over the revision's edges, never a query per node, for the
    reason `islands` gives: on a 2,930-file repository this is ~395k rows and
    a lookup inside the loop would be the whole cost of the command.

    A recursive call is not a caller for this report's purpose. `_helper`
    calling itself would otherwise be a non-test caller of itself, hiding
    every recursive helper from the report while saying nothing whatever
    about who enters the recursion.
    """
    callers: dict[str, set[str]] = {}
    for row in store.connection.execute(
        "SELECT DISTINCT src, dst FROM edges WHERE rev=? AND kind='CALLS'", (rev,)
    ):
        if row["src"] != row["dst"]:
            callers.setdefault(row["dst"], set()).add(row["src"])
    return callers


def _source_tree(store: Store, rev: str, source_roots: tuple[str, ...]) -> dict[str, str]:
    """path -> blob sha for the revision's non-test source files.

    Read from `tree` rather than from the `TreeSource`: this has to be the
    same tree the edges were resolved against, and it is also the half of
    the tree the scan needs -- excluding the test tree here is what makes a
    mention *from a test* not count as a reference.
    """
    return {
        row["path"]: row["blob_sha"]
        for row in store.connection.execute("SELECT path, blob_sha FROM tree WHERE rev=?", (rev,))
        if not is_test_path(row["path"]) and _under_source_root(row["path"], source_roots)
    }


def _referenced_names(
    source: TreeSource,
    tree: dict[str, str],
    ranges: dict[str, list[tuple[str, int, int]]],
) -> set[str]:
    """Which of `ranges`' names appear as a word somewhere in `tree`'s source
    text, other than inside their own definitions.

    `ranges[name]` is every candidate definition of that name, as
    `(path, line_start, line_end)`. Those spans are what is discounted: a
    name's own `def` line, its docstring and any self-call in its own body
    are not references to it. Everything else in a non-test source file is
    -- including a mention in a comment or a string, which is the
    over-matching the module docstring promises.

    Two passes, because line numbers are only ever needed for a handful of
    files. The first tokenizes each file once into a set and intersects with
    the names: a hit in a file that does not define the name settles it
    immediately. Only a name whose sole hits are in files that DO define it
    reaches the second pass, which re-reads those files line by line. On a
    large tree the first pass is the whole cost, and it is one
    `set(findall)` per file.
    """
    names = set(ranges)
    if not names:
        return set()

    paths_by_sha: dict[str, list[str]] = {}
    for path, sha in tree.items():
        paths_by_sha.setdefault(sha, []).append(path)
    defining = {path for spans in ranges.values() for path, _, _ in spans}

    referenced: set[str] = set()
    # path -> the names hit in a file that defines them, for the line pass.
    undecided: dict[str, set[str]] = {}
    texts: dict[str, str] = {}
    for sha, payload in source.read(sorted(paths_by_sha)):
        # A blob is source, so a decode error is a broken file rather than
        # binary data; `replace` keeps the scan going on the rest of the
        # tree instead of failing the whole report over one file.
        text = payload.decode("utf-8", errors="replace")
        hits = set(_WORD.findall(text)) & names
        if not hits:
            continue
        for path in paths_by_sha[sha]:
            if path in defining:
                texts[path] = text
            for name in hits:
                if any(spanned == path for spanned, _, _ in ranges[name]):
                    undecided.setdefault(path, set()).add(name)
                else:
                    referenced.add(name)

    for path, hit_names in undecided.items():
        pending = hit_names - referenced
        text = texts.get(path)
        if text is None:
            # The source refused to hand this blob back, so the first pass'
            # hit cannot be attributed to a line. Treat it as a reference:
            # the safe direction is dropping the candidate.
            referenced |= pending
            continue
        spans = {
            name: [(start, end) for spanned, start, end in ranges[name] if spanned == path]
            for name in pending
        }
        for number, line in enumerate(text.splitlines(), 1):
            pending -= referenced
            if not pending:
                break
            words: set[str] | None = None
            for name in pending:
                if any(start <= number <= end for start, end in spans[name]):
                    continue
                if words is None:
                    words = set(_WORD.findall(line))
                if name in words:
                    referenced.add(name)
    return referenced


def orphans_report(
    store: Store,
    rev: str,
    source: TreeSource,
    config: Config | None = None,
    limit: int = 20,
    include_public: bool = False,
    include_decorated: bool = False,
) -> Report:
    """Functions defined outside the test tree whose every recorded caller is
    a test, cut down to a list a human can actually review.

    `source` has no default. The last filter needs the source text, and a
    report that quietly skipped it because nobody passed a source would be
    the one shape of this command that must not exist -- see the module
    docstring.
    """
    connection = store.connection
    source_roots = (config or Config()).source_roots

    callers = _recorded_callers(store, rev)
    ambiguity = Ambiguity(store, rev)

    # Every node's path, for classifying callers. Includes the synthetic
    # `path::<module>` nodes: a module-scope call is a real reference, and
    # its test-ness is its file's.
    paths: dict[str, str] = {}
    definitions: list[tuple[str, str, str, int, int, bool]] = []
    for row in connection.execute(
        "SELECT id, path, qualname, kind, line_start, line_end, decorators FROM nodes WHERE rev=?",
        (rev,),
    ):
        paths[row["id"]] = row["path"]
        if row["kind"] not in ("function", "method"):
            continue
        if is_test_path(row["path"]) or not _under_source_root(row["path"], source_roots):
            continue
        leaf = row["qualname"].rpartition(".")[2]
        definitions.append(
            (
                row["id"],
                row["path"],
                leaf,
                row["line_start"],
                row["line_end"],
                bool(row["decorators"]),
            )
        )

    # The report's own question, asked of every function before the name
    # filters narrow anything, so `test_callers_only` in the summary is the
    # size of the list a user would otherwise have been handed.
    test_callers_only = 0
    candidates: list[tuple[str, str, str, int, int, list[str]]] = []
    for node_id, path, leaf, line_start, line_end, decorated in definitions:
        found = set(callers.get(node_id, ()))
        found.update(src for src in ambiguity.callers(node_id) if src != node_id)
        if not found:
            # No recorded caller at all: a singleton island, which `islands`
            # already reports and which is the case a call graph is weakest
            # on. This report is deliberately disjoint from it.
            continue
        if not all(is_test_path(paths.get(src, src.partition("::")[0])) for src in found):
            continue
        test_callers_only += 1
        if _is_dunder(leaf) or (not include_public and not leaf.startswith("_")):
            continue
        if decorated and not include_decorated:
            continue
        candidates.append((node_id, path, leaf, line_start, line_end, sorted(found)))

    ranges: dict[str, list[tuple[str, int, int]]] = {}
    for _, path, leaf, line_start, line_end, _ in candidates:
        ranges.setdefault(leaf, []).append((path, line_start, line_end))
    referenced = _referenced_names(source, _source_tree(store, rev, source_roots), ranges)

    rows: list[Row] = []
    for node_id, path, leaf, line_start, _, test_callers in candidates:
        if leaf in referenced:
            continue
        named = test_callers[:_CALLERS_PER_ROW]
        detail = (
            f"{_plural(len(test_callers), 'test caller')}, none recorded outside the"
            f" test tree; name not mentioned in the source text; called from"
            f" {', '.join(named)}"
        )
        if len(test_callers) > len(named):
            detail += f" (+{len(test_callers) - len(named)} more)"
        rows.append(
            Row(
                id=node_id,
                location=f"{path}:{line_start}",
                detail=detail,
                score=float(len(test_callers)),
            )
        )

    # `budget` sorts by score (the number of tests exercising the function)
    # and is stable, so sorting by id first is what makes ties deterministic
    # -- and on a real repository most candidates have exactly one test, so
    # most of the report is a tie. Two runs of one command must print the
    # same rows in the same order.
    rows.sort(key=lambda row: row.id)
    kept, truncated = budget(rows, limit)

    summary = {
        "functions": len(definitions),
        # The bare query, before any of the name filters: the 280 on Soup.
        "test_callers_only": test_callers_only,
        # After the private/undecorated filters (whatever the flags left of
        # them): the 10.
        "candidates": len(candidates),
        # What the source-text scan removed, reported rather than hidden --
        # it is the filter that keeps a callback out of the report, and a
        # reader should be able to see it working.
        "name_referenced": len(candidates) - len(rows),
        "reported": len(rows),
        "basis": BASIS,
        "caveat": CAVEAT,
    }
    return Report(
        summary=summary,
        groups=[Group("orphans", kept)] if kept else [],
        truncated=truncated,
    )


__all__ = ["BASIS", "CAVEAT", "orphans_report"]
