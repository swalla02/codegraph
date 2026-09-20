"""Phase 2: unresolved references -> edges, against a revision's symbol table.

Phase 1 (`parse.py`) is path-independent and records a reference as it was
written. This module is the opposite half: it never parses Python and never
shells out to git, but it does know every path in one revision. It turns
`blob_refs` into `edges`.

The `Resolver` protocol is the seam. `resolve_revision` builds the symbol
table for a revision and drives the writes; a resolver only answers
"what could this name mean here", so a smarter engine (or another language)
is a swap-in rather than a rewrite.

Over-approximation is the deliberate bias: a candidate is never dropped to
improve precision, only recorded at a lower confidence.
"""

from __future__ import annotations

import builtins
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol

from codegraph.config import Config
from codegraph.parse import (
    CALL,
    MODULE_SCOPE,
    OPAQUE,
    SUPER,
    VALUE_REF,
    ParsedRef,
    enclosing_function_scopes,
)
from codegraph.store import Store

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"

#: Canonical rank for comparing confidence tiers: higher is stronger. This
#: is the one place the tier order is defined -- `effects/propagate.py`,
#: `query/impact.py`, and `query/effects.py` all compare confidence tiers
#: and used to each redefine this table locally, with no guarantee they
#: agreed: two copies used higher-is-stronger, one used the opposite
#: polarity with the tiers spelled out as separate string literals rather
#: than these constants, so renaming a tier would have silently produced a
#: `KeyError` in whichever copy nobody happened to update. Importing this
#: one table (and `stronger`/`weaker` below) is the fix.
CONFIDENCE_RANK: dict[str, int] = {LOW: 0, MEDIUM: 1, HIGH: 2}


def stronger(a: str, b: str) -> str:
    """The more confident of two tiers (ties favor `a`)."""
    return a if CONFIDENCE_RANK[a] >= CONFIDENCE_RANK[b] else b


def weaker(a: str, b: str) -> str:
    """The less confident of two tiers (ties favor `a`)."""
    return a if CONFIDENCE_RANK[a] <= CONFIDENCE_RANK[b] else b


PROVENANCE = "static"

#: The kinds of edge this resolver writes.
#:
#: - `CALLS`: this call site may run that definition.
#: - `INHERITS`: this class lists that one among its bases.
#: - `IMPLEMENTS`: this class structurally satisfies that `typing.Protocol`,
#:   without naming it anywhere (see `protocol_implementations`).
#: - `REFERENCES`: this code evaluates that definition's name as a value and
#:   hands it somewhere (see the value-reference pass in `resolve_revision`).
CALLS, INHERITS, IMPLEMENTS, REFERENCES = "CALLS", "INHERITS", "IMPLEMENTS", "REFERENCES"

#: The kinds a change travels backwards along, and therefore the ones
#: `impact` walks and `islands` partitions on. Declared once, here, because
#: the two have to agree: an island is meant to bound what an unlimited-hop
#: `impact` walk could ever reach, and that claim is only checkable while
#: both read the same list. `effects` deliberately walks `CALLS` alone -- its
#: witness path is a chain of call sites, and neither a base class nor a
#: mention is one.
DEPENDENCY_KINDS: tuple[str, ...] = (CALLS, INHERITS, IMPLEMENTS, REFERENCES)

#: Why a reference produced no edge, as the `unresolved` rows spell it.
#:
#: - `UNKNOWN`: no candidate at all. The resolver is blind to something.
#: - `AMBIGUOUS`: too many candidates -- the bare-name fan-out, recorded once
#:   here instead of as N low-confidence edges (see `ambiguity.py`).
#: - `EXTERNAL`: the target is outside the repository, so no node in this
#:   graph can be it (see `is_external_call`).
#: - `BUILTIN`: a Python builtin the resolver understood and deliberately did
#:   not link to a repo symbol (see `is_builtin_call`).
#:
#: These were four string literals scattered across the write path until
#: #55, which gave each reason a next-action string and a test asserting
#: that mapping is total. A test can only be total over a set it can read,
#: so the set is declared here, beside the code that writes the rows, rather
#: than reconstructed by whoever consumes them.
UNKNOWN, AMBIGUOUS, EXTERNAL, BUILTIN = "unknown", "ambiguous", "external", "builtin"

#: Every reason, in the order a report lists them: the two gaps first,
#: because they are the ones a reader can act on, then the two the resolver
#: has already settled.
UNRESOLVED_REASONS: tuple[str, ...] = (UNKNOWN, AMBIGUOUS, EXTERNAL, BUILTIN)

#: Python builtins, as the names they are actually called by.
#:
#: These matter because the last-resort step matches a call's final dotted
#: segment against every definition in the repository, and plenty of builtins
#: share a name with a plausible method: `set`, `list`, `next`, `id`, `type`,
#: `format`, `hash`, `filter`, `map`, `open`, `sum`, `iter`, `compile`, `vars`.
#: On `psf/requests` that turned `badargs = set(kwargs) - set(result)` inside
#: `create_cookie` into an edge to `RequestsCookieJar.set`, which then carried
#: an effect into a witness path presented as clickable evidence. See #17.
BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))


def is_builtin_call(ref: ParsedRef) -> bool:
    """Is this reference a call to a Python builtin rather than a repo symbol?

    Only a BARE name can be: `x.set(...)` is a method call on something, and
    must keep falling through to the name match. A bare name is safe to claim
    here because the steps before this one have already ruled out every way a
    repo symbol could legitimately shadow the builtin -- a module-local
    `def set(...)` is caught at step 2 and an imported one at step 1, both at
    HIGH. So a bare builtin name arriving at the last-resort step is the
    builtin.
    """
    return not ref.dotted and ref.raw_name in BUILTIN_NAMES


def is_external_call(ref: ParsedRef, ctx: ResolveContext) -> bool:
    """Is this reference a call into a module the repository does not contain?

    `import pytest` then `pytest.main([...])`: the head is bound by an import,
    and the module it names is nowhere in the tree. Nothing in this graph can
    be the callee, because the graph never parses site-packages or the standard
    library -- so projecting `main` onto the repository's own `main` functions
    is not a weak answer, it is a wrong one. `bench/tracer.py`'s own
    `pytest.main(...)` call reported three such candidates; `json.dumps(x)`
    with a single repo `dumps` became a MEDIUM edge. See #47.

    The test is deliberately stricter than "the lookup failed". The head's
    import target is external only when its FIRST segment names no module, no
    package and no directory anywhere in the repository (`repo_segments`):

    - `import pkg` then `pkg.main()`, where `pkg` is a repo package but defines
      no `main`, is not external. The name may be bound at runtime, and it
      keeps the name match it always had.
    - `from model_fields.models import build`, where the file is
      `tests/model_fields/models.py` and a test runner put `tests/` on
      `sys.path`, is not external either. `module_for_path` spells that module
      `tests.model_fields.models`, so an exact module lookup would call it
      foreign -- but a head that names any path component in the tree is not
      provably someone else's code.

    Both errors fall in the same direction: a reference this cannot classify
    keeps today's behaviour. Only a head the repository could not possibly
    supply is written off, which is what makes it sound to claim.

    Like `is_builtin_call`, this is only consulted once every step that could
    find a repo symbol has declined: an imported name that DOES resolve was
    answered at step 1, and a module-local definition of the same name at step
    2.
    """
    head = ref.raw_name.partition(".")[0]
    target = ctx.import_map.get(head)
    if not target:
        return False
    if local_bindings(head, ref.from_qualname, ctx):
        # `def go(json): json.dumps()` -- the parameter shadows the import for
        # the whole body, so the call is on whatever was passed.
        return False
    return target.partition(".")[0] not in ctx.repo_segments


@dataclass(frozen=True)
class Binding:
    """One `blob_bindings` row, placed in its revision: where the name was bound
    (`path`, `scope`) and what the text says it was bound to."""

    path: str
    scope: str
    kind: str
    type: str | None


def local_bindings(name: str, scope: str, ctx: ResolveContext) -> list[Binding]:
    """Every binding of `name` visible from `scope` in `ctx.path`, from the
    nearest scope that binds it at all -- the function itself, then each
    enclosing function, then the module. Class bodies are skipped, as Python
    skips them. Empty if nothing binds it.

    The first scope with ANY binding wins, opaque ones included: a name bound
    in a function is local to it everywhere in its body, so a module-level
    `item = Item()` says nothing about a function that also loops over `item`.
    """
    chain = [scope, *enclosing_function_scopes(scope)]
    if MODULE_SCOPE not in chain:
        chain.append(MODULE_SCOPE)
    for candidate in chain:
        found = ctx.bindings.get((ctx.path, candidate, name))
        if found:
            return found
    return []


#: What a class must list among its bases to be a `typing.Protocol`.
PROTOCOL_BASES = frozenset({"typing.Protocol", "typing_extensions.Protocol"})


#: How many re-export hops `AstResolver._lookup_dotted` will follow.
#:
#: A chain is real -- `a/__init__.py` re-exports from `a/b.py`, which
#: re-exports from `a/c.py` -- but short: flask's whole public API is one hop,
#: and the deepest chain actually walked across the benchmark targets is two.
#: This bound is a cost and sanity guard with room to spare, NOT the
#: termination guarantee: that is the `seen` set in `_lookup_dotted`, since a
#: cycle necessarily reproduces a dotted name already tried. Every other walk
#: in this module (`breadth_first`, and `_mro`/`_descendants` on top of it) is
#: cycle-safe the same way.
REEXPORT_HOPS = 8


