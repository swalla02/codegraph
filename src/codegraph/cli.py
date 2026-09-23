"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codegraph import __version__, gitio, trace, tracer
from codegraph.ambiguity import Ambiguity
from codegraph.guide import guide_text
from codegraph.indexer import FsTreeSource, GitTreeSource, Indexer
from codegraph.init import SKIPPED, plan_init
from codegraph.maintenance import gc, plan_hooks
from codegraph.query.diff import MissingRevisionError, diff_report
from codegraph.query.effects import effects_report
from codegraph.query.history import history_range, history_report, range_report, walk_history
from codegraph.query.impact import impact_report
from codegraph.query.islands import islands_report
from codegraph.query.orphans import orphans_report
from codegraph.query.path import DEFAULT_HOPS, path_report
from codegraph.query.unknowns import DEFAULT_HOPS as UNKNOWNS_HOPS
from codegraph.query.unknowns import unknowns_report
from codegraph.render import Report, render_json, render_text
from codegraph.resolve import find_symbol
from codegraph.session_hook import ENV_VAR as SESSION_ENV_VAR
from codegraph.session_hook import HOOK_NAME, install_session_hook, uninstall_session_hook
from codegraph.sessions import session_log
from codegraph.store import WORKTREE, Store
from codegraph.uncertainty import is_incomplete

#: Exit code for a `--strict` run whose report carries a blocking unknown.
#:
#: Not `1` and not `2`, which the symbol-resolving convention below has
#: already spent on "nothing matched" and "more than one match". An agent
#: writing `codegraph impact X --strict && edit` has to be able to tell "I
#: could not find that symbol" from "I found it and my answer has a hole in
#: it": those call for opposite next moves, and a shared exit code would
#: hand back exactly the judgement call this flag exists to remove.
INCOMPLETE = 3


def open_workspace(root: Path) -> tuple[Store, Indexer]:
    """Open the store and build an indexer for `root`, choosing a git-backed
    tree source when `root` is a git repository and falling back to a plain
    filesystem walk otherwise."""
    store = Store.open(root)
    source = GitTreeSource(root) if gitio.is_repo(root) else FsTreeSource(root)
    return store, Indexer(root, store, source)


def _emit(report: Report, as_json: bool, strict: bool = False) -> int:
    """Print one report and return the exit code for it.

    The report is printed either way: `--strict` is a verdict on the
    answer, not a reason to withhold it, and a run that exits nonzero with
    nothing on stdout would leave a reader unable to see what the tool
    could not answer. The reasons repeat on stderr because that is the
    stream a `&&` chain's failure gets read from.
    """
    print(render_json(report) if as_json else render_text(report))
    if not strict or not is_incomplete(report):
        return 0
    blocking = [item.reason for item in report.unknowns if item.blocking]
    print(f"incomplete report: {', '.join(blocking)} -- see `unknowns`", file=sys.stderr)
    return INCOMPLETE


def _print_stats(stats, store: Store, rev: str) -> None:
    print(f"paths: {stats.paths_total} ({stats.paths_dirty} dirty)")
    print(f"blobs: {stats.blobs_parsed} parsed, {stats.blobs_cached} cached")
    print(f"edges: {stats.edges}, unresolved: {stats.unresolved}")
    if stats.observed_edges:
        # Separate from the edge count on purpose: those are what the
        # resolver deduced, these are what a run was seen doing, and the
        # pair is the point (#56). Printed only when a trace has been
        # imported, so `status` on an untraced repository is unchanged.
        print(f"observed: {stats.observed_edges} edge(s) from a trace -- see `codegraph trace`")
    if stats.ambiguous:
        print(
            f"ambiguous: {stats.ambiguous} bare-name reference(s), expanded at query time"
            " (see `impact --all`)"
        )
        # Two numbers, because the deferred fan-out has two sizes and they
        # diverge: django 54519 vs 37047, flask 787 vs 569.
        #
        # `stats.ambiguous` counts rows in `unresolved` -- one per reference
        # SITE. A function calling `item.save()` three times contributes three.
        #
        # `relationships` counts distinct (source, name) pairs instead, calls
        # and `class X(Base)` bases together, repeats collapsed. That is the
        # unit `rank.fan_in` adds to a node's dependents, and since #42 `impact`
        # walks both kinds, so it is the unit `impact` ranks by.
        #
        # Cost is one `Ambiguity` build, ~0.8s on django against an index run of
        # minutes, and only when this summary prints at all -- `index --quiet`,
        # the warming-hook path, returns before here.
        print(
            f"           {Ambiguity(store, rev).relationships()} distinct (source, name)"
            " relationship(s) -- what `impact` ranks by"
        )
    if stats.parse_errors:
        print(f"parse errors: {stats.parse_errors}")
    if stats.shadowed:
        print(f"warning: {stats.shadowed} shadowed definition(s)")


