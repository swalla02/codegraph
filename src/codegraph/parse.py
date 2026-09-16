"""Phase 1: blob bytes -> path-independent structure.

Nothing here may reference a filesystem path: one blob can appear at many
paths, and the parse cache is keyed on content alone.
"""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import dataclass, field

PARSER_VERSION = "4"

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


#: `ParsedBinding.kind` values.
#:
#: - `annotation`: the name was declared with this type (a parameter
#:   annotation, `x: T = ...`, a class-body `x: T`).
#: - `call`: the name was assigned the result of calling this dotted name.
#:   Whether that is a construction is not the parser's to say -- `Item()` and
#:   `load()` look identical here, and only phase 2 knows which one names a
#:   class.
#: - `opaque`: the name was bound by something no type can be read off: a loop
#:   variable, an unpacked tuple, an unannotated parameter, `x += 1`, a
#:   subscript. It carries no type, and its presence is the point: it tells the
#:   resolver the bindings it CAN read are not the whole story.
ANNOTATION, CALL, OPAQUE = "annotation", "call", "opaque"


@dataclass(frozen=True)
class ParsedBinding:
    """One way a name in one scope was bound, as the text states it.

    `scope` uses the same spelling as `ParsedRef.from_qualname`, so a call's
    receiver is looked up under exactly the scope the call was recorded in:
    `f`, `C.m`, `f.<locals>.g`, or `<module>`. An instance attribute is
    recorded under its CLASS, as `self.x`, because every method of that class
    (and of its subclasses) reads the same attribute.
    """

    ordinal: int
    scope: str
    name: str
    kind: str
    type: str | None
    line: int


@dataclass(frozen=True)
class ParseResult:
    nodes: tuple[ParsedNode, ...] = ()
    refs: tuple[ParsedRef, ...] = ()
    imports: tuple[ParsedImport, ...] = ()
    bindings: tuple[ParsedBinding, ...] = ()
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


#: Typing constructs whose FIRST argument is the type a name actually holds:
#: `Optional[T]`, `Annotated[T, meta]`, `ClassVar[T]`, `Final[T]`. Every other
#: subscript is a generic applied to its arguments -- `Box[int]` is a `Box`,
#: `list[Item]` is a `list` -- and is recorded as the thing subscripted.
_TRANSPARENT_WRAPPERS = frozenset(
    {"Optional", "Annotated", "ClassVar", "Final", "Required", "NotRequired", "ReadOnly"}
)

#: Internal marker for `self.x = name`: "whatever `name` is bound to in this
#: scope". Resolved within the parse (`_finalize_bindings`) and never stored.
_ALIAS = "alias"


def _annotation_types(node: ast.expr) -> list[str] | None:
    """The class names an annotation says a value may be, or None when it says
    something this cannot reduce to class names (`Callable[..., T]` is not a
    `Callable` whose methods get called, a `Literal` is no class at all, and a
    string that does not parse is nothing).

    `None` members are dropped rather than recorded: no repository method can
    be called on `None`, so `Resolver | None` says exactly as much as
    `Resolver` about what a call on the name can reach. An empty list is
    therefore a real answer ("only ever None"), distinct from None ("unknown").
    """
    if isinstance(node, ast.Constant):
        if node.value is None:
            return []
        if isinstance(node.value, str):
            # A forward reference, `"Catalog"`, is the same annotation quoted.
            try:
                return _annotation_types(ast.parse(node.value.strip(), mode="eval").body)
            except (SyntaxError, ValueError):
                return None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union(_annotation_types(node.left), _annotation_types(node.right))
    if isinstance(node, ast.Subscript):
        head = _dotted_name(node.value)
        if head is None:
            return None
        last = head.rpartition(".")[2]
        args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if last == "Union":
            return _union(*(_annotation_types(arg) for arg in args))
        if last in _TRANSPARENT_WRAPPERS:
            return _annotation_types(args[0]) if args else None
        return [head]
    name = _dotted_name(node)
    return [name] if name else None


