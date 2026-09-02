import hashlib
import os
import tomllib
from pathlib import Path

import pytest

from codegraph.cli import main
from codegraph.config import CONFIG_NAME, Config
from codegraph.effects.catalog import EFFECT_KINDS
from codegraph.guide import guide_text
from codegraph.init import AGENTS_BLOCK, BEGIN_MARKER, END_MARKER


def fingerprint(root: Path) -> dict[str, str]:
    """Content hash of every file under `root`, for byte-identity checks."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_init_creates_agents_md_and_a_config_stub(repo, capsys):
    """The whole point of the command: a repo that said nothing about
    codegraph now tells any AGENTS.md-reading agent about it, and carries a
    config file to edit when the built-in effect catalog isn't enough."""
    assert main(["init", "--path", str(repo)]) == 0

    agents = (repo / "AGENTS.md").read_text()
    assert BEGIN_MARKER in agents and END_MARKER in agents
    assert "codegraph impact" in agents
    assert "codegraph guide" in agents
    assert (repo / CONFIG_NAME).exists()

    out = capsys.readouterr().out
    assert "created" in out
    assert "AGENTS.md" in out
    assert CONFIG_NAME in out


def test_running_init_twice_leaves_the_repo_byte_identical(repo):
    """Idempotency is the property that makes this safe to put in a setup
    script or a README: a second run must not append a second block, flip a
    line ending, or touch a single byte anywhere in the repository."""
    main(["init", "--path", str(repo)])
    before = fingerprint(repo)
    assert main(["init", "--path", str(repo)]) == 0
    assert fingerprint(repo) == before
    assert (repo / "AGENTS.md").read_text().count(BEGIN_MARKER) == 1


def test_init_appends_to_an_existing_agents_md_without_touching_its_prose(repo):
    """An AGENTS.md that exists but has no codegraph block gets one
    appended -- and every byte the user wrote survives verbatim, which is
    the difference between a tool you run on a real repo and one you don't."""
    prose = "# AGENTS.md\n\nRun `make test` before pushing.\nUse tabs, we're like that.\n"
    (repo / "AGENTS.md").write_text(prose)

    assert main(["init", "--path", str(repo)]) == 0

    content = (repo / "AGENTS.md").read_text()
    assert content.startswith(prose)
    assert content.count(BEGIN_MARKER) == 1
    assert "codegraph impact" in content


def test_init_updates_its_block_in_place_and_keeps_prose_on_both_sides(repo):
    """A block sitting between two pieces of user prose must be refreshed
    *where it is*, not deleted and re-appended at the end of the file. A
    reader who moved the section under their own preamble should not find it
    relocated on the next run, and the prose that followed it must still
    follow it."""
    stale = (
        "# AGENTS.md\n\nTop prose.\n\n"
        f"{BEGIN_MARKER}\n## codegraph\n\nSomething an older version wrote.\n{END_MARKER}\n"
        "\nBottom prose that must stay at the bottom.\n"
    )
    (repo / "AGENTS.md").write_text(stale)

    assert main(["init", "--path", str(repo)]) == 0

    content = (repo / "AGENTS.md").read_text()
    assert "Something an older version wrote." not in content
    assert content.startswith("# AGENTS.md\n\nTop prose.\n\n")
    assert content.endswith("\nBottom prose that must stay at the bottom.\n")
    assert content.index("codegraph impact") < content.index("Bottom prose")
    assert content.count(BEGIN_MARKER) == 1


def test_init_ignores_marker_text_embedded_in_a_longer_line(repo):
    """Marker matching is anchored to a whole line, never a substring --
    the exact lesson `install_hooks` learned. An AGENTS.md that *documents*
    this convention (mentioning the markers inline) must not have everything
    between those two sentences eaten."""
    prose = (
        "# AGENTS.md\n\n"
        f"codegraph owns the section between `{BEGIN_MARKER}` and\n"
        "IMPORTANT PROJECT RULE THAT MUST SURVIVE\n"
        f"`{END_MARKER}`; edit outside it freely.\n"
    )
    (repo / "AGENTS.md").write_text(prose)

    assert main(["init", "--path", str(repo)]) == 0

    content = (repo / "AGENTS.md").read_text()
    assert "IMPORTANT PROJECT RULE THAT MUST SURVIVE" in content
    assert content.startswith(prose)


