"""Caller discovery, scored against a run: grep against `codegraph impact` (#59).

`bench/score.py` asks how much of a run's call graph codegraph has. This asks
a narrower question, and the one `AGENTS.md` actually makes a claim about:
*when you want the callers of one symbol, does querying the graph find more of
them than grepping, and does it hand you less noise to read?*

The two differ because recall over every traced edge is dominated by the edges
nobody would ever ask about -- dunders, comprehensions, framework dispatch
through werkzeug. A tool can score 0.29 over all of them and still answer the
question a person asks about a named function.

## Where the right answer comes from

The trace, and nothing else. `tracer.py` records the calls that happened using
`sys.monitoring`, so a pair in it is a call by observation rather than by
deduction -- and, crucially, it is not codegraph's own output. A benchmark
whose gold answers came from `codegraph impact` would measure nothing but
codegraph's agreement with itself.

The cost of that honesty is that a trace is a lower bound: a caller the test
suite never exercised is missing from the gold set, and a tool that finds it is
marked down for finding something true. That asymmetry runs the same way for
both sides of the comparison, and it is why the number below the recall column
is not precision (see `ToolReport.conditional_precision`).

## What is deliberately excluded from the gold set

- *Indirect edges.* The trace marks a pair whose frames were separated by an
  out-of-repo frame -- `test_x` -> werkzeug's `Client.get` -> `FlaskClient.open`.
  No text in this repository names that pair, so no reader of the source could
  find it, and grading it would reward a tool for guessing. Excluding them
  biases this comparison AGAINST codegraph, which is the direction an author
  grading their own tool should choose: dynamic dispatch is precisely where
  grep has no chance.
- *Dunders and anonymous scopes as targets*, for the reason `score.py` gives:
  `__eq__` is invoked by syntax, and a comprehension has no definition to name.
- *Symbols outside the package root.* A question about a test helper is not the
  question `AGENTS.md` is about.

The remaining rule -- between `MIN_CALLERS` and `MAX_CALLERS` observed callers
-- is a question-shape filter, not a cherry-pick: one caller is not a question,
and forty is a listing rather than an answer. Everything that passes the filter
is asked. There is no top-N ranked by anything, because a ranking chosen by the
author is where a flattering number would come from.
"""

from __future__ import annotations

import ast
import re
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache

from bench.score import Trace
from codegraph.resolve import MODULE_SCOPE

#: Anonymous scopes and nested functions, as they appear in a qualname.
#: `collapse_anonymous` has already folded comprehensions into their enclosing
#: definition by the time a `Trace` exists; what is left to exclude as a
#: question TARGET is `<locals>`, a function nobody can name from outside.
NESTED = "<locals>"

MIN_CALLERS = 3
MAX_CALLERS = 9


def _qualname(node_id: str) -> str:
    return node_id.partition("::")[2]


def _path(node_id: str) -> str:
    return node_id.partition("::")[0]


def last_name(node_id: str) -> str:
    """The bare name a grep would be given: `Flask.run` -> `run`."""
    return _qualname(node_id).rpartition(".")[2]


@dataclass(frozen=True)
class Question:
    """One symbol, and the callers a run observed reaching it directly."""

    symbol: str
    callers: frozenset[str]
    #: How many hops back the gold set reaches. 1 is "who calls this"; 2 is
    #: the shape `AGENTS.md` actually claims -- "what breaks if I change it".
    hops: int = 1

    @property
    def name(self) -> str:
        return last_name(self.symbol)


def predecessors(direct: set[tuple[str, str]]) -> dict[str, set[str]]:
    by_target: dict[str, set[str]] = {}
    for src, dst in direct:
        by_target.setdefault(dst, set()).add(src)
    return by_target


def reachers(by_target: Mapping[str, set[str]], symbol: str, hops: int) -> set[str]:
    """Everything that reached `symbol` within `hops` observed calls.

    A breadth-first walk backwards over the run's own edges, which is the
    same walk `impact` does over the resolver's -- the comparison is between
    two sets of edges, not between two algorithms.
    """
    seen: set[str] = set()
    frontier = {symbol}
    for _ in range(hops):
        frontier = {
            caller for node in frontier for caller in by_target.get(node, ()) if caller not in seen
        }
        frontier -= {symbol}
        if not frontier:
            break
        seen |= frontier
    return seen