def _union(*parts: list | None) -> list | None:
    """Concatenate, unless any part is unknown -- then the whole is unknown."""
    if any(part is None for part in parts):
        return None
    return [item for part in parts for item in part]


def _value_types(node: ast.expr, target: str) -> list[tuple[str, str]] | None:
    """What assigning `node` to `target` binds it to, as `(kind, type)` pairs,
    or None when nothing can be read off the value.

    Only shapes that name their answer are read:

    - `Item()` / `models.Item()`: a call, recorded by its callee. Phase 2
      decides whether the callee is a class.
    - `x = source`: whatever `source` is bound to in the same scope. This is
      how `self.source = source` in `__init__` inherits the parameter's
      annotation.
    - `a or B()` and `a if c else B()`: each operand is a possible value, so
      the union of what each binds. An operand that is the target itself
      (`resolver = resolver or AstResolver()`) adds nothing new.
    - `None`: binds no class, and contributes nothing (see `_annotation_types`).

    Everything else -- a subscript, an arithmetic result, a chained call's
    return value -- is opaque. Return-type inference is deliberately out of
    scope (#47): `Catalog.load(...).fingerprint()` needs to know what `load`
    returns, which is a different mechanism with different evidence.
    """
    if isinstance(node, ast.Constant) and node.value is None:
        return []
    if isinstance(node, ast.Call):
        callee = _dotted_name(node.func)
        return [(CALL, callee)] if callee else None
    if isinstance(node, ast.Name | ast.Attribute):
        name = _dotted_name(node)
        if name == target:
            return []
        return [(_ALIAS, node.id)] if isinstance(node, ast.Name) else None
    if isinstance(node, ast.BoolOp):
        operands = node.values
    elif isinstance(node, ast.IfExp):
        operands = [node.body, node.orelse]
    else:
        return None
    return _union(*(_value_types(operand, target) for operand in operands))


def enclosing_function_scopes(scope: str) -> list[str]:
    """`f.<locals>.g.<locals>.h` -> [`f.<locals>.g`, `f`]: the function scopes
    a name in `scope` can close over, nearest first. Class scopes are not among
    them, exactly as in Python, where a method cannot see its class body's
    names."""
    pieces = scope.split(".<locals>.")
    return [".<locals>.".join(pieces[:end]) for end in range(len(pieces) - 1, 0, -1)]


def _finalize_bindings(raw: list[tuple]) -> tuple[ParsedBinding, ...]:
    """Replace every `x = name` alias with what `name` is bound to, and number
    the result.

    Done after the whole module is visited, not at the assignment, because a
    binding may be written after the alias in source order (`self.x = item`
    above a later `item = other()` in the same function) and the resolver is
    flow-insensitive: a name means every binding it has in its scope.

    An alias is copied only when its source is fully known. If `name` has no
    binding in the scope (it is a global, or a closure variable), an opaque
    binding, or is itself an alias, the alias site becomes opaque -- one level
    of copying keeps this a lookup rather than a fixpoint, and anything deeper
    is left unknown rather than half-answered.
    """
    direct: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    aliased: set[tuple[str, str]] = set()
    for scope, name, kind, type_, _line, _source_scope in raw:
        if kind == _ALIAS:
            aliased.add((scope, name))
        else:
            direct.setdefault((scope, name), []).append((kind, type_))

    rows: list[tuple[str, str, str, str | None, int]] = []
    for scope, name, kind, type_, line, source_scope in raw:
        if kind != _ALIAS:
            rows.append((scope, name, kind, type_, line))
            continue
        key = (source_scope, type_)
        source = direct.get(key)
        if not source or key in aliased or any(k == OPAQUE for k, _ in source):
            rows.append((scope, name, OPAQUE, None, line))
            continue
        for copied_kind, copied_type in dict.fromkeys(source):
            rows.append((scope, name, copied_kind, copied_type, line))
    return tuple(
        ParsedBinding(ordinal=index, scope=scope, name=name, kind=kind, type=type_, line=line)
        for index, (scope, name, kind, type_, line) in enumerate(rows)
    )


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


