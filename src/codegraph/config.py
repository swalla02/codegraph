"""Project configuration, read from a tracked codegraph.toml at the repo root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "codegraph.toml"


@dataclass(frozen=True)
class Config:
    source_roots: tuple[str, ...] = ("", "src")
    effect_overrides: tuple[dict, ...] = ()

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / CONFIG_NAME
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text())
        return cls(
            source_roots=tuple(data.get("source_roots", ["", "src"])),
            effect_overrides=tuple(data.get("effect", [])),
        )
