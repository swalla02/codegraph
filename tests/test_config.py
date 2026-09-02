import pytest

from codegraph.cli import main
from codegraph.config import DEFAULT_AMBIGUITY_LIMIT, Config


def test_defaults_apply_when_there_is_no_config_file(tmp_path):
    config = Config.load(tmp_path)
    assert config.source_roots == ("", "src")
    assert config.ambiguity_limit == DEFAULT_AMBIGUITY_LIMIT
    assert config.effect_overrides == ()


def test_ambiguity_limit_is_read_from_the_config_file(tmp_path):
    (tmp_path / "codegraph.toml").write_text("ambiguity_limit = 100\n")
    assert Config.load(tmp_path).ambiguity_limit == 100


def test_ambiguity_limit_zero_is_accepted_as_no_limit(tmp_path):
    (tmp_path / "codegraph.toml").write_text("ambiguity_limit = 0\n")
    assert Config.load(tmp_path).ambiguity_limit == 0


@pytest.mark.parametrize("value", ["-1", '"25"', "2.5", "true"])
def test_a_nonsense_ambiguity_limit_is_rejected_rather_than_coerced(tmp_path, value):
    """`true` is in here on purpose: `isinstance(True, int)` is True in Python,
    so a bool would otherwise silently configure a limit of 1."""
    (tmp_path / "codegraph.toml").write_text(f"ambiguity_limit = {value}\n")
    with pytest.raises(ValueError, match="ambiguity_limit"):
        Config.load(tmp_path)


def test_a_bad_config_fails_the_command_with_one_line_not_a_traceback(repo, write, capsys):
    write("codegraph.toml", "ambiguity_limit = -3\n", commit="cfg")
    assert main(["status", "--path", str(repo)]) != 0
    err = capsys.readouterr().err
    assert "ambiguity_limit" in err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1
