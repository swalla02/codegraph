import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_is_valid():
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert data["name"] == "codegraph"
    assert data["version"]
    assert data["description"]


def test_marketplace_lists_the_plugin():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    names = {p["name"] for p in data["plugins"]}
    assert "codegraph" in names
    assert data["plugins"][0]["source"] == "./"


def test_skill_has_frontmatter_and_covers_triggers():
    text = (ROOT / "skills" / "codegraph" / "SKILL.md").read_text()
    assert text.startswith("---")
    assert "name: codegraph" in text
    assert "description:" in text
    lowered = text.lower()
    for trigger in ["what breaks", "safe to change", "grep"]:
        assert trigger in lowered


def test_skill_documents_every_shipped_command():
    text = (ROOT / "skills" / "codegraph" / "SKILL.md").read_text()
    for command in [
        "codegraph resolve",
        "codegraph impact",
        "codegraph effects",
        "codegraph orphans",
        "codegraph diff",
    ]:
        assert command in text


def test_every_command_the_readme_names_actually_exists():
    """Pins the README's command names to the CLI's actual subcommands.

    Prompted by the README opening on `impact_of(symbol)` and
    `effects_of(symbol)` -- the API sketch from before the CLI existed, left
    behind when it landed. Note this test would NOT have caught those two: they
    were written without the `codegraph ` prefix, so nothing marked them as
    commands at all. What it does catch is the ongoing case -- a command renamed
    or removed while the README keeps advertising it, which is the way this
    drifts once the names are written properly.
    """
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    named = set(re.findall(r"codegraph ([a-z][a-z-]+)", readme))
    assert named, "no commands found in the README -- has the format changed?"

    help_text = _cli_help()
    real = set(_subcommands(help_text))
    assert named <= real, f"README names commands that do not exist: {sorted(named - real)}"


def _cli_help():
    import contextlib
    import io

    from codegraph.cli import main

    # `main` builds its parser internally; --help is the supported way to see it.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
        main(["--help"])
    return buffer.getvalue()


def _subcommands(help_text):
    import re

    # argparse lists subcommands in a {a,b,c} block.
    match = re.search(r"\{([a-z,\-]+)\}", help_text)
    return match.group(1).split(",") if match else []


def test_skill_md_is_the_generated_form_of_the_packaged_guide():
    """`codegraph guide` and the plugin's SKILL.md must say the same thing,
    forever.

    They are one file: `src/codegraph/guide.md` is authored, and SKILL.md is
    that plus the skill frontmatter. It has to be the package copy that is
    canonical, because an installed codegraph ships `src/codegraph/` and
    nothing else -- a `guide` reading SKILL.md out of the repo would work
    from a checkout and fail on every real install.

    If this fails, you edited SKILL.md directly. Edit
    `src/codegraph/guide.md` instead and regenerate with
    `uv run python -m codegraph.guide`.
    """
    from codegraph.guide import skill_text

    assert (ROOT / "skills" / "codegraph" / "SKILL.md").read_text() == skill_text()