def module_for_path(path: str, source_roots: tuple[str, ...]) -> str:
    """'src/pay/service.py' -> 'pay.service'; 'pay/__init__.py' -> 'pay'."""
    trimmed = path
    for root in sorted(source_roots, key=len, reverse=True):
        prefix = f"{root}/" if root else ""
        if prefix and trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix) :]
            break
    trimmed = trimmed.removesuffix(".py").removesuffix("/__init__")
    return trimmed.replace("/", ".")


def package_for_module(module: str, path: str) -> str:
    """The package a module's relative imports are resolved against.

    A regular module's package is its parent; `pkg/__init__.py` *is* `pkg`,
    so its own name is the package.
    """
    if path.endswith("__init__.py"):
        return module
    return module.rpartition(".")[0]


def absolute_module(module: str, level: int, package: str) -> str:
    """Expand a `from ... import` target to an absolute module name.

    `level=1` is the importing file's own package, `level=2` its parent, and
    so on; `level=0` is already absolute.
    """
    if level == 0:
        return module
    parts = package.split(".") if package else []
    ascend = level - 1
    base = ".".join(parts[: len(parts) - ascend] if ascend else parts)
    if not module:
        return base
    return f"{base}.{module}" if base else module


@dataclass
class ResolveContext:
    """Everything a resolver may look at for one file in one revision.

    `qualname_index` and `name_index` are shared across every file of the
    revision and hold *live* bindings only: a shadowed definition keeps its
    node and can still be an edge target by another route, but it never wins
    a name lookup.
    """

    rev: str
    path: str
    module: str
    module_to_path: dict[str, str]
    qualname_index: dict[tuple[str, str], str]  # (path, qualname) -> node id, live only
    name_index: dict[str, list[str]]  # bare name -> node ids, live only
    import_map: dict[str, str]  # local alias -> dotted target
    #: EVERY path's alias map, not just this file's. Following a package
    #: re-export means reading what ANOTHER file imports: `flask.Flask` is
    #: answered by `src/flask/__init__.py`'s `from .app import Flask`.
    import_maps: dict[str, dict[str, str]]
    bases: dict[str, list[str]]  # class node id -> base class node ids
    enclosing_class: dict[str, str] = field(default_factory=dict)  # node id -> class node id
    subclasses: dict[str, list[str]] = field(default_factory=dict)  # inverse of `bases`
    #: Memo for `_descendants`, shared across every file of the revision. The
    #: hierarchy is fixed once inheritance has been resolved, but the walk runs
    #: per `self.X` reference -- on django that cost 3.5s of a 11.2s resolve.
    descendant_cache: dict[str, list[str]] = field(default_factory=dict)
    #: Every segment of every module name in the revision: `tests`,
    #: `model_fields` and `models` for `tests/model_fields/models.py`. A name
    #: outside this set cannot be supplied by the repository under any
    #: `sys.path` arrangement; see `is_external_call`.
    repo_segments: frozenset[str] = frozenset()
    #: (path, scope, name) -> how that name was bound there; see `Binding`.
    bindings: dict[tuple[str, str, str], list[Binding]] = field(default_factory=dict)
    #: Live class node ids.
    class_ids: frozenset[str] = frozenset()
    #: class node id -> its base references as written (`Protocol`,
    #: `typing.Protocol`), which is how a Protocol is recognised: `Protocol`
    #: lives outside the repository, so no INHERITS edge ever points at it.
    class_bases: dict[str, list[str]] = field(default_factory=dict)
    #: class node id -> the names of the methods it defines itself.
    class_members: dict[str, frozenset[str]] = field(default_factory=dict)
    #: node id -> the decorator names its definition carries, as written
    #: (`setupmethod`, `t.final`). Only decorated definitions are present.
    #: Read by `wraps_the_definition`, which is what decides whether the name
    #: of a definition still reaches the definition; see `tier_of_declaration`
    #: and the three steps that call it.
    decorators: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Memo for the receiver step's per-class answers (is it a Protocol, what
    #: does its MRO define, who implements it), shared across the revision like
    #: `descendant_cache`.
    receiver_cache: dict[tuple, object] = field(default_factory=dict)


def breadth_first(start: str, adjacency: dict[str, list[str]]) -> list[str]:
    """Breadth-first walk from `start` over `adjacency`, cycle-safe.

    Module level rather than a method because two different walks need it:
    the resolver's MRO/override walks over `bases`/`subclasses`, and
    `_inherited_constructor` below, which is not resolution of a name at
    all and so does not belong to a `Resolver`.
    """
    order, seen, queue = [], {start}, [start]
    while queue:
        current = queue.pop(0)
        order.append(current)
        for nxt in adjacency.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return order


def is_protocol(class_id: str, ctx: ResolveContext) -> bool:
    """Does the class list `typing.Protocol` among its OWN bases?

    Only its own: under PEP 544 a subclass of a Protocol that does not repeat
    `Protocol` in its bases is an ordinary class, and its implementations are
    nominal.
    """
    key = ("protocol", class_id)
    if key not in ctx.receiver_cache:
        path = class_id.partition("::")[0]
        import_map = ctx.import_maps.get(path, {})
        found = False
        for raw in ctx.class_bases.get(class_id, ()):
            head, _, rest = raw.partition(".")
            target = import_map.get(head)
            if target and (f"{target}.{rest}" if rest else target) in PROTOCOL_BASES:
                found = True
                break
        ctx.receiver_cache[key] = found
    return ctx.receiver_cache[key]  # type: ignore[return-value]


#: Decorators that leave a definition reachable by its own name.
#:
#: Everything else may not. `@decorator` rebinds the name to whatever the
#: decorator returns, so `App.route` need not hold the function written under
#: `def route`, and a call through it need not enter that body at all -- it
#: enters the wrapper, which may delegate, may register and return the
#: original, or may do something else entirely. Reading the decorator to find
#: out is a different analysis (it is a call to a function whose return value
#: this would have to model), and it is not one this resolver does.
#:
#: These seven are the exceptions, and they are exceptions for a reason that
#: can be checked rather than assumed: each is defined by the language, and
#: each leaves the decorated body as what an attribute access of that name
#: invokes. `staticmethod`, `classmethod` and `property` are descriptors over
#: the same function; `abstractmethod`, `overload`, `final` and `override`
#: are markers that return their argument. A repository's own decorator is
#: never on this list, however harmless it looks, because nothing here has
#: read it.
#:
#: Matched on the last dotted segment, as `parse.py` records them, so
#: `abc.abstractmethod`, `t.final` and a bare `final` are one entry.
TRANSPARENT_DECORATORS: frozenset[str] = frozenset(
    {
        "staticmethod",
        "classmethod",
        "property",
        "abstractmethod",
        "overload",
        "final",
        "override",
    }
)


def wraps_the_definition(node_id: str, ctx: ResolveContext) -> bool:
    """Might this definition's own name reach something other than its body?

    True when it carries a decorator `TRANSPARENT_DECORATORS` does not
    account for -- which is the resolver's cue that it has found the right
    definition but cannot promise the call arrives inside it.
    """
    return any(
        name.rpartition(".")[2] not in TRANSPARENT_DECORATORS
        for name in ctx.decorators.get(node_id, ())
    )


def tier_of_declaration(node_id: str, ref: ParsedRef, ctx: ResolveContext) -> str:
    """The tier for a declaration found by looking a name up on a class.

    HIGH, unless a decorator the language does not define stands between the
    name and the body (`wraps_the_definition`), in which case MEDIUM: the
    lookup found the right declaration and cannot promise the call arrives
    inside it. Shared by the three steps that reach a method through a class
    -- `_through_super`, `_through_self`, `_through_receiver` -- because it
    is one fact about the attribute, not three rules about three steps.

    A MENTION keeps HIGH whatever decorates it. The tier of a `REFERENCES`
    edge answers "does this name mean that definition", which a wrapper does
    not touch (`self.route` names `route`, and `functools.wraps` even keeps
    the name); "and is it then invoked" is what the edge kind says. Only a
    call claims that a frame opens on the body, so only a call can lose that
    claim -- see the value-reference pass in `resolve_refs`.
    """
    if ref.ref_kind == "call" and wraps_the_definition(node_id, ctx):
        return MEDIUM
    return HIGH


def methods_in_mro(class_id: str, ctx: ResolveContext) -> frozenset[str]:
    """Every method name the class or one of its bases defines."""
    key = ("members", class_id)
    if key not in ctx.receiver_cache:
        ctx.receiver_cache[key] = frozenset().union(
            *(
                ctx.class_members.get(owner, frozenset())
                for owner in breadth_first(class_id, ctx.bases)
            )
        )
    return ctx.receiver_cache[key]  # type: ignore[return-value]


def protocol_requirements(protocol_id: str, ctx: ResolveContext) -> frozenset[str]:
    """The method names a class must define to satisfy this Protocol: its own
    and those of its Protocol bases.

    A non-Protocol base is deliberately not counted. `class Reader(Protocol,
    Sized)` declares what a Reader must have; what `Sized` happens to provide
    is inherited by the stub, not required of an implementation.
    """
    key = ("requires", protocol_id)
    if key not in ctx.receiver_cache:
        ctx.receiver_cache[key] = frozenset().union(
            *(
                ctx.class_members.get(owner, frozenset())
                for owner in breadth_first(protocol_id, ctx.bases)
                if is_protocol(owner, ctx)
            )
        )
    return ctx.receiver_cache[key]  # type: ignore[return-value]


