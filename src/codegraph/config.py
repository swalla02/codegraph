"""Project configuration, read from a tracked codegraph.toml at the repo root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "codegraph.toml"

#: How many equally-plausible low-confidence candidates a single call site may
#: name before the graph stops enumerating them.
#:
#: The last-resort resolution step matches a call's final dotted segment against
#: every live definition in the revision. On a small codebase that yields a
#: handful of candidates and the over-approximation is useful. On a large one it
#: yields hundreds -- 971 for a single call site in django -- and the resulting
#: edge set says only "this could be anything named `save`", which is the
#: question restated, not an answer to it. Enumerating it is also quadratic in
#: repo size.
#:
#: Above this many LOW candidates the call site is recorded once as ambiguous
#: (with the name and the count) instead of as N edges. Nothing is discarded to
#: improve precision -- the claim is identical, stored in O(1) rather than O(n).
#: Raise it (or set it to 0 for no limit) if you want the full cross product.
DEFAULT_AMBIGUITY_LIMIT = 25


@dataclass(frozen=True)
class Config:
    source_roots: tuple[str, ...] = ("", "src")
    effect_overrides: tuple[dict, ...] = ()
    ambiguity_limit: int = DEFAULT_AMBIGUITY_LIMIT

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / CONFIG_NAME
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        limit = data.get("ambiguity_limit", DEFAULT_AMBIGUITY_LIMIT)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError(
                f"{CONFIG_NAME}: ambiguity_limit must be a non-negative integer, got {limit!r}"
            )
        return cls(
            source_roots=tuple(data.get("source_roots", ["", "src"])),
            effect_overrides=tuple(data.get("effect", [])),
            ambiguity_limit=limit,
        )
