import ast
import copy

from codegraph.parse import (
    _body_hash,
    _class_body_hash,
    _module_body_hash,
    parse_blob,
)


def qualnames(result):
    return [n.qualname for n in result.nodes]


def test_module_functions_and_classes():
    result = parse_blob(
        b"def alpha():\n    pass\n\n\nclass Beta:\n    def gamma(self):\n        pass\n"
    )
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


def _class_hash(result):
    return next(n.body_hash for n in result.nodes if n.qualname == "Beta")


def _method_hash(result):
    return next(n.body_hash for n in result.nodes if n.qualname == "Beta.gamma")


def test_class_body_hash_ignores_a_method_body_edit():
    """B7 regression: a class's body_hash used to include every nested
    method's body verbatim, so editing one line inside a single method
    reported BOTH the method AND its class as changed. `_BodyElider`
    already solved exactly this for module nodes; it just wasn't applied
    to classes too."""
    one = parse_blob(b"class Beta:\n    def gamma(self):\n        return 1\n")
    two = parse_blob(b"class Beta:\n    def gamma(self):\n        return 2\n")
    assert _class_hash(one) == _class_hash(two)
    # The method's own body_hash still changes -- only the class's doesn't.
    assert _method_hash(one) != _method_hash(two)


def test_class_body_hash_changes_when_a_method_is_added():
    one = parse_blob(b"class Beta:\n    def gamma(self):\n        pass\n")
    two = parse_blob(
        b"class Beta:\n    def gamma(self):\n        pass\n\n    def delta(self):\n        pass\n"
    )
    assert _class_hash(one) != _class_hash(two)


def test_class_body_hash_changes_with_bases_and_decorators():
    plain = parse_blob(b"class Beta:\n    def gamma(self):\n        pass\n")
    subclassed = parse_blob(b"class Beta(Base):\n    def gamma(self):\n        pass\n")
    decorated = parse_blob(b"@final\nclass Beta:\n    def gamma(self):\n        pass\n")
    assert _class_hash(plain) != _class_hash(subclassed)
    assert _class_hash(plain) != _class_hash(decorated)


def test_class_body_hash_ignores_line_position():
    top = parse_blob(b"class Beta:\n    def gamma(self):\n        pass\n")
    shifted = parse_blob(b"\n\n\nclass Beta:\n    def gamma(self):\n        pass\n")
    assert _class_hash(top) == _class_hash(shifted)


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
    call = next(r for r in result.refs if r.ref_kind == "call")
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


def test_calls_on_non_flattenable_receivers_are_still_recorded():
    """Regression for F2: a call whose receiver isn't a flattenable
    Name/Attribute chain used to be dropped from `refs` entirely -- no edge
    and no `unresolved` row, so the loss was invisible. `super().go()`,
    `PaymentService().charge(x)`, `self.items[0].run()`, `(a or b).fire()`
    and `d["k"].m()` must all still produce a `call` ref, carrying the
    attribute name (marked with the synthetic `<attr>.` prefix so it is
    routed past the HIGH-confidence resolver steps rather than falsely
    matched as an imported/module-local/self name)."""
    source = (
        b"class Base:\n"
        b"    def go(self):\n        pass\n\n\n"
        b"class Child(Base):\n"
        b"    def go(self):\n"
        b"        super().go()\n"
        b"        PaymentService().charge(1)\n"
        b"        self.items[0].run()\n"
        b"        (a or b).fire()\n"
        b"        d['k'].m()\n"
)
    result = parse_blob(source)
    raw = {r.raw_name for r in result.refs if r.ref_kind == "call"}
    assert raw >= {
        "<attr>.go",
        "<attr>.charge",
        "<attr>.run",
        "<attr>.fire",
        "<attr>.m",
    }


def test_call_with_no_attribute_at_all_is_recorded_under_a_placeholder():
    """`handlers[i]()` -- the callable itself isn't even an attribute
    access, so there is no name at all to key on; it must still be counted,
    not silently dropped."""
    source = b"def dispatch(handlers, i):\n    handlers[i]()\n"
    result = parse_blob(source)
    raw = {r.raw_name for r in result.refs if r.ref_kind == "call"}
    assert raw == {"<dynamic>"}


def test_open_call_mode_is_captured_in_raw_name():
    """`open`'s effect kind depends on its mode argument, which the effect
    catalog (a plain dotted-name matcher) can never see on its own -- the
    parser is the one place with the call's AST, so it encodes what it
    learns into the ref's `raw_name` for the catalog to key on."""
    source = (
        b"def f(mode):\n"
        b"    open('a')\n"
        b"    open('b', 'r')\n"
        b"    open('c', 'w')\n"
        b"    open('d', 'ab')\n"
        b"    open('e', mode='x')\n"
        b"    open('f', mode)\n"
    )
    result = parse_blob(source)
    raw = [r.raw_name for r in result.refs if r.ref_kind == "call"]
    assert raw == ["open", "open", "open!write", "open!write", "open!write", "open!ambiguous"]


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
    base = next(r for r in result.refs if r.ref_kind == "base")
    assert base.from_qualname == "Child"
    assert base.raw_name == "Parent"


def test_syntax_error_returns_error_not_exception():
    result = parse_blob(b"def broken(:\n")
    assert result.error is not None
    assert result.nodes == ()


