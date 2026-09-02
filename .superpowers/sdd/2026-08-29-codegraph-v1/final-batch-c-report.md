# Final verification — Batch C report

Single commit on `feat/packaging`, on top of `c8a2976` ("fix: CLI coherence, rendering, and docs
from whole-branch review"): `fix: evidence must justify reported confidence` (`8861357`).

Files touched: `src/codegraph/query/effects.py`, `src/codegraph/effects/propagate.py`,
`src/codegraph/effects/builtin.toml`, `tests/test_effects.py`, `tests/test_effect_catalog.py`.

All three findings stem from the one root cause the ledger names: a node can carry multiple
`direct=1` rows for the same `(node_id, kind)` at different confidences and different lines — no
`UNIQUE` constraint on `effects` (`store.py:98-105`), and `detect_direct` writes one row per call
site, so two `open()` calls in one function (one literal-mode, one variable-mode) is ordinary, not
contrived.

## C1 (HIGH) — `_evidence_location` could print evidence that contradicts the reported confidence

`query/effects.py:71-78` selected the printed location with `ORDER BY evidence_line LIMIT 1` and
no confidence filter, so the earliest call site printed regardless of whether *its own* confidence
supported the tier being reported next to it.

Fixed: `_evidence_location` now takes the reported `confidence` as a fourth argument, fetches all
`direct=1` rows for `(rev, direct_node_id, kind)`, discards any whose own `CONFIDENCE_RANK` is
below the target, and picks the earliest line among the survivors. Applied uniformly to both call
shapes — a direct effect on the queried node itself (`cause == node_id`, chain length 1) and the
terminal node of a propagated witness chain (`cause == chain[-1]`) — since the call site in
`effects_report` is a single line (`query/effects.py:58`) feeding both.

**Why `>=` and not exact equality**, verified against the existing suite before settling on it: the
task description says "rows at the reported tier," which reads as exact-match at first glance, but
that breaks two pre-existing tests that must stay green —
`test_cycle_confidence_is_the_bottleneck_out_of_the_asking_node` and
`test_direct_effect_confidence_bounds_the_propagated_confidence` — both of which report a LOW/MEDIUM
confidence for a node whose direct effect's *own* row is strictly HIGH-er (the propagation
bottleneck is the `CALLS` edge, not the direct detection). An exact-match filter would find no row
at all there and print an empty location, regressing the exact "empty location" Critical this
project already shipped once. `>=` is the correct reading: a row whose own confidence is *at least*
the reported tier never contradicts it, which is exactly the "supports" language in the finding.
Ran all four `test_no_effect_row_has_an_empty_witness_or_location` fixtures plus both bottleneck
tests after the change — all green.

**Verified live** with the exact repro from the finding (`/tmp/c1repro`, indexed and queried
through the real CLI, not just the unit test):

```
def load(path, mode, path2):
    open(path, mode)      # line 2: MEDIUM (open!ambiguous)
    return open(path2)    # line 3: HIGH (open)
```

`codegraph effects m.py::load` now prints `FS_READ HIGH via m.py::load` at **`m.py:3`** — the HIGH
call — instead of the previous `m.py:2` (the MEDIUM, ambiguous call).

**New tests**, both seeding different lines at different confidences (the exact gap B0's
`test_effects_report_picks_the_strongest_confidence_for_a_repeated_kind` missed, since it seeds
both duplicate rows at the *same* `evidence_line`):

- `test_evidence_location_picks_a_row_that_supports_the_reported_confidence` — the direct case,
  hand-seeded schema mirroring the live repro above; asserts `location == "m.py:3"`.
- `test_evidence_at_the_end_of_a_propagated_witness_chain_justifies_its_tier` — the propagated
  case the finding explicitly calls out ("make sure this holds for propagated effects too"): `a ->
  b -> c` over two HIGH edges, `c` carries a MEDIUM row at line 5 and a HIGH row at line 9; asserts
  the printed location is `m.py:9`, not the earlier MEDIUM line.

## C2 (MED) — `propagate.py:102` overwrote instead of reducing with `stronger()`

`direct_by_node.setdefault(row["node_id"], {})[row["kind"]] = row["confidence"]` kept whichever
row SQL scan order returned last for a given `(node_id, kind)`, rather than reducing with
`stronger()` the way `witness_path`'s own `direct_confidence` (propagate.py:180-182) already does.

Fixed to reduce with `stronger()`:

```python
bucket = direct_by_node.setdefault(row["node_id"], {})
bucket[row["kind"]] = stronger(bucket.get(row["kind"], row["confidence"]), row["confidence"])
```

This makes `propagate`'s `direct_by_node` reduction byte-identical in form to `witness_path`'s
`direct_confidence` reduction — before the fix the two were divergent (last-write-wins vs.
`stronger()`-max), which is exactly the kind of drift the critical constraint warns about: *the
graph a confidence is derived from must remain identical to the graph the witness BFS traverses.*
After the fix both use the same reduction, so they can't drift apart again.

