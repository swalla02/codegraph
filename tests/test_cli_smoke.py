import subprocess
import sys

from codegraph import __version__
from codegraph.cli import main


def test_version_constant_is_set():
    assert __version__


def test_main_version_returns_zero(capsys):
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_console_script_runs():
    proc = subprocess.run(
        [sys.executable, "-m", "codegraph", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


# -- B2: a bad --rev must fail with a clear one-line message, never a raw
# traceback -- matching the shape `diff` already had ("revision not
# found: X", exit 1). Covers every command that reconciles a revision.


def test_status_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["status", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "revision not found: nosuchrev"


def test_index_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["index", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "revision not found: nosuchrev"


def test_resolve_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["resolve", "alpha", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "revision not found: nosuchrev"


def test_effects_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["effects", "alpha", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "revision not found: nosuchrev"


def test_impact_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["impact", "alpha", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "revision not found: nosuchrev"


def test_islands_with_bad_rev_reports_cleanly(repo, capsys):
    assert main(["islands", "--path", str(repo), "--rev", "nosuchrev"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "revision not found: nosuchrev"


def test_install_hooks_outside_git_repo_reports_cleanly(tmp_path, capsys):
    assert main(["install-hooks", "--path", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "not a git repository" in err
    assert "Traceback" not in err


# -- B4: `--hops 0` (or negative) must be rejected, not silently answered
# as an empty (and misleadingly confident) report.


def test_impact_rejects_hops_zero(repo, write, capsys):
    write("m.py", "def target():\n    pass\n", commit="m")
    assert main(["impact", "m.py::target", "--path", str(repo), "--hops", "0"]) == 1
    err = capsys.readouterr().err
    assert "--hops" in err


def test_impact_rejects_negative_hops(repo, write, capsys):
    write("m.py", "def target():\n    pass\n", commit="m")
    assert main(["impact", "m.py::target", "--path", str(repo), "--hops", "-1"]) == 1
    err = capsys.readouterr().err
    assert "--hops" in err


# -- #46: the summary reports the deferred fan-out at both of its sizes. The
# `ambiguous:` count is reference SITES, one row per occurrence in the source;
# the line under it is the distinct (source, name) relationships those sites
# amount to -- calls and base classes both, repeats collapsed -- which is the
# unit `impact` ranks by. On django the two differ by a third, so the second
# is not derivable from the first by a reader.


def test_index_summary_collapses_repeated_ambiguous_calls(repo, write, capsys):
    write("a.py", "class One:\n    def save(self):\n        return 1\n")
    write("b.py", "class Two:\n    def save(self):\n        return 2\n")
    write("c.py", "def caller(item):\n    item.save()\n    item.save()\n", commit="ambiguous")
    assert main(["index", "--path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "ambiguous: 2 bare-name reference(s)" in out
    assert "1 distinct (source, name) relationship(s)" in out


def test_index_summary_counts_an_ambiguous_base_as_a_relationship(repo, write, capsys):
    write("a.py", "class Widget:\n    def save(self):\n        return 1\n")
    write("pkg/b.py", "class Widget:\n    def save(self):\n        return 2\n")
    write(
        "c.py",
        "def caller(item):\n    item.save()\n\n\nclass Sub(Widget):\n    pass\n",
        commit="ambiguous",
    )
    assert main(["index", "--path", str(repo)]) == 0
    out = capsys.readouterr().out
    # One call and one `class Sub(Widget)` base, both ambiguous. Since #42
    # `impact` lists `Sub` as a dependent of either `Widget`, so the base is a
    # relationship the report ranks, exactly as the call is.
    assert "ambiguous: 2 bare-name reference(s)" in out
    assert "2 distinct (source, name) relationship(s)" in out