def _cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            stats = indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        _print_stats(stats, store, args.rev)
    finally:
        store.close()
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        if args.rebuild:
            # Both layers, and in this order. Clearing only `blobs` re-parsed
            # Layer 1 and then reconciled straight into the unchanged-tree fast
            # path, because the tree and the fingerprint were untouched -- so
            # Layer 2 was served from the previous build and `--rebuild` did
            # not rebuild. Dropping the `revisions` row is what makes the fast
            # path (and the narrowing that follows it) see no current state to
            # keep. See issue #30.
            #
            # The flag is most likely to be reached for by someone who already
            # suspects the graph is stale, which is exactly when silently
            # serving the old one is worst.
            store.connection.execute("DELETE FROM blobs")
            store.connection.execute("DELETE FROM revisions WHERE rev=?", (args.rev,))
            store.connection.commit()
        try:
            stats = indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        if args.quiet:
            # --quiet suppresses the stats chatter (this is what the
            # warming hooks invoke in the background), but a parse failure
            # is a real signal, not chatter -- let it through on stderr.
            if stats.parse_errors:
                print(f"parse errors: {stats.parse_errors}", file=sys.stderr)
        else:
            _print_stats(stats, store, args.rev)
    finally:
        store.close()
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    """Fuzzy symbol lookup. Ambiguity is reported, never silently picked."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        matches = find_symbol(store, args.rev, args.query)
        for row in matches:
            print(row["id"])
        if not matches:
            print(f"no symbol matching {args.query!r}", file=sys.stderr)
            return 1
        return 2 if len(matches) > 1 else 0
    finally:
        store.close()


def _cmd_effects(args: argparse.Namespace) -> int:
    """Report the side effects transitively reachable from a symbol."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        matches = find_symbol(store, args.rev, args.symbol)
        if not matches:
            print(f"no symbol matching {args.symbol!r}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(f"ambiguous symbol {args.symbol!r}:", file=sys.stderr)
            for row in matches:
                print(f"  {row['id']}", file=sys.stderr)
            return 2
        report = effects_report(store, args.rev, matches[0]["id"])
        return _emit(report, args.json, args.strict)
    finally:
        store.close()


def _cmd_impact(args: argparse.Namespace) -> int:
    """Report the ranked dependents of a symbol -- everything a change to
    it could break."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        if args.hops < 1:
            # `--hops 0` (or negative) is not "walk zero hops"; the reverse
            # BFS never runs and the report comes back with an empty
            # `dependents` group at exit 0 -- reading exactly as "nothing
            # depends on this," the confidently-wrong answer the design doc
            # says is worse than no tool at all. Reject it loudly instead
            # of returning a report that looks like a real, checked answer.
            print(f"--hops must be >= 1 (got {args.hops})", file=sys.stderr)
            return 1
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        matches = find_symbol(store, args.rev, args.symbol)
        if not matches:
            print(f"no symbol matching {args.symbol!r}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(f"ambiguous symbol {args.symbol!r}:", file=sys.stderr)
            for row in matches:
                print(f"  {row['id']}", file=sys.stderr)
            return 2
        report = impact_report(
            store,
            args.rev,
            matches[0]["id"],
            max_hops=args.hops,
            limit=args.limit,
            include_low=args.all,
        )
        return _emit(report, args.json, args.strict)
    finally:
        store.close()


def _one_symbol(store: Store, rev: str, name: str) -> tuple[str | None, int]:
    """Resolve one name to one node id under the `0`/`1`/`2` convention
    `resolve`, `impact` and `effects` share, reporting the failure on
    stderr as they do: nothing matched is `1`, more than one match is `2`
    with every candidate printed so the reader can re-run with a full id.

    A helper because `path` takes two symbols and has to apply the
    convention twice, and a rule stated twice in one function is a rule
    that will eventually be stated two different ways.
    """
    matches = find_symbol(store, rev, name)
    if not matches:
        print(f"no symbol matching {name!r}", file=sys.stderr)
        return None, 1
    if len(matches) > 1:
        print(f"ambiguous symbol {name!r}:", file=sys.stderr)
        for row in matches:
            print(f"  {row['id']}", file=sys.stderr)
        return None, 2
    return matches[0]["id"], 0


def _cmd_path(args: argparse.Namespace) -> int:
    """Report how two symbols are connected, in whichever direction, or
    which of the three ways they are not.

    Two symbols, so the resolving convention applies to each of them; the
    exit code is about resolving a name and nothing else, which is why a
    report saying the two are not connected at all still exits `0`. "They
    are on different islands" is an answer, and a command that signalled
    failure for it would be unusable in the `&&` chain an agent writes.
    """
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        if args.hops < 1:
            # The same refusal as `impact`, for the same reason: a walk
            # that never runs reports "no path" at exit 0, which reads as a
            # checked answer and is not one.
            print(f"--hops must be >= 1 (got {args.hops})", file=sys.stderr)
            return 1
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        from_id, code = _one_symbol(store, args.rev, args.a)
        if from_id is None:
            return code
        to_id, code = _one_symbol(store, args.rev, args.b)
        if to_id is None:
            return code
        report = path_report(
            store,
            args.rev,
            from_id,
            to_id,
            max_hops=args.hops,
            include_low=args.all,
        )
        return _emit(report, args.json, args.strict)
    finally:
        store.close()


def _cmd_unknowns(args: argparse.Namespace) -> int:
    """Report what codegraph does not know about one symbol.

    Takes a symbol, so it takes the `0`/`1`/`2` convention with it, through
    the same `_one_symbol` helper `path` uses -- and the convention keeps
    meaning what it means everywhere else: it is about resolving a name.
    A symbol with forty unresolved references still exits `0` without
    `--strict`, because a full list of what is unknown is an answer, and an
    agent's `&&` chain should be able to read it.
    """
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        node_id, code = _one_symbol(store, args.rev, args.symbol)
        if node_id is None:
            return code
        report = unknowns_report(
            store,
            args.rev,
            node_id,
            indexer.config,
            max_hops=args.hops,
            limit=args.limit,
        )
        return _emit(report, args.json, args.strict)
    finally:
        store.close()


def _cmd_islands(args: argparse.Namespace) -> int:
    """Report the connected components of the revision's call graph.

    Takes no symbol, so the `0`/`1`/`2` exit convention `resolve`,
    `impact` and `effects` share -- which is entirely about resolving a
    name to one node id -- has nothing to resolve and cannot apply. This
    command exits `0` on a report (an empty repository included: "no
    symbols" is a real answer, not a failure) and `1` only on a bad
    `--rev`, matching `status`/`index`.
    """
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        report = islands_report(store, args.rev, indexer.config, limit=args.limit)
        return _emit(report, args.json, args.strict)
    finally:
        store.close()


def _cmd_orphans(args: argparse.Namespace) -> int:
    """Report functions whose every recorded caller is a test.

    Global like `islands`, so it shares `islands`' exit convention rather
    than the `0`/`1`/`2` one the symbol-taking commands use: `0` on a report
    (an empty one included -- "nothing matched the filters" is a real
    answer), `1` only on a `--rev` that will not resolve.

    The indexer's tree source is passed straight through, because the last
    filter reads the revision's source text: see `query/orphans.py` for why
    that filter is not optional and has no flag.
    """
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1
        report = orphans_report(
            store,
            args.rev,
            indexer.source,
            indexer.config,
            limit=args.limit,
            include_public=args.include_public,
            include_decorated=args.include_decorated,
        )
        return _emit(report, args.json)
    finally:
        store.close()


def _diff_revspec(root: Path, revspec: str | None) -> tuple[str, str]:
    """Split `<base>..<head>` into its two sides. A bare `<base>` (no `..`)
    diffs it against the worktree. With no argument at all, base defaults
    to `merge_base(default_branch, HEAD)` and head to the worktree --
    "what has this branch changed so far."
    """
    if revspec:
        if ".." in revspec:
            base, _, head = revspec.partition("..")
            if not base:
                # "..HEAD" -- an empty base has no sensible default (unlike
                # an empty head, which reasonably falls back to WORKTREE),
                # so name the missing side rather than passing "" through
                # to raise a blank, nameless error later.
                raise MissingRevisionError("<base>")
            return base, head or WORKTREE
        return revspec, WORKTREE
    if not gitio.is_repo(root):
        raise MissingRevisionError("HEAD")
    try:
        branch = gitio.default_branch(root)
        base = gitio.merge_base(root, branch, "HEAD")
    except gitio.GitError as exc:
        raise MissingRevisionError(str(exc)) from exc
    return base, WORKTREE


def _cmd_diff(args: argparse.Namespace) -> int:
    """Report what changed between two revisions, compared on body_hash."""
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            base, head = _diff_revspec(root, args.revspec)
            report = diff_report(store, indexer, base, head)
        except MissingRevisionError as exc:
            print(f"revision not found: {exc.rev}", file=sys.stderr)
            return 1
        return _emit(report, args.json)
    finally:
        store.close()


def _cmd_history(args: argparse.Namespace) -> int:
    """Report the graph over a range of commits: for one symbol, the commits
    that changed its body, callees or reachable effects; with no symbol, what
    every commit added and removed.

    Two optional positionals, told apart by `..`: a symbol never contains
    one and a range always does, so `history X`, `history A..B` and
    `history X A..B` each mean the one thing. The exit convention is the
    symbol-taking commands' `0`/`1`/`2`, applied to the symbol as it stands
    at the head -- or, for one the range deleted, at the start.
    """
    root = Path(args.path).resolve()
    symbol, revspec = args.symbol, args.revspec
    if revspec is None and symbol and ".." in symbol:
        symbol, revspec = None, symbol
    store, indexer = open_workspace(root)
    try:
        try:
            base, head = history_range(root, revspec)
            walk = walk_history(store, indexer, base, head, symbol)
        except MissingRevisionError as exc:
            print(f"revision not found: {exc.rev}", file=sys.stderr)
            return 1
        if symbol is None:
            return _emit(range_report(walk, limit=args.limit), args.json, args.strict)
        if not walk.steps:
            # An empty range has no revision to resolve the name at, and
            # nothing in it could have changed the symbol either way.
            return _emit(history_report(walk, symbol, limit=args.limit), args.json, args.strict)
        forward = not walk.head_matches
        matches = walk.start_matches if forward else walk.head_matches
        if not matches:
            print(f"no symbol matching {symbol!r} at {head} or {base}", file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(f"ambiguous symbol {symbol!r}:", file=sys.stderr)
            for node_id in matches:
                print(f"  {node_id}", file=sys.stderr)
            return 2
        report = history_report(walk, matches[0], forward=forward, limit=args.limit)
        return _emit(report, args.json, args.strict)
    finally:
        store.close()


def _cmd_gc(args: argparse.Namespace) -> int:
    """Prune Layer 1 (the blob parse cache) down to what HEAD, the worktree,
    and any `--keep`-named revisions still reference. Never touches Layer 2,
    so this can never make an existing answer stale -- only slower to
    rebuild for an evicted revision."""
    root = Path(args.path).resolve()
    store = Store.open(root)
    try:
        keep_revs = {"HEAD", WORKTREE, *args.keep}
        removed = gc(store, keep_revs)
        print(f"gc: removed {removed} blob(s) unreferenced by {', '.join(sorted(keep_revs))}")
    finally:
        store.close()
    return 0


def _cmd_install_hooks(args: argparse.Namespace) -> int:
    """Install post-commit/post-checkout/post-merge hooks that warm the
    cache in the background. Purely an optimization -- see D5: every query
    reconciles the working tree itself, so results are identical whether or
    not these ever fire. A hook whose pre-existing script isn't
    shell-compatible is skipped rather than corrupted -- reported by name
    and reason on stderr, never silently, since a silent skip would be the
    same trust failure as the corruption it avoids.
    """
    root = Path(args.path).resolve()
    try:
        results = plan_hooks(root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for result in results:
        if result.installed:
            print(result.path)
        else:
            print(f"skipped {result.name}: {result.reason}", file=sys.stderr)
    return 0


def _cmd_install_session_hook(args: argparse.Namespace) -> int:
    """Install, or with `--uninstall` remove, the opt-in `prepare-commit-msg`
    hook that writes `Session: $CODEGRAPH_SESSION` into commit messages.

    Its own command rather than a flag on `install-hooks`, because it is the
    one thing codegraph can install that changes what a user commits: see
    `session_hook.py`. Skips are loud on stderr for the reason
    `_cmd_install_hooks` gives.
    """
    root = Path(args.path).resolve()
    try:
        if args.uninstall:
            removal = uninstall_session_hook(root)
            if removal.reason:
                print(f"skipped {HOOK_NAME}: {removal.reason}", file=sys.stderr)
                return 1
            if removal.removed:
                print(f"removed the session trailer from {removal.path}")
            else:
                print(f"no session trailer installed in {removal.path}")
            return 0
        result = install_session_hook(root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not result.installed:
        print(f"skipped {result.name}: {result.reason}", file=sys.stderr)
        return 1
    print(result.path)
    print(
        f"commits made with {SESSION_ENV_VAR} set will carry a `Session:` trailer"
        " -- this hook writes into commit messages",
        file=sys.stderr,
    )
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    """List the session pointers commits carry as `Session:` trailers.

    Reads git and nothing else -- no reconcile, no store -- because the link
    lives in the history, not in the index (see `sessions.py`). Only commits
    that carry a pointer are listed, so a repository with none prints
    nothing (or `[]`) and exits `0`: an absence is an answer. A directory
    that is not a git repository has no history to read, and says so on
    stderr the way `init` does, without failing. `1` is kept for a revspec
    that does not resolve, matching every other command's bad `--rev`.
    """
    root = Path(args.path).resolve()
    if not gitio.is_repo(root):
        print(f"note: {root} is not a git repository", file=sys.stderr)
        records = []
    else:
        try:
            records = [r for r in session_log(root, args.revspec) if r.sessions]
        except gitio.GitError:
            print(f"revision not found: {args.revspec}", file=sys.stderr)
            return 1
    if args.json:
        payload = [{"commit": r.commit, "sessions": r.sessions} for r in records]
        print(json.dumps(payload, indent=2))
        return 0
    for record in records:
        for pointer in record.sessions:
            print(f"{record.commit}  {pointer}")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Make this repository's coding agents aware of codegraph: an AGENTS.md
    section, the CLAUDE.md bridge if there is a CLAUDE.md, and an inert
    codegraph.toml stub. Idempotent and additive -- see `codegraph.init`.

    Notably absent: git hooks. Those stay behind `install-hooks`, which the
    user has to ask for by name.
    """
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    if not gitio.is_repo(root):
        # Not fatal: codegraph indexes a plain directory too (see
        # `open_workspace`'s FsTreeSource fallback), and the files written
        # here are ordinary repo-root markdown that make just as much sense
        # without git. Worth saying out loud, though -- `--path` pointing
        # one directory off is a much likelier explanation than a
        # deliberately un-versioned project.
        print(f"note: {root} is not a git repository", file=sys.stderr)

    failed = False
    for result in plan_init(root):
        line = f"{result.action:<9} {result.path.relative_to(root)}"
        if result.reason:
            line += f" -- {result.reason}"
        if result.action == SKIPPED:
            print(line, file=sys.stderr)
            failed = True
        else:
            print(line)
    return 1 if failed else 0


def _cmd_trace(args: argparse.Namespace) -> int:
    """Import, describe or forget the observed run bound to a revision.

    A command rather than a flag on every query, for two reasons. An import
    is a write: it changes every answer for the revision until it is
    forgotten, and durable state belongs in the store beside the graph, not
    in an argument each caller has to remember to repeat. And a flag would
    make the answer depend on whether the person asking happened to know the
    file existed -- which is exactly the failure mode the `git status` model
    (see the README) was chosen to avoid everywhere else.

    Importing reconciles afterwards rather than leaving the graph for the
    next query to rebuild: the numbers it prints ("N add one the resolver
    did not have") come from the projection, and a command that reported
    them without having done the work would be reporting an estimate.
    """
    root = Path(args.path).resolve()
    store, indexer = open_workspace(root)
    try:
        try:
            indexer.reconcile(args.rev)
        except gitio.GitError:
            print(f"revision not found: {args.rev}", file=sys.stderr)
            return 1

        if args.forget:
            observed = trace.forget(store, args.rev)
            if not observed:
                print(f"no trace imported for {args.rev}", file=sys.stderr)
                return 1
            indexer.reconcile(args.rev)
            print(f"forgot the trace for {args.rev} ({observed} observed calls)")
            return 0

        if args.file:
            path = Path(args.file)
            try:
                payload = json.loads(path.read_text())
            except OSError as exc:
                print(f"cannot read {path}: {exc.strerror}", file=sys.stderr)
                return 1
            except json.JSONDecodeError as exc:
                print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
                return 1
            try:
                result = trace.import_trace(store, args.rev, payload, str(path))
            except trace.TraceMismatch as exc:
                print(str(exc), file=sys.stderr)
                return 1
            indexer.reconcile(args.rev)
            print(f"imported {result.observed} observed calls from {path}")

        text = trace.describe(store, args.rev)
        if not text:
            print(_no_trace_text(args.rev))
            return 0
        print(text)
        return 0
    finally:
        store.close()


def _no_trace_text(rev: str) -> str:
    """What to print when there is nothing to describe.

    The recipe, not just the absence. Producing a trace means running the
    program under `codegraph.tracer` in the environment that can actually
    run it, which is the one piece of this feature a reader cannot infer
    from the command's own help -- and the tracer's path on disk is the
    part they would otherwise have to go looking for.
    """
    return (
        f"no trace imported for {rev}\n\n"
        "Record one by running the program -- usually its test suite -- under the\n"
        "tracer, using the interpreter that can run it:\n\n"
        f"    python {tracer.__file__} --root . --out trace.json -- -q\n\n"
        "then `codegraph trace trace.json`. Nothing else changes: a graph with no\n"
        "trace answers exactly as it does today."
    )


def _cmd_guide(args: argparse.Namespace) -> int:
    """Print the agent-facing workflow. The AGENTS.md block `init` writes
    stays short by pointing here instead of inlining this."""
    print(guide_text(), end="")
    return 0


#: One sentence, used by every command that can be incomplete, because
#: `--strict` has to mean one thing across all of them. Spelling it out per
#: command is how two flags with one name come to behave differently.
_STRICT_HELP = (
    f"Exit {INCOMPLETE} when the report carries an unknown that could make acting"
    " on it wrong. A LOW-confidence row is an answer and does not count; a hop"
    " budget the walk did not exhaust does"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codegraph")
    parser.add_argument("--version", action="version", version=__version__)
    parser.set_defaults(handler=None)
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="Reconcile and summarize a revision")
    status_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    status_parser.add_argument("--rev", default=WORKTREE, help="Revision to reconcile")
    status_parser.set_defaults(handler=_cmd_status)

    index_parser = subparsers.add_parser("index", help="Reconcile a revision into the graph")
    index_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    index_parser.add_argument("--rev", default=WORKTREE, help="Revision to reconcile")
    index_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Discard the parse cache AND the revision's graph, forcing a cold rebuild",
    )
    index_parser.add_argument(
        "--quiet", action="store_true", help="Suppress stats output (used by warming hooks)"
    )
    index_parser.set_defaults(handler=_cmd_index)

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a name to node ids")
    resolve_parser.add_argument("query", help="Node id, qualname, or trailing name")
    resolve_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    resolve_parser.add_argument("--rev", default=WORKTREE, help="Revision to resolve against")
    resolve_parser.set_defaults(handler=_cmd_resolve)

    effects_parser = subparsers.add_parser(
        "effects", help="Report side effects reachable from a symbol"
    )
    effects_parser.add_argument("symbol", help="Node id, qualname, or trailing name")
    effects_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    effects_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    effects_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    effects_parser.add_argument("--strict", action="store_true", help=_STRICT_HELP)
    effects_parser.set_defaults(handler=_cmd_effects)

    impact_parser = subparsers.add_parser("impact", help="Report the ranked dependents of a symbol")
    impact_parser.add_argument("symbol", help="Node id, qualname, or trailing name")
    impact_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    impact_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    impact_parser.add_argument(
        "--hops", type=int, default=3, help="Maximum hops to walk (default: 3)"
    )
    impact_parser.add_argument(
        "--all",
        action="store_true",
        help="Merge LOW-confidence dependents into the main groups instead of sampling them",
    )
    impact_parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help=(
            "Maximum rows to keep, total across dependents and tests (default: 40)."
            " This is the bound on the bare-name fan-out too -- it is a property of"
            " the question, not of the graph"
        ),
    )
    impact_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    impact_parser.add_argument("--strict", action="store_true", help=_STRICT_HELP)
    impact_parser.set_defaults(handler=_cmd_impact)

    path_parser = subparsers.add_parser(
        "path",
        help="Report how two symbols are connected, in either direction",
        description=(
            "Find the shortest chain of CALLS, INHERITS, IMPLEMENTS and REFERENCES"
            " edges connecting two symbols, in whichever direction it runs, with"
            " each hop's kind, confidence and call site -- and the path's own"
            " confidence, which is its weakest hop. Both directions are always"
            " checked and the one found is named. When there is no path, the report"
            " says which of three things is true: no chain within --hops (and how"
            " many it would take), no directed chain in either direction, or the two"
            " are on different islands, which means no walk can ever connect them."
        ),
    )
    path_parser.add_argument("a", metavar="A", help="Node id, qualname, or trailing name")
    path_parser.add_argument("b", metavar="B", help="Node id, qualname, or trailing name")
    path_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    path_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    path_parser.add_argument(
        "--hops",
        type=int,
        default=DEFAULT_HOPS,
        help=(
            f"Maximum hops to walk (default: {DEFAULT_HOPS}). Higher than"
            " `impact`'s, because this walk follows one chain rather than a"
            " widening frontier"
        ),
    )
    path_parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Walk LOW-confidence hops too, including the bare-name fan-out that is"
            " not in the stored graph at all -- matching `impact`'s flag"
        ),
    )
    path_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    path_parser.add_argument("--strict", action="store_true", help=_STRICT_HELP)
    path_parser.set_defaults(handler=_cmd_path)

    unknowns_parser = subparsers.add_parser(
        "unknowns",
        help="Report what codegraph cannot answer about a symbol",
        description=(
            "The mirror of `impact`: what this graph does NOT know about one"
            " symbol, and what would settle each part of it. Every reference in"
            " the body that produced no edge, with the reason, the raw name, the"
            " line and the candidate count; how many of the body's references"
            " resolved; whether the symbol sits in an island no recognised"
            " mechanism explains, and which mechanisms were checked; and whether"
            " an `impact` walk would stop on its hop budget rather than on the"
            " graph -- an incomplete answer that otherwise reads as a complete"
            " one. Every number is a count of rows the indexer already wrote, and"
            " every next action is one fixed string per reason."
        ),
    )
    unknowns_parser.add_argument("symbol", help="Node id, qualname, or trailing name")
    unknowns_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    unknowns_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    unknowns_parser.add_argument(
        "--hops",
        type=int,
        default=UNKNOWNS_HOPS,
        help=(
            f"The `impact` hop budget to answer about (default: {UNKNOWNS_HOPS},"
            " matching `impact`'s own -- the answer is only useful if it is about"
            " the walk you are going to run)"
        ),
    )
    unknowns_parser.add_argument(
        "--limit", type=int, default=40, help="Maximum reference rows to keep (default: 40)"
    )
    unknowns_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    unknowns_parser.add_argument("--strict", action="store_true", help=_STRICT_HELP)
    unknowns_parser.set_defaults(handler=_cmd_unknowns)

    islands_parser = subparsers.add_parser(
        "islands",
        help="Report the connected components of the call graph",
        description=(
            "Split the revision's CALLS, INHERITS, IMPLEMENTS and REFERENCES edges,"
            " read as undirected, into connected"
            " components. An island is a set of symbols that share some call"
            " relationship with each other and none with anything outside it. It is"
            " NOT a reachability result: a one-symbol island is not dead code, only a"
            " symbol whose calls in or out the resolver did not record -- dunders,"
            " decorators, framework dispatch and entry points leave no call site."
        ),
    )
    islands_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    islands_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    islands_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to keep, total across islands and singletons (default: 20)",
    )
    islands_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    islands_parser.add_argument("--strict", action="store_true", help=_STRICT_HELP)
    islands_parser.set_defaults(handler=_cmd_islands)

    orphans_parser = subparsers.add_parser(
        "orphans",
        help="Report functions whose every recorded caller is a test",
        description=(
            "Find the shape of a bug the other commands cannot: a function that is"
            " defined, tested, and never called by anything outside the test tree."
            " Such a function is NOT a one-symbol island -- its test calls it -- so"
            " `islands` structurally cannot surface it. Candidates are private by"
            " name, undecorated, defined outside the test tree, and never mentioned"
            " by name anywhere in the source text; that last filter is what keeps a"
            " callback handed to a library out of the list, and it has no off"
            " switch. This is NOT a dead-code report: a name resolved at runtime"
            " leaves nothing for either half of it to find. Read the rows, then read"
            " the functions."
        ),
    )
    orphans_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    orphans_parser.add_argument("--rev", default=WORKTREE, help="Revision to query")
    orphans_parser.add_argument(
        "--limit", type=int, default=20, help="Maximum rows to keep (default: 20)"
    )
    orphans_parser.add_argument(
        "--include-public",
        action="store_true",
        help=(
            "Keep functions without a leading underscore. Off by default: a public"
            " function called only by tests is usually the package's own surface,"
            " called by code that is not in this repository"
        ),
    )
    orphans_parser.add_argument(
        "--include-decorated",
        action="store_true",
        help=(
            "Keep decorated functions. Off by default: a decorator can register or"
            " replace what it decorates, so the call site is inside the framework"
        ),
    )
    orphans_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    orphans_parser.set_defaults(handler=_cmd_orphans)

    diff_parser = subparsers.add_parser(
        "diff", help="Report what changed between two revisions"
    )
    diff_parser.add_argument(
        "revspec",
        nargs="?",
        default=None,
        help="<base>..<head> (default: merge-base(default branch, HEAD)..WORKTREE)",
    )
    diff_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    diff_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    diff_parser.set_defaults(handler=_cmd_diff)

    history_parser = subparsers.add_parser(
        "history",
        help="Report how a symbol, or the graph, changed commit by commit",
        description=(
            "Walk the commits in <base>..<head> along the first-parent line, oldest"
            " first, comparing each with its parent the way `diff` compares two"
            " revisions. With a symbol: every commit that changed its body hash,"
            " its confident callees or the side effects reachable from it,"
            " following it across a move to another file or class -- a pairing by"
            " identical body that is reported with a confidence tier, MEDIUM if"
            " unique and LOW if not, and never presented as the same id. Without"
            " one: per commit, the symbols added, removed, moved and changed, and"
            " the edges and effects gained and lost. Only the named range is"
            " materialized, nothing is checked out, and every revision the walk"
            " created is discarded when it ends."
        ),
    )
    history_parser.add_argument(
        "symbol", nargs="?", default=None, help="Node id, qualname, or trailing name"
    )
    history_parser.add_argument(
        "revspec",
        nargs="?",
        default=None,
        help="<base>..<head> (default: merge-base(default branch, HEAD)..HEAD)",
    )
    history_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    history_parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum rows to keep per group -- per commit without a symbol (default: 40)",
    )
    history_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    history_parser.add_argument("--strict", action="store_true", help=_STRICT_HELP)
    history_parser.set_defaults(handler=_cmd_history)

    gc_parser = subparsers.add_parser(
        "gc", help="Prune Layer 1 cache entries unreachable from retained revisions"
    )
    gc_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    gc_parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="REV",
        help="Additional revision to retain besides HEAD and the worktree (repeatable)",
    )
    gc_parser.set_defaults(handler=_cmd_gc)

    init_parser = subparsers.add_parser(
        "init", help="Make this repository's coding agents aware of codegraph"
    )
    init_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    init_parser.set_defaults(handler=_cmd_init)

    trace_parser = subparsers.add_parser(
        "trace",
        help="Import, describe or forget an observed run for a revision",
    )
    trace_parser.add_argument(
        "file",
        nargs="?",
        help="A trace JSON file to import (omit to describe the current one)",
    )
    trace_parser.add_argument("--forget", action="store_true", help="Discard the trace")
    trace_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    trace_parser.add_argument("--rev", default=WORKTREE, help="Revision to bind the trace to")
    trace_parser.set_defaults(handler=_cmd_trace)

    guide_parser = subparsers.add_parser("guide", help="Print the agent-facing workflow")
    guide_parser.set_defaults(handler=_cmd_guide)

    hooks_parser = subparsers.add_parser(
        "install-hooks", help="Install git hooks that warm the cache in the background"
    )
    hooks_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    hooks_parser.set_defaults(handler=_cmd_install_hooks)

    sessions_parser = subparsers.add_parser(
        "sessions",
        help="List the session pointers commits carry as `Session:` trailers",
        description=(
            "Read the `Session: <uri>` trailers in commit messages -- the pointer"
            " from a commit to the session (an agent conversation, a PR thread,"
            " notes) that produced it -- and list them as `<commit>  <pointer>`,"
            " newest first. The pointer is opaque: codegraph does not parse it or"
            " know which agent wrote it. Commits without one are not listed."
        ),
    )
    sessions_parser.add_argument(
        "revspec",
        nargs="?",
        default="HEAD",
        help="A revision or range, as `git log` takes it (default: HEAD)",
    )
    sessions_parser.add_argument("--path", default=".", help="Repository root (default: cwd)")
    sessions_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    sessions_parser.set_defaults(handler=_cmd_sessions)

    session_hook_parser = subparsers.add_parser(
        "install-session-hook",
        help=(
            "Opt in: a prepare-commit-msg hook that WRITES a `Session:` trailer"
            f" into commit messages when {SESSION_ENV_VAR} is set"
        ),
        description=(
            "Install a prepare-commit-msg hook that appends"
            f" `Session: ${SESSION_ENV_VAR}` to the commit message, through"
            f" `git interpret-trailers`, whenever {SESSION_ENV_VAR} is set and"
            " non-empty. Unlike `install-hooks` this changes what you commit, which"
            " is why it is its own command and nothing else installs it. An existing"
            " prepare-commit-msg hook is kept; merge and squash messages are left"
            " alone."
        ),
    )
    session_hook_parser.add_argument(
        "--path", default=".", help="Repository root (default: cwd)"
    )
    session_hook_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the block again, leaving the rest of the hook as it was",
    )
    session_hook_parser.set_defaults(handler=_cmd_install_session_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    if args.handler is None:
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except Exception as exc:  # noqa: BLE001 -- last-resort net, see comment below
        # Every command that can fail on user input (a bad --rev, a
        # non-git --path for install-hooks, ...) already has its own
        # targeted handler above, printing a clear one-line message. This
        # is the net underneath those: any exception a handler did not
        # anticipate still gets a one-line stderr message and a nonzero
        # exit here, never a raw traceback dumped at the user.
        print(f"error: {exc}", file=sys.stderr)
        return 1


def run() -> None:
    sys.exit(main())