def test_init_refuses_an_agents_md_with_a_half_deleted_block(repo, capsys):
    """A begin marker with no matching end is damage -- a hand-edit, or an
    interrupted write. Guessing how much of the surrounding file the missing
    partner would have covered risks deleting the user's own text, so the
    file is left byte-identical and the failure is loud."""
    broken = f"# AGENTS.md\n\n{BEGIN_MARKER}\n## codegraph\n\nhalf a block\n"
    (repo / "AGENTS.md").write_text(broken)

    assert main(["init", "--path", str(repo)]) == 1

    assert (repo / "AGENTS.md").read_text() == broken
    err = capsys.readouterr().err
    assert "malformed" in err.lower()
    assert "Traceback" not in err


def test_init_leaves_a_non_utf8_agents_md_untouched(repo, capsys):
    """A file codegraph cannot decode is a file it cannot safely rewrite.
    Refuse with one line, leave every byte alone -- never a traceback, and
    never a partial write that loses whatever the bytes meant."""
    original = b"# AGENTS.md\n\nlatin-1 caf\xe9\n"
    (repo / "AGENTS.md").write_bytes(original)

    assert main(["init", "--path", str(repo)]) == 1

    assert (repo / "AGENTS.md").read_bytes() == original
    err = capsys.readouterr().err
    assert "UTF-8" in err
    assert "Traceback" not in err


def test_init_keeps_crlf_line_endings_and_stays_idempotent(repo):
    """A repo edited on Windows has CRLF files, and a tool that silently
    rewrites every line ending shows up as a whole-file diff. The block we
    add adopts the file's own endings, so nothing outside it changes and the
    second run is still byte-identical."""
    (repo / "AGENTS.md").write_bytes(b"# AGENTS.md\r\n\r\nWindows prose.\r\n")

    assert main(["init", "--path", str(repo)]) == 0

    raw = (repo / "AGENTS.md").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "a bare LF was introduced into a CRLF file"
    assert raw.startswith(b"# AGENTS.md\r\n\r\nWindows prose.\r\n")

    assert main(["init", "--path", str(repo)]) == 0
    assert (repo / "AGENTS.md").read_bytes() == raw


def test_init_never_overwrites_an_existing_codegraph_toml(repo, capsys):
    """codegraph.toml is hand-written, committed configuration -- the one
    file here whose contents this command could only make worse."""
    original = "ambiguity_limit = 100\n\n[[effect]]\nmatch = \"app.db.*\"\nkind = \"DB_WRITE\"\n"
    (repo / CONFIG_NAME).write_text(original)

    assert main(["init", "--path", str(repo)]) == 0

    assert (repo / CONFIG_NAME).read_text() == original
    assert Config.load(repo).ambiguity_limit == 100

    reported = next(line for line in capsys.readouterr().out.splitlines() if CONFIG_NAME in line)
    assert reported.startswith("unchanged")


def test_init_never_creates_a_claude_md_and_says_why(repo, capsys):
    """Claude Code already has a better channel than AGENTS.md -- the
    plugin's skill, which loads on demand instead of into every session.
    Manufacturing a CLAUDE.md nobody asked for would make every Claude
    session permanently more expensive to buy nothing."""
    assert main(["init", "--path", str(repo)]) == 0

    assert not (repo / "CLAUDE.md").exists()
    out = capsys.readouterr().out
    assert "CLAUDE.md" in out
    assert "plugin" in out


def test_init_bridges_an_existing_claude_md_with_the_agents_import(repo):
    """Claude Code reads CLAUDE.md, not AGENTS.md. If the user has already
    accepted a CLAUDE.md's per-session cost, the documented `@AGENTS.md`
    import is what makes the block we just wrote visible there too."""
    (repo / "CLAUDE.md").write_text("# CLAUDE.md\n\nProject notes.\n")

    assert main(["init", "--path", str(repo)]) == 0

    content = (repo / "CLAUDE.md").read_text()
    assert content.startswith("# CLAUDE.md\n\nProject notes.\n")
    assert "@AGENTS.md" in content


def test_init_does_not_duplicate_an_existing_agents_import(repo):
    """A second `@AGENTS.md` line is the failure that compounds: it would
    grow by one every run. Detection is deliberately generous (any mention
    counts) because declining to add an import the user already has costs
    them one manual line, while duplicating costs them a file that grows
    forever."""
    original = "# CLAUDE.md\n\n@AGENTS.md\n\nProject notes.\n"
    (repo / "CLAUDE.md").write_text(original)

    assert main(["init", "--path", str(repo)]) == 0
    assert (repo / "CLAUDE.md").read_text() == original

    assert main(["init", "--path", str(repo)]) == 0
    assert (repo / "CLAUDE.md").read_text() == original