#: Synthetic receiver for a `super()` call, mirroring `<attr>`/`<dynamic>`.
#: `<` cannot appear in a Python identifier, so this can never collide with a
#: real dotted call.
SUPER = "<super>"


def _is_super_call(node: ast.expr) -> bool:
    """Is `node` a bare `super()` call?

    Only the zero-argument form. The explicit `super(Cls, self)` form names a
    starting class that may not be the enclosing one, so resolving it as if it
    were would be a guess dressed as a fact -- it keeps falling through to
    `<attr>`.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "super"
        and not node.args
        and not node.keywords
    )


_PASS = ast.Pass()


def _elide_nested(node: object) -> object:
    """`node` with every def/class body replaced by a single `Pass`, without
    mutating it and without copying anything that does not change.

    `ast.NodeTransformer.generic_visit` writes back into the node it is given,
    which is why the elider used to be handed `copy.deepcopy(...)`. That copy
    was quadratic in practice: a module was deep-copied once, and then every
    class inside it deep-copied again for its own hash. On django's largest test
    module -- 52 classes -- parsing that one file cost 4.8s, 3.8s of it inside
    `copy.deepcopy`, which put the "parse is proportional to the diff" half of
    the cost guarantee on the wrong side of a 1-file edit.

    This returns the original node unchanged when nothing inside it needed
    eliding, so only the spine down to each def/class is ever copied, one level
    deep at a time.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        clone = copy.copy(node)
        clone.body = [_PASS]
        return clone
    if not isinstance(node, ast.AST):
        return node
    return _elide_children(node)


def _elide_children(node: ast.AST) -> ast.AST:
    """`_elide_nested` applied to `node`'s children but never to `node` itself.

    This is the difference between hashing a module (whose own "body" is the
    thing being described) and hashing a def (whose own body must stay intact
    while its methods' bodies are elided).
    """
    replacements: dict[str, object] = {}
    for name, value in ast.iter_fields(node):
        if isinstance(value, list):
            items = [_elide_nested(item) for item in value]
            if any(new is not old for new, old in zip(items, value, strict=True)):
                replacements[name] = items
        elif isinstance(value, ast.AST):
            item = _elide_nested(value)
            if item is not value:
                replacements[name] = item
    if not replacements:
        return node
    clone = copy.copy(node)
    for name, value in replacements.items():
        setattr(clone, name, value)
    return clone


def _module_body_hash(tree: ast.Module) -> str:
    """Structural hash of `tree`'s top-level statements, reusing
    `_body_hash` (the same dump-and-hash `parse.py` already uses for a
    def/class's own body) over a view with nested def/class bodies elided."""
    return _body_hash(_elide_children(tree))


