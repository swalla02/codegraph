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
from codegraph.resolve import HIGH, LOW, MEDIUM

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


_CONFIDENCES: tuple[str, ...] = (HIGH, MEDIUM, LOW)


@dataclass(frozen=True)
class Rule:
    match: str
    kind: str
    #: Explicit override; `None` (the default, and every built-in pattern
    #: but one) means "derive from match specificity" -- see
    #: `Catalog._confidence_for`. The one built-in exception is `open`'s
    #: non-literal-mode fallback (`open!ambiguous` in builtin.toml): a
    #: fully literal pattern name would otherwise derive HIGH, but the
    #: *kind* assigned to it (FS_READ, the conservative default) is not
    #: actually known -- the call's mode argument wasn't a literal, so
    #: this is the one case where the pattern's specificity doesn't speak
    #: to how much the evidence actually supports the kind it names.
    confidence: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in EFFECT_KINDS:
            raise ValueError(
                f"unknown effect kind {self.kind!r} for pattern {self.match!r}; "
                f"must be one of {EFFECT_KINDS}"
            )
        if self.confidence is not None and self.confidence not in _CONFIDENCES:
            raise ValueError(
                f"unknown confidence {self.confidence!r} for pattern {self.match!r}; "
                f"must be one of {_CONFIDENCES}"
            )


class _Compiled(NamedTuple):
    rule: Rule
    regex: re.Pattern[str]
    prefix_len: int
    is_override: bool


def _parse_rules(text: str) -> tuple[Rule, ...]:
    data = tomllib.loads(text)
    return tuple(
        Rule(match=entry["match"], kind=entry["kind"], confidence=entry.get("confidence"))
        for entry in data.get("effect", [])
    )


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
            Rule(match=entry["match"], kind=entry["kind"], confidence=entry.get("confidence"))
            for entry in config.effect_overrides
        )
        return cls(builtin_rules + override_rules, len(override_rules))

    def _best_match(self, dotted: str, *, overrides_only: bool = False) -> _Compiled | None:
        best: _Compiled | None = None
        for compiled in self._compiled:
            if overrides_only and not compiled.is_override:
                continue
            if not compiled.regex.match(dotted):
                continue
            key = (compiled.prefix_len, compiled.is_override, compiled.rule.match)
            best_key = (
                (best.prefix_len, best.is_override, best.rule.match) if best is not None else None
            )
            if best is None or key > best_key:
                best = compiled
        return best

    def match(self, dotted: str, *, overrides_only: bool = False) -> str | None:
        """The kind of the best-matching rule for `dotted`, or None."""
        best = self._best_match(dotted, overrides_only=overrides_only)
        return best.rule.kind if best else None

    def match_with_confidence(
        self, dotted: str, *, overrides_only: bool = False
    ) -> tuple[str, str] | None:
        """(kind, confidence) for the best-matching rule, or `None`.

        Confidence falls out of the winning rule's match specificity -- the
        same longest-literal-prefix signal `match` already uses to rank
        overlapping rules against each other (see the module docstring):
        a fully literal pattern with no wildcard at all (`requests.get`,
        `os.getenv`) is HIGH; a bare wildcard-head pattern (`*.execute`,
        matching a completely uninferable receiver) can be no more than
        LOW; anything in between -- a real but partial literal prefix, like
        `requests.*` or `boto3.*` -- sits at MEDIUM: the namespace is known,
        the specific member is not. A rule may override this via its own
        `confidence` field for the rare case where specificity of the NAME
        match doesn't track certainty of the KIND assigned to it (see
        `Rule.confidence`).

        `overrides_only` restricts matching to the project's own `[[effect]]`
        rules, skipping the built-in catalog. Callers pass it for a name that
        belongs to a module THIS repository defines: the built-in catalog
        describes third-party libraries, and applying it to first-party code
        misreads the project's own functions as library calls. Project
        overrides stay eligible, because naming house abstractions is exactly
        what they are for. See `detect.py` and issue #12.
        """
        best = self._best_match(dotted, overrides_only=overrides_only)
        if best is None:
            return None
        if best.rule.confidence is not None:
            return best.rule.kind, best.rule.confidence
        pattern_len = len(best.rule.match)
        if best.prefix_len == pattern_len:
            confidence = HIGH
        elif best.prefix_len == 0:
            confidence = LOW
        else:
            confidence = MEDIUM
        return best.rule.kind, confidence

    def fingerprint(self) -> str:
        """Stable hash over all rules: same rules -> same digest, any run.

        Unlike `hash()`, which is salted per process, this is a plain
        SHA-256 over a sorted, delimited encoding of every rule, so it is
        safe to bake into an on-disk cache key.
        """
        digest = hashlib.sha256()
        for rule in sorted(self._rules, key=lambda r: (r.match, r.kind)):
            digest.update(f"{rule.match}\x00{rule.kind}\x00{rule.confidence or ''}\n".encode())
        return digest.hexdigest()