def protocol_implementations(ctx: ResolveContext) -> list[tuple[str, str]]:
    """`(implementer, protocol)` for every class that structurally satisfies a
    `typing.Protocol` in this revision without saying so anywhere.

    This is the relationship #50 already computes and then discards. Its
    receiver step matches a class against a Protocol's declared method set to
    decide what `self.source.read()` can reach, per call site and per method;
    the same test asked once per class is the edge itself, and it is the only
    thing that ties `TreeSource` to `GitTreeSource` at all. A Protocol is
    never instantiated, never subclassed and never called by name, so before
    this it was an island of one in every repository that defines one -- in a
    bucket a reader is told is where the dead code is.

    Deliberately excluded:

    - A Protocol with no declared methods. Every class in the repository
      satisfies it, and an edge that cannot fail to hold says nothing.
    - A Protocol as an implementer of another. A stub that happens to declare
      the right methods is still a stub; what runs is the class behind it.
    - A class that already lists the Protocol among its bases. PEP 544 allows
      that, and it is the stronger, nominal statement -- it already has an
      INHERITS edge, and a second edge for one declaration would count the
      class twice in every fan-in.

    Structural satisfaction is an inference, not a reading of the text: a
    one-method Protocol can be satisfied by coincidence, and a class can
    satisfy one it has never heard of. So the edge is MEDIUM (`IMPLEMENTS`
    below), the tier this resolver gives a candidate that really runs
    depending on the instance -- and the same tier #50 gives the very same
    classes when it resolves a call through the Protocol.

    The walk is per Protocol over the revision's classes, with both the method
    set and the Protocol test memoized in `receiver_cache`, so it costs one
    frozenset comparison per (Protocol, class) pair. Repositories have very
    few Protocols -- three in this one and two in psf/requests, for 5 and 2
    edges -- and none at all is the common case (django), which returns
    before looking at a single class.
    """
    protocols = [class_id for class_id in sorted(ctx.class_ids) if is_protocol(class_id, ctx)]
    found: list[tuple[str, str]] = []
    for protocol_id in protocols:
        required = protocol_requirements(protocol_id, ctx)
        if not required:
            continue
        nominal = set(breadth_first(protocol_id, ctx.subclasses))
        for class_id in sorted(ctx.class_ids):
            if class_id in nominal or is_protocol(class_id, ctx):
                continue
            if required <= methods_in_mro(class_id, ctx):
                found.append((class_id, protocol_id))
    return found