def _class_body_hash(node: ast.ClassDef) -> str:
    """Structural hash of a class def, with every nested method/nested
    class's OWN body elided -- the same treatment `_module_body_hash`
    gives a module's top-level statements, applied one level down.

    Without this, a class's `body_hash` covers its methods' bodies too, so
    editing one line inside a single method changes both that method's own
    `body_hash` (correctly) AND the class's (incorrectly) -- exactly the
    line-shift-insensitivity `body_hash` comparison exists to provide, but
    one level higher, and it matters for the same reason `diff` cares
    about module-level churn: an editor touching `PaymentService.charge`
    should not also report `PaymentService` itself as changed.

    `_elide_children` (rather than `_elide_nested`) is what makes this differ
    from hashing a plain def: it never elides `node` itself, so the class's own
    body list, bases, decorators and name stay intact -- only a
    `FunctionDef`/`ClassDef` found AMONG its children (a method, a nested
    class) gets its body replaced with a single `Pass`. Bases and decorators
    live in separate AST fields from `body`, so they're hashed as-is
    regardless.
    """
    return _body_hash(_elide_children(node))


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
        #: `(scope, name, kind, type, line, source_scope)`, aliases unresolved;
        #: see `_finalize_bindings`.
        self.raw_bindings: list[tuple] = []
        #: Qualnames of the enclosing classes, innermost last: where a
        #: `self.x = ...` inside a method is recorded.
        self._classes: list[str] = []

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
        # A class's own body_hash elides its nested defs' bodies (mirroring
        # _module_body_hash), since each method already carries its own
        # unelided body_hash -- otherwise editing one line inside a single
        # method would also change the class's hash. A function/method has
        # no nested defs to elide out this way: its own body IS the thing
        # body_hash is comparing.
        body_hash = _class_body_hash(node) if kind == "class" else _body_hash(node)
        self.nodes.append(
            ParsedNode(
                ordinal=len(self.nodes),
                qualname=self._qualname(node.name),
                kind=kind,
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno),
                body_hash=body_hash,
                name_binding="live",
                shadow_index=None,
                conditional=int(bool(self._conditional_depth) or is_overload),
                decorators=decorators,
            )
        )
        if kind == "class":
            for base in node.bases:
                # `Protocol[T]` / `Base[T]`: the class inherits from what is
                # subscripted -- the argument parametrizes it, it is not a
                # different base. Dropping it would make a generic Protocol
                # indistinguishable from a plain class, and the receiver step
                # would then bind calls through it to the stub alone.
                if isinstance(base, ast.Subscript):
                    base = base.value
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

        qualname = self._qualname(node.name)
        if kind in ("function", "method"):
            skip_first = kind == "method" and not any(
                d.rpartition(".")[2] == "staticmethod" for d in decorators
            )
            self._record_parameters(node.args, qualname, skip_first)

        self._scope.append(node.name)
        self._kinds.append("class" if kind == "class" else "function")
        if kind == "class":
            self._classes.append(qualname)
        if kind in ("function", "method"):
            self._scope.append("<locals>")
        self.generic_visit(node)
        if kind in ("function", "method"):
            self._scope.pop()
        if kind == "class":
            self._classes.pop()
        self._kinds.pop()
        self._scope.pop()

    # -- bindings ----------------------------------------------------------
    @property
    def _binding_scope(self) -> str:
        """The scope a name bound here belongs to, spelled as refs spell it."""
        return self._current_owner.removesuffix(".<locals>")

    def _add_binding(
        self, scope: str, name: str, kind: str, type_: str | None, line: int
    ) -> None:
        self.raw_bindings.append((scope, name, kind, type_, line, self._binding_scope))

    def _record_parameters(self, args: ast.arguments, scope: str, skip_first: bool) -> None:
        """A parameter is a binding like any other. Annotated, it names its
        type; unannotated, it is opaque -- the caller may pass anything, so a
        later `item = Item()` in the body does not make `item` an `Item`.

        The implicit first parameter of a method (`self`, `cls`) is skipped:
        `self.X` is `_through_self`'s, which answers it by a stronger route.
        `*args: T` and `**kwargs: T` hold a tuple and a dict OF `T`, so they are
        opaque whatever they are annotated with.
        """
        positional = [*args.posonlyargs, *args.args]
        if skip_first and positional:
            positional = positional[1:]
        for arg in [*positional, *args.kwonlyargs]:
            types = _annotation_types(arg.annotation) if arg.annotation else None
            if types is None:
                self._add_binding(scope, arg.arg, OPAQUE, None, arg.lineno)
            for type_ in types or ():
                self._add_binding(scope, arg.arg, ANNOTATION, type_, arg.lineno)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._add_binding(scope, arg.arg, OPAQUE, None, arg.lineno)

    def _bind(self, target: ast.expr, entries: list[tuple[str, str]] | None, line: int) -> None:
        """Record `target` as bound to `entries` (None: opaque).

        A plain name belongs to the current scope -- except in a class body,
        where it is a class attribute, readable through `self`, and is recorded
        as `self.x` on the class. `self.x` inside a method belongs to the
        nearest enclosing class. Unpacking binds every element, opaquely: the
        parser does not track which element of a tuple is which.
        """
        if isinstance(target, ast.Name):
            if self._kinds[-1] == "class":
                self._record(self._classes[-1], f"self.{target.id}", entries, line)
            else:
                self._record(self._binding_scope, target.id, entries, line)
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and self._classes
        ):
            self._record(self._classes[-1], f"self.{target.attr}", entries, line)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                self._bind(element, None, line)
        elif isinstance(target, ast.Starred):
            self._bind(target.value, None, line)

    def _record(
        self, scope: str, name: str, entries: list[tuple[str, str]] | None, line: int
    ) -> None:
        if entries is None:
            self._add_binding(scope, name, OPAQUE, None, line)
            return
        for kind, type_ in entries:
            self._add_binding(scope, name, kind, type_, line)

    @staticmethod
    def _target_name(target: ast.expr) -> str:
        return _dotted_name(target) or ""

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind(target, _value_types(node.value, self._target_name(target)), node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        types = _annotation_types(node.annotation)
        entries = None if types is None else [(ANNOTATION, type_) for type_ in types]
        self._bind(node.target, entries, node.lineno)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind(node.target, _value_types(node.value, node.target.id), node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind(node.target, None, node.lineno)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._bind(node.target, None, node.lineno)
        self.generic_visit(node)

    visit_AsyncFor = visit_For  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        # The target is what `__enter__` returns, not what was called.
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars, None, node.lineno)
        self.generic_visit(node)

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._record(self._binding_scope, node.name, None, node.lineno)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        # Comprehensions get no scope of their own here, so their variables are
        # recorded (opaquely) in the enclosing one. That over-reports: an outer
        # `x = Item()` alongside `[x for x in rows]` becomes unknown. It is the
        # safe direction -- the resolver falls back to what it did before.
        self._bind(node.target, None, node.target.lineno)
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            self._record(self._binding_scope, arg.arg, None, node.lineno)
        for arg in (node.args.vararg, node.args.kwarg):
            if arg is not None:
                self._record(self._binding_scope, arg.arg, None, node.lineno)
        self.generic_visit(node)

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
            if isinstance(node.func, ast.Attribute) and _is_super_call(node.func.value):
                # `super().__init__()` is not an unknown receiver. It names the
                # enclosing class's base, which the resolver already knows, so
                # keeping it apart from `<attr>.` is the difference between a
                # certainty and a 1-in-N guess.
                #
                # Before this, `super().__init__()` flattened to
                # `<attr>.__init__` and fell to the repo-wide name match: 26
                # candidates per call site on psf/requests, all LOW, exactly
                # one right. So `impact BaseAdapter.__init__` reported
                # `symbols: 0` by default and 157 dependents with `--all` --
                # for the highest-blast-radius edit there is, since adding a
                # required argument to a base `__init__` breaks every subclass.
                name = f"{SUPER}.{node.func.attr}"
            else:
                name = (
                    f"<attr>.{node.func.attr}"
                    if isinstance(node.func, ast.Attribute)
                    else "<dynamic>"
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
        self._record_outer_rebinding(node, node.names, ["<module>"])

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._record_global(node, node.names)
        self._record_outer_rebinding(
            node, node.names, enclosing_function_scopes(self._binding_scope)
        )

    def _record_outer_rebinding(self, node: ast.AST, names: list[str], scopes: list[str]) -> None:
        """`global x` / `nonlocal x` let this scope rebind a name that belongs
        to another one, with whatever it likes. The outer scope's own bindings
        of `x` are then not all of them, so an opaque binding is recorded there
        (and here). Which outer scope `nonlocal` means is the nearest one that
        binds the name; marking every enclosing function is the conservative
        superset."""
        for name in names:
            for scope in [self._binding_scope, *scopes]:
                self._add_binding(scope, name, OPAQUE, None, node.lineno)

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
        bindings=_finalize_bindings(collector.raw_bindings),
        module_body_hash=_module_body_hash(tree),
        error=None,
    )
