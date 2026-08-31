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
    for command in ["codegraph resolve", "codegraph impact", "codegraph effects", "codegraph diff"]:
        assert command in text
