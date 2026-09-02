import os
import stat
import time

from codegraph.cli import main
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.maintenance import gc, install_hooks, plan_hooks
from codegraph.query.impact import impact_report
from codegraph.resolve import find_symbol
from codegraph.store import Store
from tests.conftest import git


def test_gc_removes_unreferenced_blobs(repo, write):
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")
    write("a.py", "def alpha():\n    return 2\n", commit="edit")
    indexer.reconcile("HEAD")
    before = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    removed = gc(store, {"HEAD"})
    after = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert removed >= 1
    assert after < before
    store.close()


def test_gc_keeps_blobs_of_retained_revisions(repo):
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    gc(store, {"HEAD"})
    rows = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert rows == 1
    store.close()


def test_install_hooks_writes_executable_hooks(repo):
    written = install_hooks(repo)
    assert {p.name for p in written} == {"post-commit", "post-checkout", "post-merge"}
    for path in written:
        assert os.stat(path).st_mode & stat.S_IXUSR
        assert "codegraph index" in path.read_text()


def test_hooks_do_not_block_the_git_operation(repo, write):
    install_hooks(repo)
    write("c.py", "def gamma():\n    pass\n", commit="with hooks installed")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


# --- Extra tests per the brief -------------------------------------------


def test_install_hooks_preserves_pre_existing_hook(repo):
    """A repo may already have a post-commit hook (e.g. a linter). Installing
    codegraph's hooks must not clobber it -- the existing script's own
    behavior (here, appending to a marker file) must still run, and its
    original source text must still be present in the file verbatim."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    original = "#!/bin/sh\necho ran >> marker.txt\n"
    existing_hook.write_text(original)
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    content = existing_hook.read_text()
    assert "echo ran >> marker.txt" in content
    assert "codegraph index" in content
    assert existing_hook in written

    # The pre-existing behavior must still actually fire.
    git(repo, "commit", "--allow-empty", "-qm", "trigger hooks")
    assert (repo / "marker.txt").read_text() == "ran\n"


def test_install_hooks_fires_even_after_a_pre_existing_exit(repo, monkeypatch, tmp_path):
    """A pre-existing hook that ends in `exit 0` -- a very common idiom -- is
    exactly the case that was silently broken: code appended after that
    `exit` is unreachable, so the warming block never ran even though
    install_hooks reported success. It must fire regardless, by running
    before the pre-existing script's own logic rather than after it."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    existing_hook = hooks_dir / "post-commit"
    existing_hook.write_text("#!/bin/sh\necho hi >> marker.txt\nexit 0\n")
    existing_hook.chmod(existing_hook.stat().st_mode | stat.S_IXUSR)

    # A `codegraph` shim on PATH that just logs that it was invoked.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "invoked.log"
    shim = shim_dir / "codegraph"
    shim.write_text(f'#!/bin/sh\necho invoked >> "{log}"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    install_hooks(repo)
    content = existing_hook.read_text()
    # The codegraph block must precede the pre-existing `exit 0`, not follow it.
    assert content.index("codegraph index") < content.index("exit 0")

    git(repo, "commit", "--allow-empty", "-qm", "trigger hooks despite exit 0")

    # The backgrounded warm-up may still be running when `git commit`
    # returns; give it a short window to land before asserting it fired.
    deadline = time.monotonic() + 2.0
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert log.exists(), "codegraph shim was never invoked -- warming block was skipped"
    # The pre-existing hook's own behavior still ran too.
    assert (repo / "marker.txt").read_text() == "hi\n"


def test_install_hooks_is_idempotent(repo):
    """Running install_hooks twice must not double-append the codegraph
    block or produce two invocations of `codegraph index`."""
    install_hooks(repo)
    first = (repo / ".git" / "hooks" / "post-commit").read_text()
    written_again = install_hooks(repo)
    second = (repo / ".git" / "hooks" / "post-commit").read_text()

    assert first == second
    assert second.count("codegraph index") == 1
    assert {p.name for p in written_again} == {"post-commit", "post-checkout", "post-merge"}


# --- Interpreter allowlist and stale-install repair (2nd review round) ---


def test_install_hooks_skips_perl_hook_untouched(repo):
    """Splicing shell syntax into a hook written for a different
    interpreter corrupts it outright, not just fails to warm it. A Perl
    hook must be left byte-for-byte alone and absent from the returned
    (installed) list."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    perl_hook = hooks_dir / "post-commit"
    original = '#!/usr/bin/perl\nprint "perl-hook-ran\\n";\n'
    perl_hook.write_text(original)
    perl_hook.chmod(perl_hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    assert perl_hook not in written
    assert perl_hook.read_text() == original


def test_install_hooks_skips_env_python_hook_untouched(repo):
    """`#!/usr/bin/env python3` is exactly as unsafe to splice into as a
    direct `#!/usr/bin/perl` -- the `env` indirection must still resolve to
    the real interpreter for the allowlist check."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    python_hook = hooks_dir / "post-checkout"
    original = "#!/usr/bin/env python3\nprint('python-hook-ran')\n"
    python_hook.write_text(original)
    python_hook.chmod(python_hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    assert python_hook not in written
    assert python_hook.read_text() == original


def test_install_hooks_reports_skip_reason_on_cli(repo, capsys):
    """A skip must be loud, not silent: the CLI names the skipped hook and
    says why, on stderr, distinct from the installed paths on stdout."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    perl_hook = hooks_dir / "post-commit"
    perl_hook.write_text('#!/usr/bin/perl\nprint "hi\\n";\n')
    perl_hook.chmod(perl_hook.stat().st_mode | stat.S_IXUSR)

    assert main(["install-hooks", "--path", str(repo)]) == 0

    captured = capsys.readouterr()
    assert "post-commit" not in captured.out
    assert "post-checkout" in captured.out
    assert "post-merge" in captured.out
    assert "skipped post-commit" in captured.err
    assert "perl" in captured.err.lower()


def test_install_hooks_still_installs_allowlisted_shell_shapes(repo):
    """`#!/bin/bash -e`, `#!/usr/bin/env bash`, and a fully absent shebang
    all name (or don't contradict) a POSIX-shell-compatible interpreter and
    must still install normally."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    bash_flag_hook = hooks_dir / "post-commit"
    bash_flag_hook.write_text("#!/bin/bash -e\necho bash-ran\n")
    bash_flag_hook.chmod(bash_flag_hook.stat().st_mode | stat.S_IXUSR)

    env_bash_hook = hooks_dir / "post-checkout"
    env_bash_hook.write_text("#!/usr/bin/env bash\necho env-bash-ran\n")
    env_bash_hook.chmod(env_bash_hook.stat().st_mode | stat.S_IXUSR)

    # post-merge: no pre-existing file at all.

    written = install_hooks(repo)

    assert {p.name for p in written} == {"post-commit", "post-checkout", "post-merge"}
    assert "codegraph index" in bash_flag_hook.read_text()
    assert bash_flag_hook.read_text().startswith("#!/bin/bash -e\n")
    assert "codegraph index" in env_bash_hook.read_text()
    assert env_bash_hook.read_text().startswith("#!/usr/bin/env bash\n")


def test_install_hooks_repairs_a_stale_pre_fix_install(repo):
    """A hook carrying the OLD append-at-end block (with `$?` preservation,
    installed after the pre-existing hook's own `exit 0`) must not be left
    byte-identical -- it was silently dead. install_hooks repairs it into a
    live, top-of-file block."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    stale_hook = hooks_dir / "post-commit"
    stale_hook.write_text(
        "#!/bin/sh\n"
        "echo hi >> marker.txt\n"
        "exit 0\n"
        "# >>> codegraph (warming only; safe to remove) >>>\n"
        "_codegraph_status=$?\n"
        '( cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'
        ' && codegraph index --quiet >/dev/null 2>&1 & ) >/dev/null 2>&1 || true\n'
        "exit $_codegraph_status\n"
        "# <<< codegraph (warming only; safe to remove) <<<\n"
    )
    stale_hook.chmod(stale_hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    content = stale_hook.read_text()
    assert stale_hook in written
    assert "_codegraph_status" not in content  # old dead artifact is gone
    assert content.count("codegraph index") == 1
    assert content.index("codegraph index") < content.index("exit 0")
    assert "echo hi >> marker.txt" in content


def test_install_hooks_three_reruns_stay_at_one_invocation(repo):
    """Repair and idempotency share one mechanism (strip-then-insert), so
    repeated calls -- even against a hook that keeps ending in its own
    `exit 0` -- must converge, not accumulate."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    hook.write_text("#!/bin/sh\necho hi >> marker.txt\nexit 0\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    install_hooks(repo)
    first = hook.read_text()
    install_hooks(repo)
    second = hook.read_text()
    install_hooks(repo)
    third = hook.read_text()

    assert first == second == third
    assert third.count("codegraph index") == 1
    assert third.count("# >>> codegraph") == 1


# --- Binary hooks, env -S, multi-block convergence, marker anchoring,
# --- and half-marker refusal (3rd review round) --------------------------


def test_install_hooks_skips_binary_hook_but_installs_the_others(repo):
    """A compiled/binary post-commit (pre-commit frameworks and compiled
    shims ship these) must not crash the whole command -- it's a non-shell
    hook, refused the same as Perl, and the other two hooks still install."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    binary_hook = hooks_dir / "post-commit"
    original = bytes([0x7F, 0x45, 0x4C, 0x46, 0x02, 0x01, 0x01, 0x00, 0xFF, 0xFE, 0x00, 0x01])
    binary_hook.write_bytes(original)
    binary_hook.chmod(binary_hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    assert binary_hook not in written
    assert {p.name for p in written} == {"post-checkout", "post-merge"}
    assert binary_hook.read_bytes() == original


def test_install_hooks_resolves_env_dash_capital_s_flag(repo):
    """`#!/usr/bin/env -S bash -e` names bash, not `-S` -- env's own flags
    must be skipped when hunting for the interpreter token."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    hook.write_text("#!/usr/bin/env -S bash -e\necho bash-with-env-S-ran\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    assert hook in written
    content = hook.read_text()
    assert content.startswith("#!/usr/bin/env -S bash -e\n")
    assert "codegraph index" in content
    assert "echo bash-with-env-S-ran" in content


def test_install_hooks_converges_a_leading_and_trailing_double_block(repo):
    """A file carrying two codegraph blocks (a current one spliced at the
    top, plus a stale one still sitting at the end from an even older
    install) must converge to exactly one block, not one-fewer."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "# >>> codegraph (warming only; safe to remove) >>>\n"
        '( cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'
        " && codegraph index --quiet >/dev/null 2>&1 & ) >/dev/null 2>&1 || true\n"
        "# <<< codegraph (warming only; safe to remove) <<<\n"
        "echo real-work\n"
        "# >>> codegraph (warming only; safe to remove) >>>\n"
        "_codegraph_status=$?\n"
        '( cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"'
        " && codegraph index --quiet >/dev/null 2>&1 & ) >/dev/null 2>&1 || true\n"
        "exit $_codegraph_status\n"
        "# <<< codegraph (warming only; safe to remove) <<<\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    content = hook.read_text()
    assert hook in written
    assert content.count("# >>> codegraph (warming only; safe to remove) >>>") == 1
    assert content.count("# <<< codegraph (warming only; safe to remove) <<<") == 1
    assert content.count("codegraph index") == 1
    assert "_codegraph_status" not in content
    assert "echo real-work" in content


def test_install_hooks_ignores_marker_text_embedded_in_other_lines(repo):
    """Marker matching must be anchored to a whole line, not a substring
    search -- a hook whose own comment or echoed string happens to contain
    the marker text must keep every real statement around it."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "do_important_setup_step\n"
        'echo "reminder: # >>> codegraph (warming only; safe to remove) >>> not a real marker"\n'
        "do_important_setup_step\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    content = hook.read_text()
    assert hook in written
    assert content.count("do_important_setup_step") == 2
    assert "not a real marker" in content
    assert "codegraph index" in content


def test_install_hooks_skips_half_marker_left_byte_identical(repo):
    """A begin marker with no matching end is damage from some previous
    mishap, not a shape to guess a repair for -- it must be skipped
    loudly and left completely untouched, not silently grown a second
    (also broken) block on the next run."""
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-commit"
    original = (
        "#!/bin/sh\n"
        "# >>> codegraph (warming only; safe to remove) >>>\n"
        "echo oops-truncated-mid-block\n"
    )
    hook.write_text(original)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    written = install_hooks(repo)

    assert hook not in written
    assert hook.read_text() == original

    results = plan_hooks(repo)
    result = next(r for r in results if r.name == "post-commit")
    assert not result.installed
    assert "malformed" in result.reason.lower()


def test_gc_does_not_corrupt_a_live_graph(repo, write):
    """After gc(store, {"HEAD"}), a query at HEAD must still return the same
    answer it did before the gc: Layer 1 rows for retained revisions, and
    everything Layer 2 needs, must survive."""
    write(
        "b.py",
        "from a import alpha\n\ndef beta():\n    return alpha()\n",
        commit="add beta",
    )
    store = Store.open(repo)
    indexer = Indexer(repo, store, GitTreeSource(repo))
    indexer.reconcile("HEAD")

    node_id = find_symbol(store, "HEAD", "alpha")[0]["id"]
    before = impact_report(store, "HEAD", node_id)

    gc(store, {"HEAD"})

    after = impact_report(store, "HEAD", node_id)
    assert after == before
    assert len(before.groups) > 0
    store.close()


def test_gc_with_empty_keep_revs_removes_everything(repo):
    """An empty keep_revs retains nothing: every Layer 1 row is unreferenced
    by definition, so gc removes it all. This is the defined behavior for
    the empty case, not an accident of the query."""
    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    before = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert before > 0

    removed = gc(store, set())

    after = store.connection.execute("SELECT COUNT(*) c FROM blobs").fetchone()["c"]
    assert removed == before
    assert after == 0
    store.close()