def test_init_never_writes_into_the_git_directory(repo):
    """`init` must not install hooks as a side effect. Writing into `.git/`
    from a command named "init" is a surprise the user did not consent to;
    that stays behind `install-hooks`, which they have to ask for by name."""
    before = fingerprint(repo / ".git")

    assert main(["init", "--path", str(repo)]) == 0

    assert fingerprint(repo / ".git") == before
    hooks = repo / ".git" / "hooks"
    assert not any("codegraph" in path.read_text(errors="replace") for path in hooks.glob("*"))


def test_init_outside_a_git_repo_still_writes_the_files(tmp_path, capsys):
    """codegraph indexes a plain directory too (the FsTreeSource fallback),
    and AGENTS.md is ordinary markdown -- so a missing `.git` is worth a
    note (a mistyped `--path` is the likelier cause) but not a refusal."""
    assert main(["init", "--path", str(tmp_path)]) == 0

    assert (tmp_path / "AGENTS.md").exists()
    captured = capsys.readouterr()
    assert "not a git repository" in captured.err
    assert "Traceback" not in captured.err


def test_init_on_a_missing_path_fails_with_one_line(tmp_path, capsys):
    assert main(["init", "--path", str(tmp_path / "nope")]) == 1
    err = capsys.readouterr().err
    assert "not a directory" in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory write permissions")
def test_init_on_a_read_only_directory_reports_instead_of_crashing(repo, capsys):
    """A repo checked out read-only (a CI cache, a mounted volume) must
    produce a named, one-line failure per file and a nonzero exit -- not a
    PermissionError traceback, and not a half-written AGENTS.md.

    Both files are named, not just the first: a failure is isolated to the
    file it happened on, so the user learns everything that needs fixing in
    one run rather than one error per re-run.
    """
    repo.chmod(0o555)
    try:
        assert main(["init", "--path", str(repo)]) == 1
        err = capsys.readouterr().err
        assert "Traceback" not in err
        skipped = [line for line in err.splitlines() if line.startswith("skipped")]
        assert [line.split()[1] for line in skipped] == ["AGENTS.md", CONFIG_NAME]
        assert not (repo / "AGENTS.md").exists()
    finally:
        repo.chmod(0o755)


def test_the_agents_block_stays_short_enough_to_load_every_session(repo):
    """This block is priced per turn, not per repo -- every session of every
    AGENTS.md-reading agent pays for it. It earns that by naming the
    commands and the trigger and then deferring to `codegraph guide`; if it
    ever grows into a full workflow, that trade is gone."""
    lines = AGENTS_BLOCK.splitlines()
    assert len(lines) <= 20, f"AGENTS.md block has grown to {len(lines)} lines"
    assert max(len(line) for line in lines) <= 80
    assert "codegraph guide" in AGENTS_BLOCK


def test_the_config_stub_is_inert_until_edited(repo):
    """Every line of the stub is commented out, so dropping it into a repo
    changes no behavior at all: it parses as an empty table, and the config
    it loads is identical to the one loaded with no file present."""
    assert main(["init", "--path", str(repo)]) == 0

    text = (repo / CONFIG_NAME).read_text()
    assert tomllib.loads(text) == {}
    assert Config.load(repo) == Config()


def test_the_config_stub_documents_every_setting_and_effect_kind(repo):
    """The stub is the only configuration reference most people will read.
    A kind missing from it is a kind nobody discovers, so this fails if the
    catalog grows one and the stub is not updated."""
    assert main(["init", "--path", str(repo)]) == 0

    text = (repo / CONFIG_NAME).read_text()
    for setting in ("source_roots", "ambiguity_limit", "[[effect]]"):
        assert setting in text
    for kind in EFFECT_KINDS:
        assert kind in text, f"effect kind {kind} is undocumented in {CONFIG_NAME}"


def test_guide_prints_the_workflow_to_stdout(capsys):
    """The AGENTS.md block buys its brevity by pointing here, so `guide`
    has to actually carry the detail it defers -- the resolve/impact/effects
    workflow and the shared exit-code convention."""
    assert main(["guide"]) == 0

    out = capsys.readouterr().out
    assert out == guide_text()
    for expected in ("codegraph resolve", "codegraph impact", "codegraph effects", "exit-code"):
        assert expected in out
