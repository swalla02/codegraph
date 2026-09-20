"""A regression guard over the resolution rules, on a fixture (#39).

**This file does not measure how well codegraph does on real code, and the
1.00 recall it reports is not a claim that it does.** The fixture is a synthetic
package of hand-written call sites, every one of them authored to exercise a
rule the resolver implements: an aliased import, a relative import, `self.x()`
inside a class, an inherited method, a unique duck-typed name, an ambiguous
one, a constructor, a package re-export, an annotated receiver. It contains no
dunder invoked by syntax, no decorated target and no closure -- which are
between them the majority of what a real test suite executes. So it can fail
for one reason only: one of those rules stopped working. That is worth
guarding and is all this guards.

The number that says how well the resolver does on real code comes from
`bench/`, which traces a target repository's own test suite under
`sys.monitoring` and scores the static graph against what actually ran (#35).
It reads 0.79 recall on requests and 0.29 on flask, an order of magnitude
apart, and those are the floors `python -m bench.run <target> --check-floors`
enforces.

The ground truth in `tests/fixtures/labelled_calls.json` is hand-derived by
reasoning (and, where noted in the task-15 report, by executing a throwaway
script) about what these modules actually do at runtime -- it is never
produced by running codegraph and recording its own output. Recall is the
assertion that matters: the resolver's design deliberately over-approximates
(a candidate is never dropped to improve precision), so precision is held to a
lower bar than recall. Do not "fix" a failure by loosening a label; a label
that no longer holds is either a resolver regression or a deliberate change of
behaviour, and both deserve to be read rather than edited away.
"""

import json
from pathlib import Path

import pytest

from codegraph.ambiguity import Ambiguity
from codegraph.indexer import GitTreeSource, Indexer
from codegraph.store import Store

LABELS = Path(__file__).parent / "fixtures" / "labelled_calls.json"


def measure_labelled_rules(store, rev, labels):
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
def test_labelled_resolution_rules_have_not_regressed(repo, write):
    """Every rule the fixture demonstrates still resolves the way it did.

    The two thresholds are regression thresholds over 15 hand-written call
    sites, not effectiveness figures: the fixture was written to be resolvable,
    and it reads recall 1.00 with precision 0.91 -- the 2 false positives are
    one bare-name `save()` fan-out reaching a third class that also defines
    `save`, which is the over-approximation working as designed. A drop in
    recall means a rule the fixture demonstrates has stopped working; a drop in
    precision means a fan-out widened. See `bench/run.py --check-floors` for
    the effectiveness floors, which are per target repository and much lower.
    """
    labels = json.loads(LABELS.read_text())
    for name, source in labels["files"].items():
        write(name, source)
    from tests.conftest import git

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "resolution-rule fixture")

    store = Store.open(repo)
    Indexer(repo, store, GitTreeSource(repo)).reconcile("HEAD")
    precision, recall = measure_labelled_rules(store, "HEAD", labels["calls"])
    print(f"precision={precision:.2f} recall={recall:.2f}")
    assert recall >= 0.90, (
        "a labelled call site the resolver used to reach is no longer reached:"
        " a rule this fixture guards has regressed. This is not a measure of"
        " effectiveness on real code -- see bench/run.py --check-floors for that."
    )
    assert precision >= 0.60, (
        "the resolver returned materially more candidates than the labels expect,"
        " so a fan-out widened. Over-approximation is the design bias, which is"
        " why this bar is lower than recall's, but not by an unbounded amount."
    )
    store.close()
