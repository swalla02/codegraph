"""Resolution accuracy harness.

The ground truth in `tests/fixtures/labelled_calls.json` is hand-derived by
reasoning (and, where noted in the task-15 report, by executing a throwaway
script) about what these modules actually do at runtime -- it is never
produced by running codegraph and recording its own output. Recall is the
assertion that matters: the resolver's design deliberately over-approximates
(a candidate is never dropped to improve precision), so precision is floored
low and recall is floored high. Do not "fix" a low score by loosening a
label; a failing floor here is a real finding about the resolver.
"""

import json
from pathlib import Path

import pytest

from codegraph.ambiguity import Ambiguity
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import Store

LABELS = Path(__file__).parent / "fixtures" / "labelled_calls.json"


def measure_accuracy(store, rev, labels):
    """labels: [{"src": node_id, "expected": [node_id, ...]}]

    Measures the resolver's ANSWER, which since #25 is not all in `edges`.

    A reference whose candidates are an all-LOW fan-out is deliberately not
    materialized -- it is recorded once in `unresolved` and expanded at query
    time, which is what `impact` and `effects` actually read. Scoring `edges`
    alone would have graded the storage layer rather than the resolver, and it
    showed: recall read 0.86 while the two `item.save()` targets it was
    supposedly missing were both being returned by every real query.

    No label was touched to fix that number. The union below is the same one
    `query/impact.py` performs, so this harness and the commands it stands in
    for read the same graph -- which is the property that keeps the score
    meaningful at all.
    """
    ambiguity = Ambiguity(store, rev)
    true_positive = predicted = actual = 0
    for label in labels:
        got = {
            row["dst"]
            for row in store.connection.execute(
                "SELECT dst FROM edges WHERE rev=? AND src=? AND kind='CALLS'",
                (rev, label["src"]),
            )
        }
        for row in store.connection.execute(
            "SELECT raw_name FROM unresolved WHERE rev=? AND src=? AND reason='ambiguous'"
            " AND ref_kind='call'",
            (rev, label["src"]),
        ):
            got.update(ambiguity.candidates(row["raw_name"]))
        expected = set(label["expected"])
        true_positive += len(got & expected)
        predicted += len(got)
        actual += len(expected)
    precision = true_positive / predicted if predicted else 1.0
    recall = true_positive / actual if actual else 1.0
    return precision, recall


@pytest.mark.slow
def test_resolution_accuracy_meets_floor(repo, write):
    labels = json.loads(LABELS.read_text())
    for name, source in labels["files"].items():
        write(name, source)
    from tests.conftest import git

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "accuracy fixture")

    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    precision, recall = measure_accuracy(store, "HEAD", labels["calls"])
    print(f"precision={precision:.2f} recall={recall:.2f}")
    assert recall >= 0.90, "over-approximation is the design bias; recall must stay high"
    assert precision >= 0.60
    store.close()
