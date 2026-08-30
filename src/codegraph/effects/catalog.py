"""Effect catalog: dotted-name glob patterns mapped to side-effect kinds.

Rules come from two sources merged at load time: the built-in patterns
shipped in `builtin.toml`, and a project's `codegraph.toml` overrides
(`Config.effect_overrides`). Most real side effects sit behind house
abstractions (`app.db.save`), not `requests.get` directly, which is why the
override channel exists at all rather than the built-in list being enough.

Precedence: on a name that matches more than one rule, the rule with the
longest leading literal segment (the run of characters before the first
glob wildcard) wins. `requests.get` (override, fully literal) beats
`requests.*` (built-in, literal prefix `requests.`), so an override always
beats a built-in it is more specific than, regardless of load order.

This module owns rules and matching only. Detecting which call sites are
effects, and propagating that through the call graph, is Task 10.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from codegraph.config import Config

EFFECT_KINDS: tuple[str, ...] = (
    "DB_READ",
    "DB_WRITE",
    "NETWORK",
    "FS_READ",
    "FS_WRITE",
    "PROCESS",
    "ENV_READ",
    "GLOBAL_MUTATE",
    "NONDETERMINISM",
)

_BUILTIN_TOML = Path(__file__).with_name("builtin.toml")

_WILDCARD = re.compile(r"[*?\[]")


def _literal_prefix_len(pattern: str) -> int:
    """Length of the leading run of literal characters before a glob wildcard."""
    found = _WILDCARD.search(pattern)
    return found.start() if found else len(pattern)


@dataclass(frozen=True)
class Rule:
    match: str
    kind: str


class _Compiled(NamedTuple):
    rule: Rule
    regex: re.Pattern[str]
    prefix_len: int
    is_override: bool


def _parse_rules(text: str) -> tuple[Rule, ...]:
    data = tomllib.loads(text)
    return tuple(Rule(match=entry["match"], kind=entry["kind"]) for entry in data.get("effect", []))


class Catalog:
    """A merged, ready-to-query set of effect rules."""

    def __init__(self, rules: tuple[Rule, ...], override_count: int) -> None:
        self._rules = rules
        first_override = len(rules) - override_count
        self._compiled = tuple(
            _Compiled(
                rule=rule,
                regex=re.compile(fnmatch.translate(rule.match)),
                prefix_len=_literal_prefix_len(rule.match),
                is_override=i >= first_override,
            )
            for i, rule in enumerate(rules)
        )

    @classmethod
    def load(cls, config: Config) -> Catalog:
        builtin_rules = _parse_rules(_BUILTIN_TOML.read_text())
        override_rules = tuple(
            Rule(match=entry["match"], kind=entry["kind"]) for entry in config.effect_overrides
        )
        return cls(builtin_rules + override_rules, len(override_rules))

    def match(self, dotted: str) -> str | None:
        """The kind of the best-matching rule for `dotted`, or None."""
        best: _Compiled | None = None
        for compiled in self._compiled:
            if not compiled.regex.match(dotted):
                continue
            key = (compiled.prefix_len, compiled.is_override, compiled.rule.match)
            best_key = (
                (best.prefix_len, best.is_override, best.rule.match) if best is not None else None
            )
            if best is None or key > best_key:
                best = compiled
        return best.rule.kind if best else None

    def fingerprint(self) -> str:
        """Stable hash over all rules: same rules -> same digest, any run.

        Unlike `hash()`, which is salted per process, this is a plain
        SHA-256 over a sorted, delimited encoding of every rule, so it is
        safe to bake into an on-disk cache key.
        """
        digest = hashlib.sha256()
        for rule in sorted(self._rules, key=lambda r: (r.match, r.kind)):
            digest.update(f"{rule.match}\x00{rule.kind}\n".encode())
        return digest.hexdigest()