def questions(
    trace: Trace,
    *,
    package_root: str,
    hops: int = 1,
    min_callers: int = MIN_CALLERS,
    max_callers: int = MAX_CALLERS,
) -> list[Question]:
    """Every symbol the trace can pose a caller question about, in id order.

    The filter is always on the DIRECT caller count, whatever `hops` the gold
    set is built at, so asking the same symbols at depth 1 and depth 2 gives
    two answers about one question set rather than two different question
    sets that happen to share a name.

    Deterministic and unranked on purpose -- see this module's docstring.
    """
    by_target = predecessors(trace.edges - trace.indirect)
    found = [
        Question(symbol, frozenset(reachers(by_target, symbol, hops)), hops)
        for symbol, callers in by_target.items()
        if _askable(symbol, package_root) and min_callers <= len(callers) <= max_callers
    ]
    return sorted(found, key=lambda question: question.symbol)


def _askable(symbol: str, package_root: str) -> bool:
    if not _path(symbol).startswith(package_root):
        return False
    qualname = _qualname(symbol)
    if qualname == MODULE_SCOPE or NESTED in qualname:
        return False
    return not last_name(symbol).startswith("__")


@dataclass(frozen=True)
class Scope:
    """A definition's line span, for attributing a grep hit to its caller."""

    start: int
    end: int
    qualname: str


