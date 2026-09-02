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
