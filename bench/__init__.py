"""Effectiveness benchmark: score the static graph against runtime traces (#35).

Nothing here runs during `pytest -q`. A run needs a clone of a target
repository, a virtualenv per target, and a full execution of that
repository's test suite -- see `bench/run.py` and the README section
"Effectiveness benchmark". The one part the default suite does exercise is
`bench.score`, whose arithmetic and miss classification are pure functions
over sets and are unit-tested in `tests/test_bench_scorer.py`.
"""
