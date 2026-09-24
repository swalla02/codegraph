"""Session pointers (#69): reading `Session:` trailers, and the opt-in hook
that writes them.

The reader is core and must be invisible when unused: a repository with no
trailers, or no git at all, answers with nothing and never an error. The
writer is an add-on: nothing but its own command installs it, and it writes
only when the agent exported a pointer.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest

from codegraph import gitio
from codegraph.cli import main
from codegraph.maintenance import install_hooks
from codegraph.session_hook import install_session_hook, uninstall_session_hook
from codegraph.sessions import session_log, session_pointers, sessions_by_commit
from tests.conftest import git


def _commit(repo, message: str, *extra: str) -> str:
    args = ["commit", "--allow-empty", "-qm", message]
    for paragraph in extra:
        args += ["-m", paragraph]
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD").strip()


def _message(repo, rev: str = "HEAD") -> str:
    return git(repo, "log", "-1", "--format=%B", rev)


# -- reading -----------------------------------------------------------------


def test_a_commit_without_a_trailer_has_no_pointers(repo):
    assert session_pointers(repo, "HEAD") == []


def test_a_trailer_is_read_as_an_opaque_string(repo):
    sha = _commit(repo, "work", "Session: claude-code://session/abc 123?x=1")
    assert session_pointers(repo, sha) == ["claude-code://session/abc 123?x=1"]


def test_several_trailers_come_back_in_order_beside_other_trailers(repo):
    sha = _commit(
        repo,
        "work",
        "Session: first\nCo-Authored-By: Someone <s@example.com>\nsession: second",
    )
    # Git matches trailer keys case-insensitively, so `session:` counts too.
    assert session_pointers(repo, sha) == ["first", "second"]


def test_a_session_line_in_the_body_is_not_a_trailer(repo):
    sha = _commit(repo, "work", "Session: not-a-trailer\nbecause this paragraph", "Done.")
    assert session_pointers(repo, sha) == []


def test_a_directory_that_is_not_a_repository_answers_empty(tmp_path):
    assert session_pointers(tmp_path, "HEAD") == []
    assert session_log(tmp_path) == []
    assert sessions_by_commit(tmp_path, ["HEAD"]) == {}


def test_an_unknown_revision_is_an_error(repo):
    with pytest.raises(gitio.GitError):
        session_pointers(repo, "nosuchrev")


def test_the_batch_reads_exactly_the_commits_named(repo):
    first = _commit(repo, "one", "Session: s1")
    middle = _commit(repo, "two")
    last = _commit(repo, "three", "Session: s3")
    assert sessions_by_commit(repo, [last, first]) == {last: ["s3"], first: ["s1"]}
    assert sessions_by_commit(repo, [middle]) == {middle: []}
    assert sessions_by_commit(repo, []) == {}


def test_the_log_walks_a_range_newest_first(repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    first = _commit(repo, "one", "Session: s1")
    second = _commit(repo, "two")
    records = session_log(repo, f"{base}..HEAD")
    assert [(r.commit, r.sessions) for r in records] == [(second, []), (first, ["s1"])]


# -- the `sessions` command ----------------------------------------------------


def test_sessions_lists_only_commits_that_carry_a_pointer(repo, capsys):
    _commit(repo, "plain")
    sha = _commit(repo, "linked", "Session: s1\nSession: s2")
    assert main(["sessions", "--path", str(repo)]) == 0
    assert capsys.readouterr().out == f"{sha}  s1\n{sha}  s2\n"

    assert main(["sessions", "--path", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"commit": sha, "sessions": ["s1", "s2"]}]


def test_sessions_on_a_repository_with_none_prints_nothing(repo, capsys):
    assert main(["sessions", "--path", str(repo)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_sessions_outside_a_repository_is_not_an_error(tmp_path, capsys):
    assert main(["sessions", "--path", str(tmp_path), "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "not a git repository" in captured.err


def test_sessions_with_a_bad_revspec_reports_cleanly(repo, capsys):
    assert main(["sessions", "nosuchrev", "--path", str(repo)]) == 1
    assert capsys.readouterr().err.strip() == "revision not found: nosuchrev"


def test_trailers_change_no_other_answer(repo, write, capsys):
    """The link is additive: the same tree, committed with and without a
    pointer, answers `impact` identically."""
    write("m.py", "def target():\n    pass\n\n\ndef caller():\n    target()\n", commit="m")
    assert main(["impact", "m.py::target", "--path", str(repo), "--json"]) == 0
    before = capsys.readouterr().out
    git(repo, "commit", "--amend", "-qm", "m", "-m", "Session: s1")
    assert main(["impact", "m.py::target", "--path", str(repo), "--json"]) == 0
    assert capsys.readouterr().out == before


# -- writing: the opt-in hook ------------------------------------------------


@pytest.fixture
def hooked(repo, monkeypatch):
    """A repository with the session hook installed and no pointer exported."""
    monkeypatch.delenv("CODEGRAPH_SESSION", raising=False)
    result = install_session_hook(repo)
    assert result.installed
    return repo


def _hook(repo):
    return repo / ".git" / "hooks" / "prepare-commit-msg"


def test_the_hook_writes_nothing_without_the_variable(hooked):
    sha = _commit(hooked, "plain")
    assert session_pointers(hooked, sha) == []
    assert _message(hooked) == "plain\n\n"


def test_the_hook_writes_nothing_for_an_empty_variable(hooked, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_SESSION", "")
    sha = _commit(hooked, "plain")
    assert session_pointers(hooked, sha) == []


def test_the_hook_writes_the_trailer_when_the_variable_is_set(hooked, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_SESSION", "codex://rollout/42")
    sha = _commit(hooked, "work")
    assert session_pointers(hooked, sha) == ["codex://rollout/42"]


def test_the_hook_composes_with_co_authored_by(hooked, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    _commit(hooked, "work", "Co-Authored-By: Someone <s@example.com>")
    assert _message(hooked).endswith("\n\nCo-Authored-By: Someone <s@example.com>\nSession: s1\n\n")


def test_the_hook_does_not_duplicate_a_pointer_already_there(hooked, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    sha = _commit(hooked, "work", "Session: s1")
    assert session_pointers(hooked, sha) == ["s1"]
    # An amend in the same session re-runs the hook over the same message.
    git(hooked, "commit", "--amend", "--allow-empty", "--no-edit", "-q")
    assert session_pointers(hooked, "HEAD") == ["s1"]


def test_an_amend_from_another_session_adds_the_second_pointer(hooked, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    _commit(hooked, "work")
    monkeypatch.setenv("CODEGRAPH_SESSION", "s2")
    git(hooked, "commit", "--amend", "--allow-empty", "--no-edit", "-q")
    assert session_pointers(hooked, "HEAD") == ["s1", "s2"]


def test_the_hook_leaves_a_merge_message_alone(hooked, monkeypatch, write):
    git(hooked, "checkout", "-qb", "side")
    write("side.py", "x = 1\n", commit="side")
    git(hooked, "checkout", "-q", "main")
    write("main.py", "y = 1\n", commit="main")
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    git(hooked, "merge", "-q", "--no-ff", "--no-edit", "side")
    # The text, not just the parsed trailers: git drafts a one-line merge
    # message, and a line appended to it would not parse as a trailer at all.
    assert "s1" not in _message(hooked)


def test_the_hook_leaves_a_draft_the_editor_will_write_alone(hooked, monkeypatch, tmp_path):
    """No `-m`: the hook runs before the user writes anything, so it keeps
    out, and a commit whose message is written in the editor carries none."""
    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\nprintf "typed in the editor\\n" > "$1"\n')
    editor.chmod(editor.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GIT_EDITOR", str(editor))
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    git(hooked, "commit", "--allow-empty", "-q")
    assert _message(hooked) == "typed in the editor\n\n"


def test_quitting_the_editor_still_aborts_the_commit(hooked, monkeypatch):
    """The reason the editor draft is left alone: a trailer in it would make
    an unwritten message non-empty, and git would commit it."""
    monkeypatch.setenv("GIT_EDITOR", "true")
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    before = git(hooked, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        git(hooked, "commit", "--allow-empty", "-q")
    assert git(hooked, "rev-parse", "HEAD") == before


def test_the_hook_refuses_a_multi_line_value(hooked, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1\nSigned-off-by: forged")
    _commit(hooked, "work")
    assert "forged" not in _message(hooked)


def test_installing_twice_leaves_one_block(hooked):
    first = _hook(hooked).read_text()
    install_session_hook(hooked)
    assert _hook(hooked).read_text() == first
    assert first.count("interpret-trailers") == 1


def test_installing_keeps_an_existing_prepare_commit_msg_hook(repo, monkeypatch):
    path = _hook(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "# from the user hook" >> "$1"\nexit 0\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    assert install_session_hook(repo).installed
    content = path.read_text()
    assert 'echo "# from the user hook" >> "$1"' in content
    # Spliced before the user's `exit 0`, which would otherwise skip it.
    assert content.index("interpret-trailers") < content.index("from the user hook")

    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    sha = _commit(repo, "work")
    assert session_pointers(repo, sha) == ["s1"]


def test_installing_skips_a_non_shell_hook_untouched(repo):
    path = _hook(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/usr/bin/env python3\nprint('hi')\n"
    path.write_text(original)
    result = install_session_hook(repo)
    assert not result.installed
    assert "session trailer not installed" in result.reason
    assert path.read_text() == original


def test_uninstall_removes_a_hook_it_created(hooked):
    removal = uninstall_session_hook(hooked)
    assert removal.removed
    assert not _hook(hooked).exists()
    assert not uninstall_session_hook(hooked).removed


def test_uninstall_restores_an_existing_hook_byte_for_byte(repo):
    path = _hook(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '#!/bin/sh\necho hi >> "$1"\n'
    path.write_text(original)
    install_session_hook(repo)
    assert uninstall_session_hook(repo).removed
    assert path.read_text() == original


def test_the_two_installers_never_touch_each_others_blocks(repo):
    """Warming and the session trailer have separate markers. The default
    `install-hooks` must not install, repair or strip the trailer, and the
    trailer's install and uninstall must not touch a warming block."""
    install_hooks(repo)
    warmed = {p.name: p.read_text() for p in (repo / ".git" / "hooks").iterdir()}
    assert "prepare-commit-msg" not in warmed

    install_session_hook(repo)
    trailer = _hook(repo).read_text()
    install_hooks(repo)
    assert _hook(repo).read_text() == trailer
    uninstall_session_hook(repo)
    after = {p.name: p.read_text() for p in (repo / ".git" / "hooks").iterdir()}
    assert after == warmed


def test_default_install_hooks_writes_nothing_into_commits(repo, monkeypatch):
    """`install-hooks` stays a pure warming optimization: with the variable
    set, a commit made under its hooks carries no trailer."""
    install_hooks(repo)
    monkeypatch.setenv("CODEGRAPH_SESSION", "s1")
    sha = _commit(repo, "work")
    assert session_pointers(repo, sha) == []
    assert not _hook(repo).exists()


# -- the `install-session-hook` command ---------------------------------------


def test_install_session_hook_command_says_it_writes_into_commits(repo, capsys):
    assert main(["install-session-hook", "--path", str(repo)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == str(_hook(repo))
    assert "writes into commit messages" in captured.err
    assert os.stat(_hook(repo)).st_mode & stat.S_IXUSR

    assert main(["install-session-hook", "--path", str(repo), "--uninstall"]) == 0
    assert "removed" in capsys.readouterr().out
    assert not _hook(repo).exists()


def test_install_session_hook_outside_a_repository_reports_cleanly(tmp_path, capsys):
    assert main(["install-session-hook", "--path", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "not a git repository" in err
    assert "Traceback" not in err
