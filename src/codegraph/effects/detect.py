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
from codegraph.resolve import (
    HIGH,
    MODULE_SCOPE,
    absolute_module,
    module_for_path,
    package_for_module,
)
from codegraph.store import Store


def detect_direct(store: Store, rev: str, catalog: Catalog) -> int:
    """Tag every ref in `rev` that is a direct effect. Returns rows written."""
    connection = store.connection
    config = Config.load(store.directory.parent)
    import_maps = _import_maps(store, rev, config)
    owner_index = _owner_index(store, rev)

    rows: list[tuple] = []
    for row in connection.execute(
        "SELECT t.path AS path, r.from_qualname, r.ref_kind, r.raw_name, r.line"
        " FROM blob_refs r JOIN tree t ON t.blob_sha = r.blob_sha"
        " WHERE t.rev=? AND r.ref_kind IN ('call', 'global')",
        (rev,),
    ):
        if row["ref_kind"] == "global":
            kind = "GLOBAL_MUTATE"
        else:
            dotted = _expand(row["raw_name"], import_maps.get(row["path"], {}))
            kind = catalog.match(dotted)
        if kind is None:
            continue
        node_id = _owning_node(row["path"], row["from_qualname"], row["line"], owner_index)
        rows.append((rev, node_id, kind, 1, row["path"], row["line"], HIGH))

    connection.execute("DELETE FROM effects WHERE rev=? AND direct=1", (rev,))
    connection.executemany(
        "INSERT INTO effects(rev, node_id, kind, direct, evidence_path, evidence_line,"
        " confidence) VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


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


def _import_maps(store: Store, rev: str, config: Config) -> dict[str, dict[str, str]]:
    """Per-path local-alias -> absolute-dotted-module map.

    Mirrors the alias expansion `resolve.py`'s symbol table builds for call
    resolution, but detection only needs the alias map, not a full resolver.
    """
    connection = store.connection
    paths = [
        row["path"]
        for row in connection.execute("SELECT DISTINCT path FROM tree WHERE rev=?", (rev,))
    ]
    module_for = {path: module_for_path(path, config.source_roots) for path in paths}
    maps: dict[str, dict[str, str]] = {path: {} for path in paths}

    rows = connection.execute(
        "SELECT t.path, i.module, i.level, i.name, i.alias FROM blob_imports i"
        " JOIN tree t ON t.blob_sha = i.blob_sha WHERE t.rev=? ORDER BY t.path, i.ordinal",
        (rev,),
    )
    for row in rows:
        path = row["path"]
        alias_map = maps[path]
        package = package_for_module(module_for[path], path)
        module = absolute_module(row["module"], row["level"], package)
        if row["name"] is None:
            alias_map[row["alias"] or module] = module
            if row["alias"] is None:
                alias_map.setdefault(module.partition(".")[0], module.partition(".")[0])
        else:
            target = f"{module}.{row['name']}" if module else row["name"]
            alias_map[row["alias"] or row["name"]] = target
    return maps


def _owner_index(store: Store, rev: str) -> dict[tuple[str, str], list[tuple[str, int, int]]]:
    """(path, qualname) -> [(node id, line_start, line_end), ...], including
    shadowed definitions, so a ref inside a shadowed body still attributes
    to it rather than to the live definition sharing its name."""
    index: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    for row in store.connection.execute(
        "SELECT id, path, qualname, line_start, line_end FROM nodes WHERE rev=? ORDER BY id",
        (rev,),
    ):
        index.setdefault((row["path"], row["qualname"]), []).append(
            (row["id"], row["line_start"], row["line_end"])
        )
    return index


def _owning_node(
    path: str,
    from_qualname: str,
    line: int,
    index: dict[tuple[str, str], list[tuple[str, int, int]]],
) -> str:
    """The node that owns a ref; mirrors `resolve.py`'s `_source_id`."""
    if from_qualname == MODULE_SCOPE:
        return f"{path}::{MODULE_SCOPE}"
    candidates = index.get((path, from_qualname))
    if not candidates:
        return f"{path}::{from_qualname}"
    if len(candidates) == 1:
        return candidates[0][0]
    for node_id, line_start, line_end in candidates:
        if line_start <= line <= line_end:
            return node_id
    return candidates[-1][0]
