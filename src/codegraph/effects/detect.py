"""Direct effect detection: catalog matches against refs, not edges.

`requests.get` never resolves to a node in this repo -- resolution only
links to symbols the repo itself defines -- so an edge-only pass would miss
every external call, which is most real side effects. This module reads
`blob_refs` (joined through `tree` for the revision) instead: every `call`
ref is expanded through its file's import map to a canonical dotted name
and matched against the effect `Catalog`; every `global`/`nonlocal` ref
(`ref_kind="global"`, from `parse.py`) is a `GLOBAL_MUTATE` by construction,
since that kind is syntactic and has no catalog pattern.
"""

from __future__ import annotations

from codegraph.config import Config
from codegraph.effects.catalog import Catalog
from codegraph.resolve import HIGH, MODULE_SCOPE, build_import_maps, module_for_path
from codegraph.store import Store


def detect_direct(store: Store, rev: str, catalog: Catalog, config: Config) -> int:
    """Tag every ref in `rev` that is a direct effect. Returns rows written."""
    connection = store.connection
    import_maps, _imported_modules = build_import_maps(connection, rev, config)
    owner_index, live_index = _owner_index(store, rev)
    first_party = _first_party_modules(store, rev, config)

    rows: list[tuple] = []
    for row in connection.execute(
        "SELECT t.path AS path, r.from_qualname, r.ref_kind, r.raw_name, r.line"
        " FROM blob_refs r JOIN tree t ON t.blob_sha = r.blob_sha"
        " WHERE t.rev=? AND r.ref_kind IN ('call', 'global')",
        (rev,),
    ):
        if row["ref_kind"] == "global":
            # Syntactic detection (assignment to a name declared `global`/
            # `nonlocal`), not a catalog match -- there is no pattern
            # specificity to derive uncertainty from, and none is
            # warranted: this is exact by construction.
            kind, confidence = "GLOBAL_MUTATE", HIGH
        else:
            dotted = _expand(row["raw_name"], import_maps.get(row["path"], {}))
            matched = catalog.match_with_confidence(
                dotted, overrides_only=_is_first_party(dotted, first_party)
            )
            if matched is None:
                continue
            kind, confidence = matched
        node_id = _owning_node(
            row["path"], row["from_qualname"], row["line"], owner_index, live_index
        )
        rows.append((rev, node_id, kind, 1, row["path"], row["line"], confidence))

    connection.execute("DELETE FROM effects WHERE rev=? AND direct=1", (rev,))
    connection.executemany(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _first_party_modules(store: Store, rev: str, config: Config) -> set[str]:
    """Every module name this revision defines."""
    return {
        module_for_path(row["path"], config.source_roots)
        for row in store.connection.execute("SELECT path FROM tree WHERE rev=?", (rev,))
    }


def _is_first_party(dotted: str, first_party: set[str]) -> bool:
    """Does `dotted` name something inside a module this repository defines?

    The built-in catalog describes third-party libraries. When the repository
    under analysis IS one of those libraries -- or merely shares a namespace
    prefix with one -- every internal call expands into the catalogued
    namespace and matches it.

    Measured on `psf/requests`: `resolve_proxies` expands through the import
    map to `requests.utils.resolve_proxies`, which the `requests.*` NETWORK
    rule matches at MEDIUM. `Session.send` ended up with five direct NETWORK
    rows, none of them on the line holding `adapter.send(...)` -- the only call
    there that actually touches the network, and one the catalog does not match
    at all. The effects layer was close to inverted on that repo. See #12.

    Any dotted prefix being a module of this revision is enough: `requests`,
    `requests.utils` and `requests.utils.resolve_proxies` all mean the name
    resolves into first-party code.
    """
    parts = dotted.split(".")
    return any(".".join(parts[:split]) in first_party for split in range(len(parts), 0, -1))


def _expand(raw_name: str, import_map: dict[str, str]) -> str:
    """Expand a local alias through the file's import map, e.g. `db.save`
    (with `from app import db`) -> `app.db.save`, so a project override like
    `app.db.*` matches the house abstraction rather than the call's literal
    spelling. A name whose head isn't imported (a builtin, a local, `self`)
    passes through unchanged, which is what the built-in catalog expects."""
    head, _, rest = raw_name.partition(".")
    target = import_map.get(head)
    if target is None:
        return raw_name
    return f"{target}.{rest}" if rest else target


def _owner_index(
    store: Store, rev: str
) -> tuple[dict[tuple[str, str], list[tuple[str, int, int]]], dict[tuple[str, str], str]]:
    """(path, qualname) -> [(node id, line_start, line_end), ...], including
    shadowed definitions, so a ref inside a shadowed body still attributes
    to it rather than to the live definition sharing its name; and
    (path, qualname) -> live node id, mirroring `resolve.py`'s
    `qualname_index` for the same-fallback reason `_owning_node` uses it."""
    index: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    live: dict[tuple[str, str], str] = {}
    for row in store.connection.execute(
        "SELECT id, path, qualname, line_start, line_end, name_binding FROM nodes"
        " WHERE rev=? ORDER BY id",
        (rev,),
    ):
        key = (row["path"], row["qualname"])
        index.setdefault(key, []).append((row["id"], row["line_start"], row["line_end"]))
        if row["name_binding"] == "live":
            live[key] = row["id"]
    return index, live


def _owning_node(
    path: str,
    from_qualname: str,
    line: int,
    index: dict[tuple[str, str], list[tuple[str, int, int]]],
    live: dict[tuple[str, str], str],
) -> str:
    """The node that owns a ref; mirrors `resolve.py`'s `_source_id`,
    including its fallback to the live binding (not just "the last
    candidate") when a ref's line falls inside no recorded span -- so
    detection and resolution attribute the same ref to the same node."""
    if from_qualname == MODULE_SCOPE:
        return f"{path}::{MODULE_SCOPE}"
    key = (path, from_qualname)
    candidates = index.get(key)
    if not candidates:
        return f"{path}::{from_qualname}"
    if len(candidates) == 1:
        return candidates[0][0]
    for node_id, line_start, line_end in candidates:
        if line_start <= line <= line_end:
            return node_id
    return live.get(key, candidates[-1][0])