class Resolver(Protocol):
    def resolve_call(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """Return `(node_id, confidence)` candidates for `ref`; [] if unresolved.

        Asked of every reference kind the parser records -- a call, a base
        class, and a name used as a value -- since all three are "what could
        this name mean here". An implementation that wants to answer them
        differently reads `ref.ref_kind`, as `AstResolver` does in
        `_by_last_segment` and `_through_receiver`.
        """
        ...


class AstResolver:
    """Scope-aware heuristics over the revision's symbol table.

    First match wins, in the order the spec fixes: imported name, module-local
    name, `self.X` through the class and its bases, a method on a receiver whose
    type is written down, then a repo-wide match on the final dotted segment.
    """

    def resolve_call(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        for step in (
            self._imported,
            self._module_local,
            self._through_super,
            self._through_self,
            self._through_receiver,
            self._by_last_segment,
        ):
            hits = step(ref, ctx)
            if hits:
                return hits
        return []

    # -- step 1: an imported name ---------------------------------------
    def _imported(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        head, _, rest = ref.raw_name.partition(".")
        target = ctx.import_map.get(head)
        if target is None:
            return []
        dotted = f"{target}.{rest}" if rest else target
        node_id = self._lookup_dotted(dotted, ctx)
        return [(node_id, HIGH)] if node_id else []

    @classmethod
    def _lookup_dotted(cls, dotted: str, ctx: ResolveContext) -> str | None:
        """A dotted name -> the definition it names, following re-exports.

        A module attribute can be a name the module merely *imported*, and for
        a package `__init__.py` that is the normal case: `flask.Flask` is not
        defined in `src/flask/__init__.py`, it is bound there by
        `from .app import Flask`. Looking only for a definition (step one
        below) answered 218 of flask's 1039 ambiguous references with nothing
        -- the package's entire public API, the most-written form of every
        import a library user makes. See #38.

        So each round does two things, in this order:

        1. is the remaining qualname DEFINED in the module the prefix names?
        2. if not, is it IMPORTED there? Rewrite the dotted name through that
           import and go round again.

        Definition first is what keeps a re-export from shadowing a real local
        definition: an `__init__.py` that both defines `Foo` and imports a
        different `Foo` resolves to the one a reader looking it up in that file
        would find, which is the same rule `constructor_target` follows for a
        method. (Python itself would give the later binding, but `nodes` holds
        definitions, not assignment order, and preferring the definition is the
        conservative half of that disagreement -- it never invents a target in
        another file.)

        The claim stays HIGH, because it is the same evidence step one already
        claims HIGH for: `from .app import Flask` is an exact recorded fact
        about the source text, not an inference over it. Nothing is guessed --
        no MRO approximation, no instance type, no repo-wide name search -- and
        a chain of hops is a conjunction of such facts, so depth does not
        weaken it: three exact facts are not less certain than one. What could
        still be wrong is that the module rebinds the name at runtime, and that
        is exactly as true of the single-hop imported-name step which has been
        HIGH since the start; this adds no new kind of doubt, it reads the same
        kind of statement in a different file. The hop bound therefore never
        produces a weaker answer: exhausting it produces NO answer, and the
        reference falls through to the weak bare-name path it takes today.
        """
        seen = {dotted}
        for _ in range(REEXPORT_HOPS + 1):
            node_id = cls._lookup_defined(dotted, ctx)
            if node_id:
                return node_id
            followed = cls._follow_reexport(dotted, ctx)
            # `followed in seen` is the cycle guard: a re-export cycle
            # (`a/__init__.py` imports X from `a.b`, `a/b.py` imports X from
            # `a`) necessarily reproduces a dotted name already tried, so this
            # terminates it before the hop bound does.
            if followed is None or followed in seen:
                return None
            seen.add(followed)
            dotted = followed
        return None

    @staticmethod
    def _lookup_defined(dotted: str, ctx: ResolveContext) -> str | None:
        """Split `a.b.c` at every module/qualname boundary, longest module first."""
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            path = ctx.module_to_path.get(".".join(parts[:split]))
            if path is None:
                continue
            node_id = ctx.qualname_index.get((path, ".".join(parts[split:])))
            if node_id:
                return node_id
        return None

    @staticmethod
    def _follow_reexport(dotted: str, ctx: ResolveContext) -> str | None:
        """`flask.Flask.run` -> `flask.app.Flask.run`, via one import statement.

        Same longest-module-prefix split as `_lookup_defined`, but the first
        segment after the prefix is looked up in that module's *import* map
        instead of its definitions, and any further segments ride along
        untouched (`Flask.run` is `run` on whatever `Flask` turns out to be).

        `from .x import *` is deliberately NOT followed. `build_import_maps`
        records it under the literal name `*`, which no attribute access can
        ever spell, so a star import simply contributes nothing here. Expanding
        it would mean deciding which of the starred module's names are public,
        and that is governed by `__all__` -- which this codebase does not
        record and which real packages build at runtime
        (`__all__ = [...] + other.__all__`). Guessing "every name not starting
        with an underscore" would claim HIGH for names that may not be exported
        at all, so a name reachable only through a star import keeps falling
        through to the weak path, exactly as it does today.

        `__all__` is otherwise irrelevant to this lookup, which is worth saying
        because it looks like it should matter: `__all__` gates `from pkg
        import *` and nothing else. `flask.Flask` reads a module attribute, and
        the attribute is there because the import bound it, whether or not
        `__all__` mentions it.
        """
        parts = dotted.split(".")
        for split in range(len(parts) - 1, 0, -1):
            path = ctx.module_to_path.get(".".join(parts[:split]))
            if path is None:
                continue
            name, *tail = parts[split:]
            target = ctx.import_maps.get(path, {}).get(name)
            if target is None:
                continue
            return ".".join([target, *tail])
        return None

    # -- step 2: a name defined in the same module ------------------------
    def _module_local(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        if ref.dotted:
            return []
        node_id = ctx.qualname_index.get((ctx.path, ref.raw_name))
        return [(node_id, HIGH)] if node_id else []

    # -- step 2b: super().X through the enclosing class's bases -------------
    def _through_super(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """`super().X()` -- the enclosing class's inherited `X`, at HIGH.

        This is one of the most certain calls Python has: the starting class is
        the one the call is written in, and the lookup skips it. It was LOW
        because `parse.py` used to flatten `super()` to the unknown-receiver
        marker, so it fell to the repo-wide name match -- 26 candidates per site
        on psf/requests, all LOW, exactly one right.

        Unlike `_through_self`, subclass overrides are NOT candidates.
        `super()` walks strictly upwards; a subclass override is what it exists
        to bypass.

        The base walk is the same breadth-first approximation of the MRO used
        everywhere else in this module, and starts at the bases rather than the
        class itself -- `super().__init__()` inside `Child.__init__` must not
        resolve to `Child.__init__`.

        A decorated declaration is MEDIUM here as it is everywhere a method is
        reached through a class (`tier_of_declaration`). `super().X()` is an
        attribute lookup like `self.X()`, one starting class further up, so a
        wrapper sits between the call and the body in exactly the same way.
        Neither benchmark repository exercises it -- flask and requests have
        no decorated target among this step's HIGH claims at all -- so this is
        the same fact applied where it holds, not a second measurement; see
        `_through_self`, which is where the measurement is.
        """
        head, _, attribute = ref.raw_name.partition(".")
        if head != SUPER or not attribute or "." in attribute:
            return []
        owner = ctx.qualname_index.get((ctx.path, ref.from_qualname))
        start = ctx.enclosing_class.get(owner) if owner else None
        if start is None:
            return []
        for class_id in breadth_first(start, ctx.bases)[1:]:
            found = self._method_on(class_id, attribute, ctx)
            if found:
                return [(found, tier_of_declaration(found, ref, ctx))]
        return []

    # -- step 3: self.X through the class, its bases, and its overrides ----
    def _through_self(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """`self.X` resolves to the method the enclosing class inherits, PLUS
        every override of it in a subclass.

        Walking only up the MRO and stopping at the first hit is wrong, and
        wrong in the direction that hurts most. `self` is an instance of the
        enclosing class *or of any subclass of it*, so an override is a real
        runtime candidate, and dropping it is the over-approximation bias
        pointing backwards.

        The damage is worst when the base declaration is an abstract stub. In
        `requests`, `SessionRedirectMixin.send` has a `...` body and
        `Session(SessionRedirectMixin)` supplies the real one; first-match-wins
        bound `self.send()` inside `resolve_redirects` to the stub at HIGH
        confidence, so `impact Session.send` -- the single most important edge
        in the library, the one that drives every redirect hop -- reported
        nothing. See issue #14.

        The inherited match keeps HIGH: it is the declaration this class
        actually resolves to by name. Overrides are MEDIUM, because which one
        runs depends on the instance, and that is genuinely less certain than
        a name lookup -- not LOW, which is the tier for a repo-wide guess with
        no hierarchy behind it.

        The decorated declaration (#64). The MRO walk establishes which
        declaration the name reaches. It does not establish that the class
        attribute of that name still holds the function written under `def`:
        `@setupmethod` returns a wrapper, so `self.add_url_rule(...)` opens
        `setupmethod.<locals>.wrapper_func` and enters the decorated body only
        if that wrapper chooses to call it -- which is a question about code
        this resolver has not read. The declaration remains the right answer
        to "where does an edit land", so nothing is dropped; what it cannot be
        is HIGH, which is read as "this call site runs that definition". The
        test is `wraps_the_definition`, written for `_through_receiver` by #54
        and applied here through `tier_of_declaration`.

        Whether the drop should be gentler here is the question this step
        deserved separately, because this is the strongest rung of the ladder:
        the class is the one the call is written in, not one inferred from an
        annotation that Python does not enforce. The measurement answers it.
        Split by whether the target carried a decorator, this step's HIGH
        claims on flask were 86 right and 3 wrong undecorated, against 0 right
        and 27 wrong decorated -- all 27 `@setupmethod` -- which is the same
        shape the receiver step showed at 0 and 148, not a better one. Knowing
        the class exactly is evidence about which declaration; it is no
        evidence at all about what the attribute holds, and the second half is
        the one HIGH's meaning rests on. So the tier is the same MEDIUM as
        `_through_receiver`'s, and for a reason rather than by analogy: MEDIUM
        is what this resolver already means by a candidate that is really
        there and whose execution depends on something it cannot see -- the
        overrides above -- and it keeps the candidate in `impact`'s default
        report, where LOW, the tier of a repo-wide guess, would be sampled
        away.

        Where else the question applies, since the mechanism belongs to the
        attribute rather than to `self`:

        - `_through_super` does the same lookup from one class further up and
          is weakened with it. No number moved: neither benchmark repository
          has a decorated target among that step's HIGH claims.
        - `_imported` and `_module_local` resolve a name in a MODULE
          namespace, where a decorator rebinds just as freely -- and there the
          measurement points the other way. flask and requests give those two
          steps 13 HIGH claims on decorated targets, every one of them
          `functools.cache` or `contextlib.contextmanager`; of the six whose
          endpoints both ran, five were observed and the sixth is a
          self-recursive edge the tracer drops by construction. Wrappers that
          do delegate are the norm there, so the rule would cost right answers
          and buy nothing. Moving them needs its own measurement, not this
          one's symmetry.
        - MEDIUM and LOW candidates are untouched wherever they occur. Neither
          claims that a frame opens on the body, so neither has that claim to
          lose, and demoting them further would be deciding a wrapped method
          away -- the narrower, more confident, more wrong answer this step
          exists to avoid.
        """
        head, _, attribute = ref.raw_name.partition(".")
        if head != "self" or not attribute or "." in attribute:
            return []
        owner = ctx.qualname_index.get((ctx.path, ref.from_qualname))
        start = ctx.enclosing_class.get(owner) if owner else None
        if start is None:
            return []

        hits: list[tuple[str, str]] = []
        for class_id in self._mro(start, ctx):
            node_id = self._method_on(class_id, attribute, ctx)
            if node_id:
                # The declaration the name reaches -- HIGH, unless a decorator
                # stands between the name and the body, which is precisely the
                # part HIGH would be promising. See the paragraph above.
                hits.append((node_id, tier_of_declaration(node_id, ref, ctx)))
                break
        if not hits:
            return []

        seen = {hits[0][0]}
        for class_id in self._descendants(start, ctx):
            node_id = self._method_on(class_id, attribute, ctx)
            if node_id and node_id not in seen:
                seen.add(node_id)
                hits.append((node_id, MEDIUM))
        return hits

    @staticmethod
    def _method_on(class_id: str, attribute: str, ctx: ResolveContext) -> str | None:
        class_path, _, class_qualname = class_id.partition("::")
        return ctx.qualname_index.get((class_path, f"{class_qualname}.{attribute}"))

    @staticmethod
    def _mro(start: str, ctx: ResolveContext) -> list[str]:
        """The class and its known bases, nearest first."""
        return breadth_first(start, ctx.bases)

    @staticmethod
    def _descendants(start: str, ctx: ResolveContext) -> list[str]:
        """Every known subclass of `start`, transitively (excluding `start`)."""
        cached = ctx.descendant_cache.get(start)
        if cached is None:
            cached = breadth_first(start, ctx.subclasses)[1:]
            ctx.descendant_cache[start] = cached
        return cached

    # -- step 3b: x.m() through what `x` was declared or constructed as ----
    def _through_receiver(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """`catalog.fingerprint()` where `catalog: Catalog`, `item.save()` where
        `item = Item()`, and `self.source.tree()` where `__init__` did
        `self.source = source` from `source: TreeSource`.

        Every step above resolves a NAME. Nothing resolved a receiver, so a call
        on a variable reached the bare-name match with only its last segment,
        even when the variable's type was written a few lines up. On this
        repository that was half of the ambiguous references. See #47.

        The receiver's type is read from `blob_bindings`, never inferred:

        - Annotations -- parameters, `x: T`, a class-body `x: T`, and a
          `self.x` copied from an annotated parameter. They cover what crosses
          a function boundary.
        - Constructor tracking -- `item = Item()`, the same for `self.x`. It
          covers what is born inside one.

        The type name is looked up with the machinery that already answers a
        call: the file's import map and `_lookup_dotted` (re-exports and all),
        then a class defined in an enclosing function, then one defined in the
        module. The method is then found on it the way `_through_self` finds
        one.

        The step declines -- the reference falls through to exactly the answer
        it had before -- unless EVERY binding of the receiver is known: one
        opaque binding (a loop variable, an unannotated parameter), a callee
        that is a function rather than a class, a type outside the repository,
        or a class on which the method cannot be found, and nothing is claimed.
        A partial answer here would be narrower than the fan-out and not more
        right.

        Confidence, per mechanism:

        - An annotation on a concrete class, bound once: the declaration its
          MRO resolves to is HIGH, and every subclass override is MEDIUM. This
          is `_through_self`'s argument unchanged. `catalog: Catalog` is a
          recorded fact about the text, as `from x import Catalog` is, and the
          value may be a subclass instance exactly as `self` may be -- so the
          overrides are real candidates (#14), less certain than the
          declaration and far more grounded than a repo-wide guess. Python does
          not enforce the annotation; neither does it enforce that an imported
          name is not rebound, and that has been HIGH since the start.
        - A construction, bound once: HIGH, and NO subclass overrides.
          `Item()` names the exact class, for the reason `constructor_target`
          gives -- the instance is an `Item`, not a subclass of one.
        - A Protocol: see below.
        - Any of these with more than one binding (the name is reassigned, or
          annotated with a union of classes): every candidate is capped at
          MEDIUM. Which binding reaches the call depends on control flow this
          does not track, and each is still a real, declared candidate --
          neither a certainty nor a guess. `x: T | None` counts as one binding:
          `None` is not a class, and the parser drops it.
        - Any of these where the declaration the MRO reaches is DECORATED by
          anything the language does not define (`wraps_the_definition`):
          MEDIUM, whatever the receiver's evidence was. See below.

        The decorated declaration. Resolving the receiver establishes which
        class the method is looked up on. It does not establish that the class
        attribute of that name still holds the function written under `def`:
        a decorator returns whatever it likes, and `app.route(...)` then calls
        that instead. The candidate is not wrong -- `route`'s body is where a
        reader goes and where an edit lands, so the edge belongs in the graph
        -- but HIGH is read as "this call site runs that definition", and that
        is the half a decorator can take away.

        This is measurement, not caution (#54). #50 moved flask's conditional
        precision 0.93 -> 0.74; instrumenting the resolver over the same trace
        attributed 148 of the 149 new wrong HIGH claims to one shape --
        `@setupmethod`-wrapped Flask methods (`Scaffold.route`,
        `Blueprint.register_blueprint`) reached through `app = Flask(__name__)`
        or an annotated `Blueprint`. At runtime the frame that opens is
        `setupmethod.<locals>.wrapper_func`, never the decorated body, so
        every such claim was contradicted by the trace. Split by whether the
        target was decorated, this step's HIGH claims there were 60 right and
        1 wrong undecorated, against 0 right and 148 wrong decorated. Nothing
        else separated them, and no Protocol was involved in any of it: flask
        defines none, so the structural-implementer branch below never runs on
        the repository whose number fell.

        What this does NOT do is drop anything. The candidate stays, one tier
        down, where `impact` still shows it -- the failure mode being avoided
        throughout this step is a narrower, more confident, more wrong answer,
        and silently deciding a wrapped method away would be exactly that.

        The Protocol question. `typing.Protocol` is satisfied structurally, so
        `GitTreeSource` implements `TreeSource` without subclassing it and the
        hierarchy holds no link between them. Resolving `source: TreeSource` to
        `TreeSource.tree` alone would bind the call to a stub at HIGH and hide
        the code that runs -- #14's failure again, narrower and more confident
        than the LOW fan-out it replaced, which at least contained the right
        answer. Of the three options #47 lays out, this takes the first AND
        the second:

        - the Protocol's declaration is HIGH -- it is what the annotation names
          and the contract every caller is written against, so editing its
          signature does affect this call;
        - every structural implementer -- a non-Protocol class whose MRO
          defines every method the Protocol (and its Protocol bases) declares,
          or a class with a subclass that does -- is MEDIUM, the tier
          `_through_self` gives a candidate that runs depending on the instance;
        - and every candidate the bare-name fan-out would have produced that is
          not already among them stays, at LOW.

        The last clause is the floor, and it holds by construction: no
        Protocol-typed receiver can lose a candidate today's fan-out contains.
        Structural matching alone cannot promise that. An implementer can
        inherit a method from a class outside the repository, or satisfy a
        member with an attribute assigned in `__init__`, and neither is a node
        whose method set this can read; dropping those classes would trade
        recall for precision, which this resolver never does. What the step
        adds for a Protocol is ranking, not removal: the implementers a reader
        would name rise above the unrelated `read` methods, and the result is
        materialized as edges rather than deferred as `ambiguous`.

        Skipping Protocol receivers entirely (option 3) was the other safe
        choice, and it was rejected because five of the six annotation cases on
        this repository are Protocols -- it would have left the mechanism
        idle where the evidence says it matters.

        ABCs are nominal: an implementation must subclass one (or be
        `register()`ed, which is not tracked), so an ABC annotation takes the
        concrete-class path, where subclass overrides are already candidates.
        """
        if ref.ref_kind != "call":
            # A base class is named, never called on an instance -- and a
            # value reference names no receiver either: `self.source.read`
            # read as a value is a mention of a method, which `_through_self`
            # answers when it can, not an invocation through a typed variable.
            # The distinction matters because this step is the one that
            # materializes a Protocol's LOW fan-out (below), and that is
            # exactly what a mention must not carry.
            return []
        head, _, rest = ref.raw_name.partition(".")
        if head == "self":
            attribute, _, method = rest.partition(".")
            if not method or "." in method:
                return []
            bindings = self._attribute_bindings(attribute, ref, ctx)
        elif not rest or "." in rest or head.startswith("<"):
            # `<attr>.x`, `<super>.x` and `<dynamic>` have no receiver name.
            return []
        else:
            method = rest
            bindings = local_bindings(head, ref.from_qualname, ctx)
        if not bindings:
            return []

        types: list[tuple[str, bool]] = []
        for binding in bindings:
            if binding.kind == OPAQUE:
                return []
            class_id = self._resolve_type(binding, ctx)
            if class_id is None:
                return []
            types.append((class_id, binding.kind == CALL))

        hits: dict[str, str] = {}

        def add(node_id: str, confidence: str) -> None:
            previous = hits.get(node_id)
            hits[node_id] = confidence if previous is None else stronger(previous, confidence)

        structural = False
        for class_id, constructed in types:
            declared = self._inherited(class_id, method, ctx)
            if declared is None:
                return []
            # HIGH says the call site runs that definition. A decorated
            # definition is not what its own name holds, so that is exactly
            # the part this cannot promise; see `tier_of_declaration`, which
            # is the same rule `_through_self` and `_through_super` apply, and
            # the paragraph above.
            add(declared, tier_of_declaration(declared, ref, ctx))
            if is_protocol(class_id, ctx):
                structural = True
                for node_id in self._implementers(class_id, method, ctx):
                    add(node_id, MEDIUM)
            elif not constructed:
                for subclass in self._descendants(class_id, ctx):
                    node_id = self._method_on(subclass, method, ctx)
                    if node_id:
                        add(node_id, MEDIUM)

        if len(bindings) > 1:
            hits = {node_id: weaker(conf, MEDIUM) for node_id, conf in hits.items()}
        if structural:
            for node_id in ctx.name_index.get(method, ()):
                hits.setdefault(node_id, LOW)
        return list(hits.items())

    def _attribute_bindings(
        self, attribute: str, ref: ParsedRef, ctx: ResolveContext
    ) -> list[Binding]:
        """Every binding of `self.<attribute>` the enclosing class can see.

        That is the class, its bases, AND its subclasses: `self` may be a
        subclass instance, and a subclass that assigns the attribute something
        else changes what `self.x` holds inside an inherited method. Collecting
        across the whole hierarchy means one opaque assignment anywhere in it
        makes the step decline, which is the conservative direction.
        """
        owner = ctx.qualname_index.get((ctx.path, ref.from_qualname))
        start = ctx.enclosing_class.get(owner) if owner else None
        if start is None:
            return []
        found: list[Binding] = []
        for class_id in [*self._mro(start, ctx), *self._descendants(start, ctx)]:
            path, _, qualname = class_id.partition("::")
            found.extend(ctx.bindings.get((path, qualname, f"self.{attribute}"), ()))
        return found

    def _resolve_type(self, binding: Binding, ctx: ResolveContext) -> str | None:
        """The live class a binding's type name refers to, or None.

        Resolved where the binding was written, which for an inherited
        attribute is another file: its import map, then a class defined in the
        binding's function or an enclosing one (`def test(): class Local`), then
        one defined at the top of that module.
        """
        dotted = binding.type or ""
        head, _, rest = dotted.partition(".")
        target = ctx.import_maps.get(binding.path, {}).get(head)
        if target is not None:
            node_id = self._lookup_dotted(f"{target}.{rest}" if rest else target, ctx)
        else:
            node_id = None
            for scope in [binding.scope, *enclosing_function_scopes(binding.scope)]:
                node_id = ctx.qualname_index.get((binding.path, f"{scope}.<locals>.{dotted}"))
                if node_id:
                    break
            node_id = node_id or ctx.qualname_index.get((binding.path, dotted))
        return node_id if node_id in ctx.class_ids else None

    def _inherited(self, class_id: str, method: str, ctx: ResolveContext) -> str | None:
        """The declaration of `method` that `class_id`'s MRO walk reaches first."""
        for owner in self._mro(class_id, ctx):
            node_id = self._method_on(owner, method, ctx)
            if node_id:
                return node_id
        return None

    def _implementers(self, protocol_id: str, method: str, ctx: ResolveContext) -> list[str]:
        """Every definition of `method` on a class that structurally satisfies
        the Protocol: a non-Protocol class whose MRO defines every method the
        Protocol and its Protocol bases declare -- or a class with a subclass
        that does, since that subclass runs the inherited method."""
        key = ("implementers", protocol_id, method)
        if key not in ctx.receiver_cache:
            required = protocol_requirements(protocol_id, ctx)
            found: list[str] = []
            for node_id in ctx.name_index.get(method, ()):
                path, _, qualname = node_id.partition("::")
                owner = ctx.qualname_index.get((path, qualname.rpartition(".")[0]))
                if owner is None or owner not in ctx.class_ids or is_protocol(owner, ctx):
                    continue
                if any(
                    required <= methods_in_mro(candidate, ctx)
                    for candidate in [owner, *self._descendants(owner, ctx)]
                ):
                    found.append(node_id)
            ctx.receiver_cache[key] = found
        return ctx.receiver_cache[key]  # type: ignore[return-value]

    # -- steps 4 and 5: a repo-wide match on the last segment -------------
    def _by_last_segment(self, ref: ParsedRef, ctx: ResolveContext) -> list[tuple[str, str]]:
        """The last resort, and the only step a value reference never reaches.

        For a call, matching the final segment against every definition in the
        repository is weak but grounded: something IS being called at that
        line, and the right answer is somewhere in the set -- which is why the
        fan-out is recorded rather than dropped (`is_derivable_fanout`) and
        expanded on demand.

        A mention carries no such guarantee. `self.handler` is far more often
        an attribute holding an object than a reference to a function named
        `handler`, and there is no call at that line for the set to be the
        answer to. Letting it through would put the report that exists to say
        which regions are genuinely apart at the mercy of every attribute name
        that collides with a function name -- and it is the step, not the new
        reference kind, that would be doing the damage. So a value reference
        is recorded only where the resolver could NAME the definition it
        means, which also makes `REFERENCES` the one edge kind that is never
        LOW.
        """
        if ref.ref_kind == VALUE_REF:
            return []
        if is_builtin_call(ref) or is_external_call(ref, ctx):
            return []
        candidates = ctx.name_index.get(ref.raw_name.rpartition(".")[2], ())
        if len(candidates) == 1:
            return [(candidates[0], MEDIUM)]
        return [(node_id, LOW) for node_id in candidates]


@dataclass(frozen=True)
class ResolveStats:
    edges: int = 0
    unresolved: int = 0
    ambiguous: int = 0


def build_import_maps(
    connection: sqlite3.Connection, rev: str, config: Config
) -> tuple[dict[str, dict[str, str]], dict[str, set[str]]]:
    """Per-path local-alias -> absolute-dotted-module map, and the set of
    modules each path imports (feeds `dependents()`).

    The one place a raw `import`/`from ... import` row -- with its
    relative-import `level` -- gets expanded into an absolute dotted module
    name. Both the resolver (`_SymbolTable`, below) and effect detection's
    catalog expansion (`effects/detect.py`) build on this rather than each
    repeating the expansion, so the two cannot silently drift apart.
    """
    paths = [
        row["path"]
        for row in connection.execute("SELECT DISTINCT path FROM tree WHERE rev=?", (rev,))
    ]
    module_for = {path: module_for_path(path, config.source_roots) for path in paths}
    alias_maps: dict[str, dict[str, str]] = {path: {} for path in paths}
    imported_modules: dict[str, set[str]] = {path: set() for path in paths}

    rows = connection.execute(
        "SELECT t.path, i.module, i.level, i.name, i.alias FROM blob_imports i"
        " JOIN tree t ON t.blob_sha = i.blob_sha WHERE t.rev=? ORDER BY t.path, i.ordinal",
        (rev,),
    )
    for row in rows:
        path = row["path"]
        alias_map = alias_maps[path]
        modules = imported_modules[path]
        package = package_for_module(module_for[path], path)
        module = absolute_module(row["module"], row["level"], package)
        if row["name"] is None:
            # `import a.b` / `import a.b as c`: the alias names the module,
            # and a plain import also makes the full dotted path usable.
            alias_map[row["alias"] or module] = module
            if row["alias"] is None:
                alias_map.setdefault(module.partition(".")[0], module.partition(".")[0])
        else:
            target = f"{module}.{row['name']}" if module else row["name"]
            alias_map[row["alias"] or row["name"]] = target
            # `from a.b import c` may name a module or a symbol; record both.
            modules.add(target)
        if module:
            modules.add(module)
    return alias_maps, imported_modules


class _SymbolTable:
    """The revision's live symbol table, plus the per-file import maps."""

    def __init__(
        self, store: Store, rev: str, config: Config, only_paths: set[str] | None = None
    ) -> None:
        connection = store.connection
        self.paths: list[str] = sorted(
            row["path"] for row in connection.execute("SELECT path FROM tree WHERE rev=?", (rev,))
        )
        self.module_for: dict[str, str] = {
            path: module_for_path(path, config.source_roots) for path in self.paths
        }
        self.module_to_path: dict[str, str] = {}
        for path in self.paths:
            # Sorted paths, so a module reachable from two source roots
            # deterministically binds to the first one.
            self.module_to_path.setdefault(self.module_for[path], path)
        self.repo_segments: frozenset[str] = frozenset(
            segment for module in self.module_to_path for segment in module.split(".")
        )

        self.qualname_index: dict[tuple[str, str], str] = {}
        self.name_index: dict[str, list[str]] = {}
        # Every node sharing a (path, qualname) — live and shadowed alike —
        # keyed by their line span, so a ref originating inside a shadowed
        # definition can be attributed to it rather than to the live one.
        self.owner_index: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
        class_ids: dict[tuple[str, str], str] = {}
        all_rows = connection.execute(
            "SELECT id, path, qualname, kind, line_start, line_end, name_binding, decorators"
            " FROM nodes WHERE rev=? ORDER BY id",
            (rev,),
        ).fetchall()
        for row in all_rows:
            key = (row["path"], row["qualname"])
            self.owner_index.setdefault(key, []).append(
                (row["id"], row["line_start"], row["line_end"])
            )

        live_rows = [row for row in all_rows if row["name_binding"] == "live"]
        for row in live_rows:
            key = (row["path"], row["qualname"])
            self.qualname_index[key] = row["id"]
            self.name_index.setdefault(row["qualname"].rpartition(".")[2], []).append(row["id"])
            if row["kind"] == "class":
                class_ids[key] = row["id"]

        #: Live class nodes, by id -- the test `_constructor_target` applies
        #: to a resolved call before treating it as an instantiation.
        self.class_node_ids: frozenset[str] = frozenset(class_ids.values())

        self.enclosing_class: dict[str, str] = {}
        for row in live_rows:
            found = _nearest_class(row["path"], row["qualname"], class_ids)
            if found:
                self.enclosing_class[row["id"]] = found

        self.import_maps, self.imported_modules = build_import_maps(connection, rev, config)

        members: dict[str, set[str]] = {}
        for row in live_rows:
            if row["kind"] != "method":
                continue
            owner_qualname, _, name = row["qualname"].rpartition(".")
            owner = class_ids.get((row["path"], owner_qualname))
            if owner:
                members.setdefault(owner, set()).add(name)
        self.class_members: dict[str, frozenset[str]] = {
            owner: frozenset(names) for owner, names in members.items()
        }

        # Decorated definitions only. Most are not, and an empty entry would
        # cost a dict the size of `nodes` to say nothing; `wraps_the_definition`
        # reads a missing key as "carries no decorator", which is what it means.
        self.decorators: dict[str, tuple[str, ...]] = {
            row["id"]: tuple(row["decorators"].split(",")) for row in live_rows if row["decorators"]
        }

        self.class_bases: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT t.path, r.from_qualname, r.raw_name FROM blob_refs r"
            " JOIN tree t ON t.blob_sha = r.blob_sha WHERE t.rev=? AND r.ref_kind='base'"
            " ORDER BY t.path, r.ordinal",
            (rev,),
        ):
            owner = class_ids.get((row["path"], row["from_qualname"]))
            if owner:
                self.class_bases.setdefault(owner, []).append(row["raw_name"])

        # A local binding is only ever read by references in its own file, so a
        # narrowed pass needs only its own paths' -- but `self.x` is read
        # across the class hierarchy, wherever a subclass or base lives.
        sql = (
            "SELECT t.path, b.scope, b.name, b.kind, b.type FROM blob_bindings b"
            " JOIN tree t ON t.blob_sha = b.blob_sha WHERE t.rev=?"
        )
        args: tuple = (rev,)
        if only_paths is not None:
            marks = ",".join("?" * len(only_paths))
            sql += f" AND (b.name LIKE 'self.%' OR t.path IN ({marks}))"
            args += tuple(sorted(only_paths))
        self.bindings: dict[tuple[str, str, str], list[Binding]] = {}
        for row in connection.execute(sql + " ORDER BY t.path, b.ordinal", args):
            self.bindings.setdefault((row["path"], row["scope"], row["name"]), []).append(
                Binding(row["path"], row["scope"], row["kind"], row["type"])
            )

    def context(
        self,
        rev: str,
        path: str,
        bases: dict[str, list[str]],
        subclasses: dict[str, list[str]] | None = None,
        descendant_cache: dict[str, list[str]] | None = None,
        receiver_cache: dict[tuple, object] | None = None,
    ) -> ResolveContext:
        return ResolveContext(
            rev=rev,
            path=path,
            module=self.module_for[path],
            module_to_path=self.module_to_path,
            qualname_index=self.qualname_index,
            name_index=self.name_index,
            import_map=self.import_maps[path],
            import_maps=self.import_maps,
            bases=bases,
            enclosing_class=self.enclosing_class,
            subclasses=subclasses if subclasses is not None else {},
            descendant_cache=descendant_cache if descendant_cache is not None else {},
            repo_segments=self.repo_segments,
            bindings=self.bindings,
            class_ids=self.class_node_ids,
            class_bases=self.class_bases,
            class_members=self.class_members,
            decorators=self.decorators,
            receiver_cache=receiver_cache if receiver_cache is not None else {},
        )


def _nearest_class(path: str, qualname: str, class_ids: dict[tuple[str, str], str]) -> str | None:
    """The innermost enclosing class of `qualname`, if any."""
    parts = qualname.split(".")
    for split in range(len(parts) - 1, 0, -1):
        found = class_ids.get((path, ".".join(parts[:split])))
        if found:
            return found
    return None


def _refs_by_path(
    store: Store, rev: str, ref_kind: str, only_paths: list[str] | None = None
) -> dict[str, list[ParsedRef]]:
    """References of one kind, grouped by owning path.

    `only_paths` restricts the scan itself, not just the loop over the result:
    a narrowed resolve that still read every reference in the repository would
    be proportional to repo size in the one place the narrowing exists to fix.
    """
    sql = (
        "SELECT t.path, r.ordinal, r.from_qualname, r.ref_kind, r.raw_name, r.dotted, r.line"
        " FROM blob_refs r JOIN tree t ON t.blob_sha = r.blob_sha"
        " WHERE t.rev=? AND r.ref_kind=?"
    )
    args: tuple = (rev, ref_kind)
    if only_paths is not None:
        sql += f" AND t.path IN ({','.join('?' * len(only_paths))})"
        args += tuple(only_paths)
    rows = store.connection.execute(sql + " ORDER BY t.path, r.ordinal", args)
    grouped: dict[str, list[ParsedRef]] = {}
    for row in rows:
        grouped.setdefault(row["path"], []).append(
            ParsedRef(
                ordinal=row["ordinal"],
                from_qualname=row["from_qualname"],
                ref_kind=row["ref_kind"],
                raw_name=row["raw_name"],
                dotted=row["dotted"],
                line=row["line"],
            )
        )
    return grouped


def _source_id(ref: ParsedRef, table: _SymbolTable, path: str) -> str:
    """The node that owns a reference; module scope gets a stable pseudo-id.

    A qualname can own more than one node when an earlier definition is
    shadowed by a later one of the same name — both still execute, so a
    call made from inside the shadowed body must be attributed to it, not
    to the live definition that happens to share its name. The ref's `line`
    (recorded verbatim as `callsite_line` on the edge) picks out which node's
    span it actually falls inside; a qualname with a single owner keeps the
    direct-lookup fast path.
    """
    if ref.from_qualname == MODULE_SCOPE:
        return f"{path}::{MODULE_SCOPE}"
    candidates = table.owner_index.get((path, ref.from_qualname))
    if not candidates:
        return f"{path}::{ref.from_qualname}"
    if len(candidates) == 1:
        return candidates[0][0]
    for node_id, line_start, line_end in candidates:
        if line_start <= ref.line <= line_end:
            return node_id
    # Should not happen (every ref sits inside the definition it came from);
    # fall back to the live binding rather than dropping the edge.
    return table.qualname_index.get((path, ref.from_qualname), candidates[-1][0])


def is_derivable_fanout(hits: list[tuple[str, str]]) -> bool:
    """Is this candidate set the bare-name fan-out, recomputable from the
    name index alone?

    True exactly when every candidate is LOW, which happens exactly when the
    last-resort step (`_by_last_segment`) matched a bare name against more
    than one live definition. Every other step returns at least one HIGH or
    MEDIUM candidate: an imported name, a module-local name, a `self.X` hit and
    its overrides, a typed receiver, and a last-segment match with a single
    answer all name something the resolver actually distinguished, and all get
    an edge. (A Protocol-typed receiver also carries the rest of the fan-out at
    LOW, and those are materialized with it: the set as a whole is not
    `name_index[name]` ranked flat, so it is not derivable. See
    `AstResolver._through_receiver`.)

    A LOW set does not. It is `name_index[name]` verbatim -- a set the `nodes`
    table already determines -- so materializing it stores nothing the graph
    did not already contain, at a cost quadratic in repository size (2.09M of
    django's 2.16M edges, 96.6%, before #6). It is recorded once in
    `unresolved` with `reason='ambiguous'` instead, and `ambiguity.py`
    reconstructs it on demand. Nothing is dropped and nothing is capped: the
    bound on how many of them a *reader* sees is `--limit`, a property of the
    question, not of the graph. See #25.
    """
    return bool(hits) and all(confidence == LOW for _, confidence in hits)


#: The method a call to a class actually runs. `Cls()` resolves to the class
#: node, and nothing in the source ever writes `Cls.__init__`, so without the
#: edge below a constructor has no incoming call at all -- on psf/requests that
#: left `src/requests/adapters.py::BaseAdapter.__init__` an island of exactly
#: one. #27 records this as a plain bug rather than a limit of static analysis:
#: the caller IS in the source, it just spells the callee's name as the class's.
CONSTRUCTOR = "__init__"


def constructor_target(
    class_id: str, table: _SymbolTable, bases: dict[str, list[str]]
) -> str | None:
    """The `__init__` that `class_id()` runs, or None if it neither defines
    nor inherits one.

    The walk is the same breadth-first approximation of the MRO that
    `AstResolver._through_self` already resolves an inherited method with,
    and is deliberately the same: the class a name is declared on is what a
    reader looking the call up would find. It is not a C3 linearization, so
    under multiple inheritance the branch reached first can differ from the
    one Python picks -- but every candidate it can return is a real
    `__init__` on a real base, and the alternative is the missing edge this
    exists to fix.

    Subclass overrides are deliberately NOT candidates, which is the one
    place this differs from `_through_self`. `self.x()` may run a subclass's
    override because `self` may be an instance of a subclass; `Cls()` names
    the exact class being instantiated, so its `__init__` is looked up on
    `Cls` and its bases and nowhere else.
    """
    for owner in breadth_first(class_id, bases):
        path, _, qualname = owner.partition("::")
        found = table.qualname_index.get((path, f"{qualname}.{CONSTRUCTOR}"))
        if found:
            return found
    return None


def with_constructors(
    hits: list[tuple[str, str]],
    table: _SymbolTable,
    bases: dict[str, list[str]],
    cache: dict[str, str | None],
) -> list[tuple[str, str]]:
    """`hits`, plus the `__init__` each class among them would run.

    Applied only to hits that are actually materialized, so a reference whose
    LOW fan-out was deferred to query time (see `is_derivable_fanout`) cannot
    be re-expanded through the back door -- the constructor edges follow
    exactly the class edges the graph really holds. In practice an all-LOW
    fan-out has no class to construct anyway: `Cls()` resolves through an
    import or a module-local name, never through the bare-name fallback.

    A constructor edge carries the confidence of the class edge implying it.
    It makes the same claim ("this call site may instantiate this class")
    and `Cls()` running `Cls.__init__` adds no uncertainty of its own, so
    weakening it would understate an edge that is certain given the class.
    """
    extra: list[tuple[str, str]] = []
    seen = {node_id for node_id, _ in hits}
    for node_id, confidence in hits:
        if node_id not in table.class_node_ids:
            continue
        if node_id not in cache:
            cache[node_id] = constructor_target(node_id, table, bases)
        target = cache[node_id]
        if target is not None and target not in seen:
            seen.add(target)
            extra.append((target, confidence))
    return hits + extra if extra else hits


def _load_bases(connection: sqlite3.Connection, rev: str) -> dict[str, list[str]]:
    """Rebuild the class hierarchy from already-materialized INHERITS edges.

    Only HIGH links feed the MRO walk, which is exactly the filter the
    inheritance pass applies when it builds this map from scratch, so reading it
    back is equivalent -- provided the base references it was built from have
    not changed. That is a precondition the caller checks before narrowing; see
    `Indexer._narrowable`.
    """
    bases: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT src, dst FROM edges WHERE rev=? AND kind=? AND confidence=?",
        (rev, INHERITS, HIGH),
    ):
        bases.setdefault(row["src"], []).append(row["dst"])
    return bases


def resolve_revision(
    store: Store,
    rev: str,
    config: Config,
    resolver: Resolver | None = None,
    only_paths: set[str] | None = None,
) -> ResolveStats:
    """Rewrite `edges`, `imports` and `unresolved` for one revision.

    `only_paths` narrows the rewrite to those paths, leaving every other path's
    rows in place. That is sound only when the revision's symbol table is
    unchanged outside them -- a definition appearing or disappearing anywhere
    changes what bare-name calls in unrelated files can match, and a changed
    base class changes `self.X` resolution in every subclass, wherever it
    lives. The caller owns that check (`Indexer._narrowable`); this function
    trusts it. Passing `None` rewrites the whole revision, which cannot leave a
    stale edge behind under any circumstances.

    The bare-name fan-out is never written to `edges`: a reference whose only
    candidates are LOW is recorded once in `unresolved` as ambiguous, carrying
    the source node, the name, and the count, and is expanded at query time
    instead. See `is_derivable_fanout` and `ambiguity.py`.
    """
    resolver = resolver or AstResolver()
    connection = store.connection
    table = _SymbolTable(store, rev, config, only_paths)

    if only_paths is None:
        target_paths = table.paths
        bases: dict[str, list[str]] = {}
        connection.execute("DELETE FROM edges WHERE rev=?", (rev,))
        connection.execute("DELETE FROM imports WHERE rev=?", (rev,))
        connection.execute("DELETE FROM unresolved WHERE rev=?", (rev,))
    else:
        target_paths = [path for path in table.paths if path in only_paths]
        # Read the hierarchy back BEFORE deleting the edges it is derived from.
        bases = _load_bases(connection, rev)
        marks = ",".join("?" * len(target_paths))
        args = (rev, *target_paths)
        connection.execute(
            f"DELETE FROM edges WHERE rev=? AND callsite_path IN ({marks})", args
        )
        connection.execute(
            f"DELETE FROM imports WHERE rev=? AND importer_path IN ({marks})", args
        )
        connection.execute(f"DELETE FROM unresolved WHERE rev=? AND path IN ({marks})", args)
    scan_paths = None if only_paths is None else target_paths

    connection.executemany(
        "INSERT INTO imports(rev, importer_path, module) VALUES(?, ?, ?)",
        [
            (rev, path, module)
            for path in target_paths
            for module in sorted(table.imported_modules[path])
        ],
    )

    edge_rows: list[tuple] = []
    unresolved_rows: list[tuple] = []
    ambiguous_rows: list[tuple] = []
    builtin_rows: list[tuple] = []
    external_rows: list[tuple] = []

    # Inheritance first: `self.X` walks the class hierarchy, so the hierarchy
    # has to exist before any call is resolved.
    base_refs = _refs_by_path(store, rev, "base", scan_paths)
    for path in target_paths:
        ctx = table.context(rev, path, bases)
        for ref in base_refs.get(path, ()):
            src = _source_id(ref, table, path)
            # A base named by a bare, repo-wide-ambiguous name fans out exactly
            # like a call does, and on a large repo it is the larger half of the
            # blowup. Deferring it cannot affect the MRO: only HIGH links feed
            # `bases`, and only an all-LOW set is deferred.
            hits = resolver.resolve_call(ref, ctx)
            if is_derivable_fanout(hits):
                ambiguous_rows.append(
                    (rev, src, path, ref.line, ref.raw_name, "base", AMBIGUOUS, len(hits))
                )
                continue
            for node_id, confidence in hits:
                edge_rows.append(
                    (rev, src, node_id, INHERITS, confidence, PROVENANCE, path, ref.line)
                )
                # Only a certain link feeds the MRO walk, which claims HIGH.
                # A weaker one still gets its edge, and a `self.X` that misses
                # the walk falls through to the repo-wide name match anyway.
                if confidence == HIGH and only_paths is None:
                    bases.setdefault(src, []).append(node_id)

    # The hierarchy is complete now, so it can be inverted once: `self.X`
    # needs to see downwards (overrides in subclasses) as well as upwards.
    subclasses: dict[str, list[str]] = {}
    for subclass, base_ids in bases.items():
        for base in base_ids:
            subclasses.setdefault(base, []).append(subclass)

    # One cache object shared by every file's context, so the descendant walk
    # runs once per class for the whole revision rather than once per reference.
    descendant_cache: dict[str, list[str]] = {}
    receiver_cache: dict[tuple, object] = {}

    # Structural Protocol implementation, which is a property of the class
    # table rather than of any reference, so it is written once here rather
    # than per file. `callsite_path`/`callsite_line` point at the implementing
    # class's own declaration: there is no call site to point at, and the
    # class is where a reader would go to check the claim.
    #
    # Under a narrowed rewrite only the edges of paths being rewritten are
    # deleted, so only those paths' implementers are re-emitted. That is
    # sound for the same reason the narrowing itself is: `Indexer._narrowable`
    # requires every dirty path to declare exactly the same symbols and bases
    # as before, and a Protocol's requirements and a class's method set are
    # read from nothing else.
    if target_paths:
        protocol_ctx = table.context(
            rev, target_paths[0], bases, subclasses, descendant_cache, receiver_cache
        )
        line_start = {
            node_id: start for owners in table.owner_index.values() for node_id, start, _ in owners
        }
        rewriting = set(target_paths)
        for implementer, protocol_id in protocol_implementations(protocol_ctx):
            implementer_path = implementer.partition("::")[0]
            if implementer_path in rewriting:
                edge_rows.append(
                    (
                        rev,
                        implementer,
                        protocol_id,
                        IMPLEMENTS,
                        MEDIUM,
                        PROVENANCE,
                        implementer_path,
                        line_start.get(implementer, 0),
                    )
                )

    # A name used as a value: `connection.row_factory = _Row`, a method listed
    # in a dispatch table, a function passed as a callback. The relationship is
    # real -- sqlite calls `_Row` for every row it hands back -- but the text
    # does not say so, and until now the graph held nothing for it at all.
    #
    # Written as its own kind rather than as a weak `CALLS`. A mention is not
    # a call site: `effects` builds a witness path out of call sites and
    # presents it as clickable evidence that the effect happens, and a chain
    # that steps through "this line mentions the name" would be a claim the
    # source does not support. `impact` and `islands` do cross it, because a
    # change to `_Row` does reach the line that hands it to sqlite. See
    # `DEPENDENCY_KINDS`.
    #
    # The confidence tier is the resolver's own, unweakened -- HIGH for an
    # exact module-local or imported name, as it would be for a call. Capping
    # it was the other option and it conflates two different questions:
    # confidence answers "does this reference mean that symbol", which is
    # exactly as certain here as for a call (it is the same name lookup on
    # the same text), while "and is it then invoked" is what the edge KIND
    # says. Encoding the second in the first would leave `impact` ranking a
    # certain dependent as a doubtful one. What does keep the tier honest is
    # that a mention never reaches the bare-name fan-out
    # (`_by_last_segment`), so a `REFERENCES` edge is never LOW and never
    # ambiguous -- no `unresolved` row is written here either, in any of its
    # flavours: that count is a health signal about the CALL graph ("this
    # many calls found no callee"), and a name read as a value that turns out
    # to be a builtin or a plain attribute is not a gap in it.
    value_refs = _refs_by_path(store, rev, VALUE_REF, scan_paths)
    for path in target_paths:
        ctx = table.context(rev, path, bases, subclasses, descendant_cache, receiver_cache)
        for ref in value_refs.get(path, ()):
            src = _source_id(ref, table, path)
            for node_id, confidence in resolver.resolve_call(ref, ctx):
                edge_rows.append(
                    (rev, src, node_id, REFERENCES, confidence, PROVENANCE, path, ref.line)
                )

    # `Cls()` -> `Cls.__init__` is the same lookup for every call site that
    # names the same class, and on django that is tens of thousands of them.
    constructor_cache: dict[str, str | None] = {}

    call_refs = _refs_by_path(store, rev, "call", scan_paths)
    for path in target_paths:
        ctx = table.context(rev, path, bases, subclasses, descendant_cache, receiver_cache)
        for ref in call_refs.get(path, ()):
            src = _source_id(ref, table, path)
            hits = resolver.resolve_call(ref, ctx)
            if is_derivable_fanout(hits):
                # Not an edge and not a gap: the answer, held in the one form
                # that does not grow with the square of the repository. See
                # `is_derivable_fanout`.
                #
                # No constructor edge is added here. `with_constructors` below
                # only ever fires on a hit the resolver actually distinguished
                # -- `Cls()` resolves through an import or a module-local name,
                # never through the bare-name fallback -- so an all-LOW fan-out
                # has no class in it to construct.
                ambiguous_rows.append(
                    (rev, src, path, ref.line, ref.raw_name, "call", AMBIGUOUS, len(hits))
                )
                continue
            for node_id, confidence in with_constructors(hits, table, bases, constructor_cache):
                edge_rows.append((rev, src, node_id, CALLS, confidence, PROVENANCE, path, ref.line))
            if is_builtin_call(ref):
                # Recorded, but not as a gap. A builtin is a reference the
                # resolver understood and deliberately did not link to a repo
                # symbol -- counting it as "unresolved" buries the real gaps
                # under a large constant. Still written, so the choice is
                # visible rather than silent.
                builtin_rows.append((rev, src, path, ref.line, ref.raw_name, "call", BUILTIN, 0))
            elif not hits and is_external_call(ref, ctx):
                # The same choice one boundary further out: not a repo symbol,
                # and known not to be one, so not a gap either. Its own reason
                # rather than 'builtin', because the two are different claims
                # -- and 'ambiguous' is exactly what it used to be mistaken
                # for. See `is_external_call`.
                external_rows.append((rev, src, path, ref.line, ref.raw_name, "call", EXTERNAL, 0))
            elif not hits:
                # Never dropped: the ref stays in `blob_refs` for effect
                # detection, and the gap is counted as a health signal.
                unresolved_rows.append((rev, src, path, ref.line, ref.raw_name, "call", UNKNOWN, 0))

    connection.executemany(
        "INSERT INTO edges(rev, src, dst, kind, confidence, provenance, callsite_path,"
        " callsite_line) VALUES(?,?,?,?,?,?,?,?)",
        edge_rows,
    )
    connection.executemany(
        "INSERT INTO unresolved(rev, src, path, line, raw_name, ref_kind, reason,"
        " candidates) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        unresolved_rows + ambiguous_rows + builtin_rows + external_rows,
    )

    # Counted over the whole revision, not over this pass: a narrowed rewrite
    # touches a handful of paths but `status` has to describe the whole graph.
    def total(sql: str, *args: str) -> int:
        return connection.execute(sql, (rev, *args)).fetchone()["n"]

    by_reason = "SELECT COUNT(*) AS n FROM unresolved WHERE rev=? AND reason=?"
    return ResolveStats(
        edges=total("SELECT COUNT(*) AS n FROM edges WHERE rev=?"),
        unresolved=total(by_reason, UNKNOWN),
        ambiguous=total(by_reason, AMBIGUOUS),
    )


def dependents(store: Store, rev: str, modules: set[str]) -> set[str]:
    """Paths whose imports name any of these modules — the re-resolve set."""
    if not modules:
        return set()
    placeholders = ",".join("?" * len(modules))
    rows = store.connection.execute(
        f"SELECT DISTINCT importer_path FROM imports WHERE rev=? AND module IN ({placeholders})",
        (rev, *modules),
    )
    return {row["importer_path"] for row in rows}


def find_symbol(store: Store, rev: str, query: str) -> list[sqlite3.Row]:
    """Fuzzy lookup: exact id, then exact qualname, then suffix match.

    All three steps compare case-insensitively (`COLLATE NOCASE` for the
    two exact steps; `LIKE`'s own ASCII case-folding, already the default,
    for the suffix step), so a query's case can never change which set of
    symbols comes back -- `resolve charge` and `resolve CHARGE` return the
    identical result. Before this, steps 1-2 compared with binary `=`
    while step 3 was already case-insensitive, so a query differing only
    in case from the real name could fall straight through the (missed)
    exact steps and land on step 3's dot-anchored suffix pattern -- which
    can never match a top-level, dot-free qualname at all -- producing a
    completely different, disjoint match set instead of the same one.
    """
    columns = "id, path, qualname, kind, line_start, line_end, name_binding"
    for clause, parameters in (
        ("id=? COLLATE NOCASE", (query,)),
        ("qualname=? COLLATE NOCASE", (query,)),
        ("qualname LIKE ? ESCAPE '\\'", (f"%.{_escape_like(query)}",)),
    ):
        rows = store.connection.execute(
            f"SELECT {columns} FROM nodes WHERE rev=? AND {clause} ORDER BY id", (rev, *parameters)
        ).fetchall()
        if rows:
            return rows
    return []


def _escape_like(value: str) -> str:
    for character in ("\\", "%", "_"):
        value = value.replace(character, f"\\{character}")
    return value
