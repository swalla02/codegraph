import json

from codegraph.render import Group, Report, Row, budget, render_json, render_text


def sample():
    return Report(
        summary={"symbols": 47, "modules": 12, "entry_points": 9, "low_confidence_hidden": 31},
        groups=[
            Group("hop 1", [Row("a.py::one", "a.py:10", "HIGH", 9.0)]),
            Group("tests", [Row("tests/test_a.py::test_one", "tests/test_a.py:3", "HIGH", 1.0)]),
        ],
        truncated=True,
    )


def test_budget_truncates_and_flags():
    rows = [Row(f"m.py::f{i}", "m.py:1", "HIGH", float(i)) for i in range(10)]
    kept, truncated = budget(rows, 4)
    assert len(kept) == 4
    assert truncated is True


def test_budget_keeps_highest_scores_first():
    rows = [Row(f"m.py::f{i}", "m.py:1", "HIGH", float(i)) for i in range(5)]
    kept, _ = budget(rows, 2)
    assert [r.score for r in kept] == [4.0, 3.0]


def test_text_leads_with_summary():
    text = render_text(sample())
    first = text.splitlines()[0]
    assert "47" in first and "12" in first


def test_text_keeps_tests_in_their_own_group():
    text = render_text(sample())
    assert "tests" in text
    assert text.index("hop 1") < text.index("tests")


def test_json_is_machine_readable_and_flags_truncation():
    payload = json.loads(render_json(sample()))
    assert payload["truncated"] is True
    assert payload["summary"]["symbols"] == 47
    assert payload["groups"][0]["rows"][0]["id"] == "a.py::one"
