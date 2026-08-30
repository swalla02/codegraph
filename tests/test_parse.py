from codegraph.parse import parse_blob


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
