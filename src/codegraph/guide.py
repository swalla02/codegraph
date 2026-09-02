"""The agent-facing workflow text, and the one place it is authored.

Three consumers want the same words: `codegraph guide` (stdout, for any
agent), `skills/codegraph/SKILL.md` (the Claude Code plugin), and the
`AGENTS.md` block `codegraph init` writes -- which stays short precisely by
pointing at the first of those instead of restating it.

The text is authored in `guide.md`, *inside the package*, because that is
the only copy an installed `codegraph` has: the wheel ships
`src/codegraph/` and nothing else (see `pyproject.toml`), so a `guide` that
read `skills/codegraph/SKILL.md` from the repo would work from a checkout
and fail everywhere it actually matters. `SKILL.md` is therefore the
derived copy -- `guide.md` plus the skill frontmatter -- regenerated with
`python -m codegraph.guide` and pinned byte-for-byte by
`test_packaging.py::test_skill_md_is_the_generated_form_of_the_packaged_guide`,
so the two cannot drift silently in either direction.

Shipping a `.md` as package data is the same pattern as
`effects/builtin.toml`: hatchling includes every file under the packaged
directory, so no manifest entry is needed for it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GUIDE_PATH = Path(__file__).with_name("guide.md")

#: Skill-runtime metadata, which is about *when to load* the text rather
#: than part of the text -- so it lives here, with the generator, and not
#: in `guide.md`, which `codegraph guide` prints verbatim. The
#: `description` stays on one long line because a skill frontmatter value
#: is scalar: wrapping it would change what it means, not just how it looks.
_SKILL_FRONTMATTER = """---
name: codegraph
description: Use when about to modify a Python function or class, or when asked "what breaks if I change this", "what does this affect", "is this safe to change", or "what did this branch change" — answers with a ranked, git-native call graph instead of a grep.
---
"""


def guide_text() -> str:
    """The agent-facing workflow, as printed by `codegraph guide`."""
    return _GUIDE_PATH.read_text(encoding="utf-8")


def skill_text() -> str:
    """The full expected contents of `skills/codegraph/SKILL.md`."""
    return f"{_SKILL_FRONTMATTER}\n{guide_text()}"


def _regenerate_skill_file() -> int:
    """Dev chore: rewrite `skills/codegraph/SKILL.md` from `guide.md`.

    Deliberately not a `codegraph` subcommand -- it is maintenance of this
    repository's own files, not something a user of the tool ever runs, and
    the path it writes only exists in a checkout.
    """
    repo_root = Path(__file__).resolve().parents[2]
    skill_path = repo_root / "skills" / "codegraph" / "SKILL.md"
    if not skill_path.parent.is_dir():
        print(
            f"no skill directory at {skill_path.parent} (not a codegraph checkout?)",
            file=sys.stderr,
        )
        return 1
    skill_path.write_text(skill_text(), encoding="utf-8")
    print(skill_path)
    return 0


if __name__ == "__main__":
    sys.exit(_regenerate_skill_file())