**New test**, proving order-dependence the way the finding asks (`HIGH`-then-`LOW` vs.
`LOW`-then-`HIGH` on an identical graph):
`test_propagate_reduces_duplicate_direct_rows_with_stronger_not_scan_order` seeds `callee`'s HIGH
NETWORK row *before* its LOW row (last-write-wins would have kept LOW, making `callee` ineligible
at the HIGH tier and silently downgrading `caller`, reached over a HIGH `CALLS` edge, to LOW).
Asserts `caller`'s report is `NETWORK HIGH via ...`.

**Totality re-verified**: `test_no_effect_row_has_an_empty_witness_or_location` (all four fixture
shapes) and both C1 regression tests above (which call `propagate()` and then walk the witness
chain end to end) all pass — the BFS never comes back empty for a triple this fix produces.

## C3 (follow-up) — restore HIGH for the literal `requests` HTTP verbs

Added eight literal builtin rules to `builtin.toml` — `requests.get`, `.post`, `.put`, `.patch`,
`.delete`, `.head`, `.options`, `.request` — each `kind = "NETWORK"` with no `confidence` override,
since a pattern with no wildcard already derives HIGH from `Catalog.match_with_confidence`'s own
specificity rule (`best.prefix_len == pattern_len`). `requests.*` is untouched and stays the MEDIUM
catch-all for the rest of the namespace (`requests.codes`, `requests.utils.*`, `Session()`).

**Verified longest-literal-prefix-wins actually favors the literal rules, not just assumed it**:
read `Catalog._best_match` (`catalog.py:126-137`) — it ranks by `(prefix_len, is_override,
rule.match)` and picks the max, so a literal `requests.get` (`prefix_len = 13`, no wildcard) beats
`requests.*` (`prefix_len = 9`, up to the `*`) on `prefix_len` alone, `is_override` never entering
it. Confirmed live via `Catalog.load(Config()).match_with_confidence(...)`:

```
requests.get      -> ('NETWORK', 'HIGH')
requests.post      -> ('NETWORK', 'HIGH')
requests.put/.patch/.delete/.head/.options/.request -> all ('NETWORK', 'HIGH')
requests.codes      -> ('NETWORK', 'MEDIUM')   # catch-all still applies
requests.utils.quote -> ('NETWORK', 'MEDIUM')  # catch-all still applies
httpx.get           -> ('NETWORK', 'MEDIUM')   # left alone, see below
```

Also re-ran `test_override_beats_builtin_on_longer_prefix` (an override `requests.get -> DB_READ`
must still beat the new builtin `requests.get -> NETWORK` rule, since both have the same
`prefix_len` and the tie breaks on `is_override`): passes unchanged, `catalog.match("requests.get")
== "DB_READ"`.

**One pre-existing test needed a genuine update, not just re-passing**:
`test_partial_prefix_match_is_medium_confidence` asserted `requests.get` was MEDIUM — now false by
design, since C3 is exactly "make `requests.get` HIGH again." Changed its `requests` half to
`requests.codes`, which still exercises the bare-catch-all MEDIUM path (no verb-specific rule
matches it) without contradicting the fix. `boto3.client` (the other half of that test) is
untouched.

**New tests** in `test_effect_catalog.py`: a parametrized test asserting all eight verbs are
`('NETWORK', 'HIGH')`, and `test_requests_verb_literal_beats_requests_namespace_catchall` asserting
both the verb-wins-over-catch-all precedence and that the catch-all still serves
`requests.codes`.

**Left alone, with justification** (per the explicit instruction not to go on a catalog-rewriting
spree): `httpx.*` was not given the same literal-verb treatment. `requests` was singled out by the
finding as "the most common and least ambiguous network call in Python" and specifically
worse-labeled *by this branch's own F3 fix* — a regression this batch is undoing, not a new
improvement. `httpx.*` was never HIGH before F3 (it's always been under the same MEDIUM
namespace-wildcard rule), so leaving it alone doesn't reintroduce any regression, and extending the
same treatment to it, `socket.*`, `subprocess.*`, etc. would be a broader precision/recall change
outside what was asked. `boto3.*` is explicitly still open as deferred finding #1 (S3 reads
mislabeled as writes) — a different, unrelated problem with that catalog entry — and not
something this batch's scope covers.

## Gates (run for real)

- `uv run pytest -q` → **212 passed, 3 deselected** (200 baseline + 12 new: 3 in
  `tests/test_effects.py` — C1 x2, C2 x1 — plus 9 in `tests/test_effect_catalog.py` — the 8
  parametrized HTTP-verb tests plus the precedence test; the 9th file change,
  `test_partial_prefix_match_is_medium_confidence`, is a correction to an existing test, not a
  new one).
