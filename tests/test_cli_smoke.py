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
    )
    assert proc.returncode == 0