def scopes(source: str) -> list[Scope]:
    """Every `def` and `class` in a file, innermost last at equal starts.

    Classes are in here because a decorator application is a call made by the
    class body, and `@setupmethod` on a method is one of the call sites a grep
    for callers is supposed to find.

    A decorator line falls OUTSIDE the span of the function it decorates --
    `ast` starts a `def` at the `def` keyword -- so it attributes to the
    enclosing class or module, which is where the trace attributes it too.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[Scope] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}{child.name}"
                found.append(Scope(child.lineno, child.end_lineno or child.lineno, qualname))
                nested = (
                    f"{qualname}." if isinstance(child, ast.ClassDef) else f"{qualname}.{NESTED}."
                )
                walk(child, nested)
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def enclosing(file_scopes: Iterable[Scope], line: int) -> str:
    """The qualname of the innermost definition containing `line`."""
    containing = [scope for scope in file_scopes if scope.start <= line <= scope.end]
    if not containing:
        return MODULE_SCOPE
    return max(containing, key=lambda scope: (scope.start, -scope.end)).qualname


#: The two patterns an agent actually types. `CALL` is what somebody looking
#: for call sites writes; `WORD` is what they fall back to when the first
#: returns nothing, and it is the one that finds a decorator or a reference
#: passed as a value. Both are scored, because quoting only the worse of them
#: would be arguing against a straw grep.
PATTERNS = {
    "grep-call": r"\b{name}\s*\(",
    "grep-word": r"\b{name}\b",
}


@dataclass(frozen=True)
class GrepResult:
    callers: frozenset[str]
    #: Matching lines. The reading cost of the answer: every one has to be
    #: looked at by whoever asked, and most of them are not call sites.
    hits: int


#: Maximal runs of word characters -- the only thing either pattern in
#: `PATTERNS` can match. Both are anchored by `\b` on the left, and what
#: follows the name is a word boundary too (`\b`, or the whitespace and `(`
#: of the call form), so every match is exactly one of these runs.
WORDS = re.compile(r"\w+")


class Corpus:
    """The tree grep searches, with an index from name to the files holding it.

    The index changes no answer: a file whose text does not contain the name
    as a whole word cannot match either pattern, so skipping it skips nothing.
    It changes what the comparison costs to compute, and on django that is the
    difference between running it and not. A two-hop grep aims its second
    round at every name the first turned up -- 800 of them for `save` -- and
    scanning 2,932 files per name puts one question at ten minutes.

    Results are remembered per (name, pattern) because the rounds repeat
    themselves heavily: the callers of `save` and the callers of `delete`
    overlap almost entirely, and every question re-walks the same names.
    """

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = files
        self.holders: dict[str, list[str]] = {}
        for path, source in files.items():
            for word in set(WORDS.findall(source)):
                self.holders.setdefault(word, []).append(path)
        self._answers: dict[tuple[str, str], GrepResult] = {}

    def callers(self, name: str, pattern: str) -> GrepResult:
        key = (name, pattern)
        if key not in self._answers:
            self._answers[key] = self._search(name, pattern)
        return self._answers[key]

    def _search(self, name: str, pattern: str) -> GrepResult:
        expression = re.compile(pattern.format(name=re.escape(name)))
        callers: set[str] = set()
        hits = 0
        for path in self.holders.get(name, ()):
            source = self.files[path]
            lines = source.splitlines()
            matched = [index + 1 for index, text in enumerate(lines) if expression.search(text)]
            if not matched:
                continue
            hits += len(matched)
            file_scopes = _cached_scopes(source)
            for line in matched:
                callers.add(f"{path}::{enclosing(file_scopes, line)}")
        return GrepResult(frozenset(callers), hits)


def grep_callers(files: Mapping[str, str] | Corpus, name: str, pattern: str) -> GrepResult:
    """Run one pattern over the tree and attribute each hit to its definition.

    This models grep at its best, not grep as used: every hit is attributed
    perfectly, no file is skimmed, nothing is missed by a reader who stopped
    reading at the fortieth match. The comparison should lose to grep where
    grep can win.
    """
    return corpus(files).callers(name, pattern)


def corpus(files: Mapping[str, str] | Corpus) -> Corpus:
    """Accept either a tree or an already-indexed one.

    A caller that asks many questions of one tree should build the `Corpus`
    once and keep it, which is what `discovery.run` does; a caller with three
    files and one question should not have to care.
    """
    return files if isinstance(files, Corpus) else Corpus(files)


@cache
def _cached_scopes(source: str) -> tuple[Scope, ...]:
    """`scopes` again for every hit in the same file otherwise: a transitive
    grep re-reads the tree a dozen times, and parsing dominates the run."""
    return tuple(scopes(source))


def grep_rounds(
    files: Mapping[str, str] | Corpus, name: str, pattern: str, hops: int
) -> GrepResult:
    """Grep for callers, then grep for THEIR callers, `hops` times over.

    This is the workflow the instruction in `AGENTS.md` displaces: to answer
    "what breaks if I change this" with grep you search the name, read the
    hits, work out which definition each one is in, and search those names
    in turn. Modelled at its best again -- no hit misread, no round skipped
    -- so the cost is a lower bound on the real one.

    A module-level hit ends its branch: the callers of a module are its
    importers, and no grep for a name finds those. That dead end is a
    property of the workflow and is left in rather than papered over.
    """
    tree = corpus(files)
    callers: set[str] = set()
    hits = 0
    searched: set[str] = set()
    frontier = {name}
    for _ in range(hops):
        found: set[str] = set()
        for target in sorted(frontier - searched):
            searched.add(target)
            result = tree.callers(target, pattern)
            hits += result.hits
            found |= result.callers
        if not found:
            break
        callers |= found
        frontier = {
            last_name(node) for node in found if _qualname(node).rpartition(".")[2] != MODULE_SCOPE
        }
    return GrepResult(frozenset(callers), hits)


def impact_callers(payload: dict) -> frozenset[str]:
    """The dependents `codegraph impact --json` reported, as node ids."""
    return frozenset(
        row["id"] for group in payload.get("groups", ()) for row in group.get("rows", ())
    )


@dataclass(frozen=True)
class Outcome:
    """One tool's answer to one question, judged against the run."""

    question: Question
    tool: str
    found: frozenset[str]
    #: What the answer costs to read: grep's matching lines, or the rows a
    #: query printed. Not comparable to a token count; comparable to itself.
    cost: int

    @property
    def hit(self) -> frozenset[str]:
        return self.found & self.question.callers

    @property
    def missed(self) -> frozenset[str]:
        return self.question.callers - self.found

    @property
    def extra(self) -> frozenset[str]:
        return self.found - self.question.callers

    @property
    def recall(self) -> float:
        return len(self.hit) / len(self.question.callers) if self.question.callers else 1.0