- `uv run pytest -m slow -s` → **3 passed**. Precision/recall observed: **precision=1.00,
  recall=0.90** — unchanged from the pre-C3 baseline recorded in the ledger. This makes sense:
  the accuracy fixture (`tests/fixtures/labelled_calls.json`) grades `CALLS`-edge resolution, and
  C3 only changes effect-*confidence* tiering for a catalog pattern, never which calls resolve to
  which node. Did not touch the fixture or the floors; reporting the real number as instructed.
- `uv run ruff check .` → **All checks passed!** (repo-wide, no new suppressions needed).
- `skills/codegraph/SKILL.md` re-checked line by line against the "Reading the output" section:
  its existing claim ("`location` is that call site's `file:line`, clickable evidence rather than
  a claim you have to trust") was already accurate prose — C1's fix makes that claim *true* in the
  duplicate-row case it previously wasn't; no wording changed since nothing false is now being
  asserted. No mention of `requests` or HTTP verbs anywhere in the file, so C3 needed no doc
  update either. Confirmed via `grep`, not by assumption.

## Found but left alone (in scope for triage, not for this batch)

- `httpx.*`, `socket.*`, `boto3.*`, and every other namespace-wildcard catalog entry — see C3's
  "left alone" note above; extending literal-verb treatment to them would be new scope, not part
  of restoring `requests`'s pre-F3 detection.
- `catalog.py`'s module docstring (lines 9-13) illustrates precedence with an example that reads
  "`requests.get` (override, fully literal) beats `requests.*` (built-in...)" — still a correct,
  general illustration of the precedence mechanism, just no longer the only way `requests.get`
  could exist as a literal rule now that it's also a builtin. Left as-is: it's not inaccurate, and
  rewording it is cosmetic, outside this batch's three findings.
- Everything already filed for post-merge in the ledger (deferred findings, F8, F13, F16) is
  untouched — none of it intersects the `effects`/`propagate`/catalog code this batch touched.

---

## Follow-up — flaky perf smoke test in `tests/test_perf.py`

`test_branch_switch_is_under_a_second` was reported flaky: 1 failure in 3 isolated local runs, and
a verification agent hit it failing twice at 1.20s and 1.28s against its `elapsed < 1.0` threshold.
No recent commit touches the indexer path this test exercises, so this reads as scheduler/IO noise
on a shared sandbox, not a regression — the same branch-switch code path is exercised deterministically
by `stats.blobs_parsed == 0`, which never failed.

**What the test protects, and what I changed.** The test carries two assertions with very
different jobs:

- `assert stats.blobs_parsed == 0` — the project's load-bearing cost guarantee: creating a branch
  must re-parse nothing. Deterministic, the real content of the test. **Left untouched.**
- `assert elapsed < 1.0` — a wall-clock smoke check meant to catch an order-of-magnitude
  regression (e.g. branch switch accidentally becoming O(repo size) again), not to referee a
  ~280ms scheduling hiccup on a shared box.

I raised the wall-clock bound from `1.0` to `3.0` seconds and added a comment stating explicitly
that it is an order-of-magnitude guard, not a performance target, so it doesn't get tightened back
down by someone chasing a benchmark number later. `blobs_parsed == 0` is exactly as strict as
before — I am not loosening the guarantee, I am stopping wall-clock noise from being mistaken for
it.

**Rename.** `test_branch_switch_is_under_a_second` no longer described the bound after raising it
to 3.0s, so I renamed it to `test_branch_switch_reparses_nothing` — which also better names what
the test actually verifies (the `blobs_parsed == 0` guarantee is the point; the timing is a
backstop). Checked for other references to the old name: only the test file itself and
`docs/superpowers/plans/2026-08-29-codegraph-v1.md` (a historical planning doc recording the
original plan snippet, not code) mention it. Left the plan doc alone — it's a record of what was
planned, not a live reference, and out of scope for this fix.

**`test_cold_index_and_warm_query_are_fast`** — checked for the same fragility
(`cold < 60.0`, `warm < 0.3`) per instructions, but only to act if there was real evidence. Ran it
7 times in isolation: **7/7 passed** (9.68s-12.62s cold time, well under 60s each run; no warm-query
failures at any run). No evidence of flakiness, so per the "only if you find real evidence"
instruction, I left both bounds in that test unchanged.

### Gates

- `uv run pytest -q` → **212 passed, 3 deselected** (matches expected).
- `uv run pytest -m slow -q`, run **five times**, full results:
  1. `3 passed, 212 deselected in 28.42s`
  2. `3 passed, 212 deselected in 23.01s`
  3. `3 passed, 212 deselected in 26.58s`
  4. `3 passed, 212 deselected in 26.23s`
  5. `3 passed, 212 deselected in 21.58s`

  All five green, no flakiness observed after the fix.
- `uv run ruff check .` → **All checks passed!**

Single commit: `test: stop wall-clock noise from failing the cost-guarantee test`. Not pushed, no
PR opened, per instructions.