def test_parse_is_deterministic():
    source = b"class A:\n    def m(self):\n        return other()\n"
    assert parse_blob(source) == parse_blob(source)


def test_global_statement_recorded_as_ref():
    source = b"COUNT = 0\n\n\ndef bump():\n    global COUNT\n    COUNT += 1\n"
    result = parse_blob(source)
    ref = next(r for r in result.refs if r.ref_kind == "global")
    assert ref.from_qualname == "bump"
    assert ref.raw_name == "COUNT"
    assert ref.line == 5


def test_nonlocal_statement_recorded_as_global_ref():
    source = (
        b"def outer():\n"
        b"    x = 0\n"
        b"    def inner():\n"
        b"        nonlocal x\n"
        b"        x += 1\n"
        b"    return inner\n"
    )
    result = parse_blob(source)
    ref = next(r for r in result.refs if r.ref_kind == "global")
    assert ref.from_qualname == "outer.<locals>.inner"
    assert ref.raw_name == "x"


def test_global_statement_with_multiple_names_recorded_separately():
    source = b"def bump():\n    global A, B\n    A += 1\n    B += 1\n"
    result = parse_blob(source)
    names = {r.raw_name for r in result.refs if r.ref_kind == "global"}
    assert names == {"A", "B"}


def test_module_body_hash_ignores_line_shift():
    top = parse_blob(b"def alpha():\n    return 1\n")
    shifted = parse_blob(b"\n\n\ndef alpha():\n    return 1\n")
    assert top.module_body_hash == shifted.module_body_hash


def test_module_body_hash_ignores_nested_body_changes():
    one = parse_blob(b"def alpha():\n    return 1\n")
    two = parse_blob(b"def alpha():\n    return 2\n")
    assert one.module_body_hash == two.module_body_hash


def test_module_body_hash_changes_with_top_level_statement():
    one = parse_blob(b"def alpha():\n    return 1\n")
    two = parse_blob(b"import requests\n\n\ndef alpha():\n    return 1\n")
    assert one.module_body_hash != two.module_body_hash


def test_module_body_hash_changes_when_def_is_added():
    one = parse_blob(b"def alpha():\n    return 1\n")
    two = parse_blob(b"def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
    assert one.module_body_hash != two.module_body_hash


# -- body hashing is a persistent transform, not a deep copy ----------------
#
# `_elide_children` replaced a `deepcopy` + `NodeTransformer` pair. The copy was
# quadratic in practice -- a module deep-copied once, then every class inside it
# deep-copied again for its own hash -- and cost 4.8s to parse django's largest
# test module, 3.8s of it inside `copy.deepcopy`. That put the "parse is
# proportional to the diff" half of the cost guarantee on the wrong side of a
# one-file edit. See #7.
#
# The reference implementation below is the code that was replaced. It is kept
# as an oracle: the rewrite had to produce byte-identical hashes, or every
# `body_hash` in every store would change meaning. Verified equal over all 2,929
# modules and 11,072 classes of django before landing; this pins the property on
# inputs a unit test can carry.


class _ReferenceElider(ast.NodeTransformer):
    """The pre-rewrite implementation, kept only to check the new one."""

    def _elide(self, node):
        clone = copy.copy(node)
        clone.body = [ast.Pass()]
        return clone

    def visit_FunctionDef(self, node):
        return self._elide(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        return self._elide(node)


def _reference_module_hash(tree):
    return _body_hash(_ReferenceElider().visit(copy.deepcopy(tree)))


def _reference_class_hash(node):
    skeleton = copy.deepcopy(node)
    _ReferenceElider().generic_visit(skeleton)
    return _body_hash(skeleton)


NASTY_SOURCE = '''
import os
from a import b as c

CONST = [1, 2, {"k": (3, 4)}]

@decorated(arg=1)
async def top(a, *args, b: int = 2, **kw) -> None:
    """doc"""
    def closure():
        return 1
    class Inner:
        def deep(self):
            return closure()
    return Inner


class Outer(Base, metaclass=Meta):
    """doc"""
    attr = 1

    class Nested:
        def method(self):
            if True:
                def deeper():
                    pass
            return 2

    async def amethod(self):
        async with x:
            pass

    @property
    def prop(self):
        return self.attr


if os.environ:
    def conditional():
        pass
else:
    class AlsoConditional:
        pass

try:
    from d import e
except ImportError:
    e = None
'''


def test_the_rewritten_body_hash_matches_the_implementation_it_replaced():
    tree = ast.parse(NASTY_SOURCE)
    assert _module_body_hash(tree) == _reference_module_hash(tree)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert len(classes) >= 4, "fixture stopped covering nested and conditional classes"
    for node in classes:
        assert _class_body_hash(node) == _reference_class_hash(node), node.name


def test_hashing_does_not_mutate_the_tree_it_is_given():
    """The whole reason the old code deep-copied. If the transform ever starts
    writing back into the original, the second hash of the same tree differs
    from the first and every cached `body_hash` silently rots."""
    tree = ast.parse(NASTY_SOURCE)
    before = ast.dump(tree)
    first = _module_body_hash(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _class_body_hash(node)
    assert ast.dump(tree) == before
    assert _module_body_hash(tree) == first