@dataclass(frozen=True)
class ToolReport:
    """Every outcome for one tool, aggregated two ways.

    Micro-averaging (`recall`) answers "of all the calls that happened, how
    many would this tool have shown me". Per-question recall answers "on a
    typical question, how much does it find", and its spread is the part a
    single number hides: a tool that is perfect on half the questions and
    blind on the other half has the same micro average as one that is
    mediocre on all of them, and they are not the same tool.
    """

    tool: str
    outcomes: list[Outcome]
    #: Claimed callers whose caller ran during the trace and yet never made
    #: the call, and the total of claims in that judgeable position. See
    #: `conditional_precision`.
    testable: int
    observed: int

    @property
    def gold(self) -> int:
        return sum(len(outcome.question.callers) for outcome in self.outcomes)

    @property
    def hits(self) -> int:
        return sum(len(outcome.hit) for outcome in self.outcomes)

    @property
    def recall(self) -> float:
        return self.hits / self.gold if self.gold else 1.0

    @property
    def per_question(self) -> list[float]:
        return sorted(outcome.recall for outcome in self.outcomes)

    @property
    def perfect(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.missed)

    @property
    def cost(self) -> int:
        return sum(outcome.cost for outcome in self.outcomes)

    @property
    def yield_per_row(self) -> float:
        """True callers per line of output the reader has to get through.

        The number this comparison turns on. Recall says what an answer
        contains; this says what it costs to extract, and a tool that hands
        back every true caller inside fifteen thousand matching lines has not
        answered the question so much as restated it.
        """
        return self.hits / self.cost if self.cost else 0.0

    @property
    def conditional_precision(self) -> float:
        """Observed / testable among claimed callers that ran.

        Not precision, for the reason `score.py` gives at greater length: a
        claimed caller the suite never ran is unjudged, not wrong. A claimed
        caller that DID run and still never made the call is the only kind
        this evidence can speak against -- and even then a guarded branch is
        a legitimate explanation. Read it as an upper bound on how much noise
        a tool adds, not as a count of errors.
        """
        return self.observed / self.testable if self.testable else 1.0


def spread(values: list[float]) -> tuple[float, float, float]:
    """Median and quartiles, the shape a mean would hide."""
    if not values:
        return (0.0, 0.0, 0.0)
    if len(values) < 4:
        return (min(values), statistics.median(values), max(values))
    quartiles = statistics.quantiles(values, n=4, method="inclusive")
    return (quartiles[0], quartiles[1], quartiles[2])


def report(tool: str, outcomes: list[Outcome], executed: frozenset[str]) -> ToolReport:
    """Aggregate one tool's outcomes, counting the judgeable claims as it goes."""
    testable = 0
    observed = 0
    for outcome in outcomes:
        for claim in outcome.found:
            # The callee ran -- it is the target of an observed call -- so the
            # caller having run is the whole of "both endpoints executed".
            if claim in executed:
                testable += 1
                observed += claim in outcome.question.callers
    return ToolReport(tool, outcomes, testable, observed)


def format_reports(title: str, reports: Iterable[ToolReport]) -> str:
    """The table the command prints, and what gets quoted in the README."""
    lines = [title, ""]
    header = (
        f"{'tool':<16}{'recall':>8}{'found':>8}{'gold':>7}{'perfect':>9}"
        f"{'cond.prec':>11}{'cost':>8}{'yield':>8}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for item in reports:
        lines.append(
            f"{item.tool:<16}{item.recall:>8.2f}{item.hits:>8}{item.gold:>7}"
            f"{item.perfect:>4}/{len(item.outcomes):<4}{item.conditional_precision:>11.2f}"
            f"{item.cost:>8}{item.yield_per_row:>8.3f}"
        )
    lines.append("")
    lines.append("per-question recall (min, q1, median, q3, max):")
    for item in reports:
        values = item.per_question
        low, median, high = spread(values)
        lines.append(
            f"  {item.tool:<16}{min(values):>6.2f}{low:>7.2f}{median:>7.2f}"
            f"{high:>7.2f}{max(values):>7.2f}"
        )
    return "\n".join(lines)
