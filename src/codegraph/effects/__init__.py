"""Effect catalog: rules and matching for side-effect kinds.

Detecting effects at call sites and propagating them through the call
graph lives elsewhere (Task 10); this package only owns the rule set.
"""

from __future__ import annotations

from codegraph.effects.catalog import EFFECT_KINDS, Catalog, Rule

__all__ = ["EFFECT_KINDS", "Catalog", "Rule"]
