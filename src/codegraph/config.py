"""Project configuration, read from a tracked codegraph.toml at the repo root."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "codegraph.toml"

#: DEPRECATED (#25). The write-time cap on how many equally-plausible
#: low-confidence candidates one call site would be expanded into.
#:
#: It is still read, still validated, and no longer does anything, because
#: nothing needs it to. The last-resort resolution step matches a call's
#: final dotted segment against every live definition in the revision; the
#: resulting candidate set is `name_index[name]` verbatim, which the `nodes`
#: table already determines. Materializing it stored nothing the graph did
#: not contain, at a cost quadratic in repository size -- 971 candidates for
#: one django call site, 2.09M of 2.16M edges. So the fan-out is no longer
#: written to `edges` at all, at any size, and `ambiguity.py` expands
#: it on demand instead.
#:
#: Which makes the setting the wrong shape as well as unnecessary: whether
#: N candidates is too many is a property of the question being asked, not
#: of the graph. That bound is `--limit` now, per query, where a reader can
#: change it without rebuilding anything and without changing the answer
#: every other query gets. Setting `ambiguity_limit` warns; it will be
#: removed in a future release.
DEFAULT_AMBIGUITY_LIMIT = 25

#: Emitted once per `Config.load` that sees the deprecated key, on stderr so
#: it is visible in a terminal without polluting `--json` on stdout.
AMBIGUITY_LIMIT_DEPRECATED = (
    f"{CONFIG_NAME}: ambiguity_limit is deprecated and no longer affects the graph."
    " The bare-name fan-out is expanded at query time now; bound it per query"
    " with `impact --limit` instead. Remove the setting."
)


@dataclass(frozen=True)
class Config:
    source_roots: tuple[str, ...] = ("", "src")
    effect_overrides: tuple[dict, ...] = ()
    #: Retained for one release so an existing codegraph.toml keeps loading.
    #: Nothing reads it; see `DEFAULT_AMBIGUITY_LIMIT`.
    ambiguity_limit: int = DEFAULT_AMBIGUITY_LIMIT

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / CONFIG_NAME
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        limit = data.get("ambiguity_limit", DEFAULT_AMBIGUITY_LIMIT)
        # Still rejected rather than coerced. A deprecated setting that
        # silently accepts nonsense is a worse migration than one that keeps
        # saying no: a user who wrote `ambiguity_limit = "lots"` has a
        # mistaken belief about the file, and this is the release in which
        # they can still be told about it.
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError(
                f"{CONFIG_NAME}: ambiguity_limit must be a non-negative integer, got {limit!r}"
            )
        if "ambiguity_limit" in data:
            print(AMBIGUITY_LIMIT_DEPRECATED, file=sys.stderr)
        return cls(
            source_roots=tuple(data.get("source_roots", ["", "src"])),
            effect_overrides=tuple(data.get("effect", [])),
            ambiguity_limit=limit,
        )
