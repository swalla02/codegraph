"""Phase 1: blob bytes -> path-independent structure.

Nothing here may reference a filesystem path: one blob can appear at many
paths, and the parse cache is keyed on content alone.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass, field

PARSER_VERSION = "2"

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
    #: Structural hash of the module's top-level statements, with every
    #: nested function/class BODY elided (see `_module_skeleton`). Empty on
    #: a parse error, since there is no tree to hash.
    module_body_hash: str = ""
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


#: Any of these appearing in a literal `open(...)` mode string makes the
#: call a write (or write-capable, for `+`): 'w'rite, 'a'ppend, e'x'clusive
#: create, or read-'+'-write.
_WRITE_MODE_CHARS = frozenset("wax+")


def _open_call_marker(node: ast.Call) -> str:
    """`open`'s effect kind (FS_READ vs FS_WRITE) depends on its `mode`
    argument, which a plain dotted-name catalog match can never see --
    this is the one place in the parser that still has the call site's
    AST, so it is the one place this can be decided. Three outcomes,
    encoded as three distinct synthetic names the built-in catalog
    (`builtin.toml`) maps separately:

    - no mode argument at all, or a literal mode with none of w/a/x/+:
      a genuine read (`open` unchanged -- the plain, and by far the most
      common, case).
    - a literal mode containing w/a/x/+: a genuine write (`open!write`).
    - a mode argument that isn't a string literal (a variable, an
      f-string, ...): honestly unknown, so it keeps the conservative
      FS_READ default (`open!ambiguous`) but at lower confidence, since
      unlike the first case the evidence doesn't actually support it.
    """
    mode_node = node.args[1] if len(node.args) >= 2 else None
    if mode_node is None:
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode_node = keyword.value
                break
    if mode_node is None:
        return "open"
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        if any(char in _WRITE_MODE_CHARS for char in mode_node.value):
            return "open!write"
        return "open"
    return "open!ambiguous"


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


class _BodyElider(ast.NodeTransformer):
    """Replace every function/class def's `body` with a single placeholder
    statement, without recursing into the original body first.

    Used to build the module-level structural hash: a def/class's own
    `body_hash` already covers everything inside it (via `_body_hash` on
    the untouched node), so folding that same content into the module hash
    too would double-count it and reintroduce the exact line-shift churn
    `body_hash` comparison exists to avoid (an edit two functions away from
    a def would ripple into every def nested inside it, transitively, all
    the way up to the module). Not recursing into the original body before
    replacing it also means a closure nested inside a top-level function
    is dropped for free -- it is already inside that function's own
    (unelided) `body_hash`.

    Everything else about the def -- its name, decorators, arguments,
    base classes, and its position among the module's other top-level
    statements -- is left intact, so renaming a def, changing its
    signature, or reordering top-level statements still changes the
    module hash.
    """

    def _elide(self, node: ast.AST) -> ast.AST:
        clone = copy.copy(node)
        clone.body = [ast.Pass()]
        return clone

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self._elide(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return self._elide(node)


def _module_body_hash(tree: ast.Module) -> str:
    """Structural hash of `tree`'s top-level statements, reusing
    `_body_hash` (the same dump-and-hash `parse.py` already uses for a
    def/class's own body) over a copy with nested def/class bodies elided."""
    skeleton = _BodyElider().visit(copy.deepcopy(tree))
    return _body_hash(skeleton)


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[ParsedNode] = []
        self.refs: list[ParsedRef] = []
        self.imports: list[ParsedImport] = []
        self._scope: list[str] = []
        # Per-instance stack of enclosing scope kinds, parallel to `_scope`
        # (plus an implicit "module" base). Used to decide whether a def is
        # a "method" (immediately inside a class) or a "function". This
        # replaces a class-level set, which would leak state across parses.
        self._kinds: list[str] = ["module"]
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
        self._kinds.append("class" if kind == "class" else "function")
        if kind in ("function", "method"):
            self._scope.append("<locals>")
        self.generic_visit(node)
        if kind in ("function", "method"):
            self._scope.pop()
        self._kinds.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self._kinds[-1] == "class" else "function"
        self._visit_def(node, kind)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_def(node, "class")

    # -- references ------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name is None:
            # The receiver isn't a flattenable Name/Attribute chain --
            # `super().go()`, `PaymentService().charge(x)`,
            # `self.items[0].run()`, `(a or b).fire()`, `d["k"].m()`. Losing
            # the resolved target is fine; losing the ref entirely is not,
            # since that would drop it from both the call graph AND the
            # `unresolved` count with no signal at all (the failure mode
            # this branch exists to close).
            #
            # Record whatever IS known: when `node.func` is itself an
            # `Attribute` (true of all five examples above -- only its
            # `.value` chain fails to flatten), that's the attribute name.
            # It is deliberately given the synthetic `<attr>.` prefix rather
            # than the bare name: `<attr>.go` still contains a "." so it
            # flows through `resolve.py`'s existing `dotted`-gated pipeline
            # exactly like any other qualified call, which routes it past
            # the HIGH-confidence steps (imported-name, module-local,
            # self-through-MRO -- none of which have any real basis for a
            # receiver we know nothing about) and into the *existing*
            # repo-wide by-last-segment step, unresolved-heuristic MEDIUM/LOW
            # match on `go` alone. No new resolution logic. `<` can never
            # appear in a real Python identifier, so this can never collide
            # with a genuine dotted call. A call with no attribute at all
            # (e.g. `handlers[i]()`) has nothing to key on and is recorded
            # under a synthetic placeholder instead, purely so the COUNT is
            # never silently lost.
            name = (
                f"<attr>.{node.func.attr}" if isinstance(node.func, ast.Attribute) else "<dynamic>"
            )
        elif name == "open":
            name = _open_call_marker(node)
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

    def visit_Global(self, node: ast.Global) -> None:
        self._record_global(node, node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._record_global(node, node.names)

    def _record_global(self, node: ast.AST, names: list[str]) -> None:
        # `global`/`nonlocal` both bind the name to an outer scope, so a
        # write to it after this statement is a mutation of shared state --
        # GLOBAL_MUTATE has no catalog pattern to match against; this is
        # the syntactic detection Task 10 hooks into.
        owner = self._current_owner.removesuffix(".<locals>")
        for name in names:
            self.refs.append(
                ParsedRef(
                    ordinal=len(self.refs),
                    from_qualname=owner,
                    ref_kind="global",
                    raw_name=name,
                    dotted=None,
                    line=node.lineno,
                )
            )

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
        module_body_hash=_module_body_hash(tree),
        error=None,
    )
