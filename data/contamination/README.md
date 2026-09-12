# Contamination (leak-detectable) artifact

`leak_detectable.json` maps `instance_id -> [FAIL_TO_PASS node ids absent
at base_commit]` for the active dataset (ADR-0038 §2).  An agent that names
one of these tests in a patch or trajectory knew a test it could not have read.

**Current numbers (SWE-bench Verified, 500 instances, committed 2026-08-21):**
475 instances are in the map; **335 (67%) are leak-detectable** (at least one
FAIL_TO_PASS id absent at base), the rest `[]` are present-at-base, and **25 are
omitted as UNKNOWN**.  The 67% is the exposure the honesty denominator measures —
do not report it as anything lower.

## Three distinct values — empty arrays are meaningful

- **instance → non-empty list**: leak-detectable — those FAIL_TO_PASS node ids
  are absent at base.  `leak_detectable = True`.
- **instance → `[]`**: **checked**, and nothing was absent — the test exists at
  base.  `leak_detectable = False`.  This is NOT "unknown".
- **instance ABSENT from the map**: **UNKNOWN** — we refused to guess.  Two,
  distinct reasons (both `leak_detectable = NULL`):
  1. **Node-id format/prose**: the FAIL_TO_PASS entries are prose descriptions
     (e.g. `"Field instances from abstract models are not equal."`), not test
     ids — ~11 of the 25.
  2. **Named-but-not-added**: the full paths parse but the named tests are not
     among the definitions the test patch ADDS (they existed at base; the patch
     touches them indirectly) — ~14 of the 25.
  In both, guessing would assert "this instance cannot show leakage" (a `[]`)
  and inflate the clean count, so we omit.  The decision is conservative by
  design: an omitted instance drops from the denominator, a wrong `[]` would
  not.

Do not "tidy" empty arrays out of this file: removing a `[]` silently converts
every `False` into `NULL` (unknown), weakening the honesty claim — the same
trap that would make a run look like it never checked.

## Why the map is diff-parse, not a checkout

At `base_commit` the gold test patch is not applied, so a test the patch ADDS
(its `def test_...` / `class Test...` on a `+` line) did not exist in the repo
the agent saw.  Parsing the gold `test_patch` for added test definitions yields
the absent FAIL_TO_PASS ids with no checkout/clone/install (brief §1.2).

**Known limitation (honest-limits):** a *parametrised case* added to a test that
already exists at base (the `def` line is not added, only more
`@pytest.mark.parametrize` entries) is classified `[]` (present) by this pass,
not leak-detectable.  It would need `pytest --collect-only` on the base tree to
catch.  We accept this and state it rather than build 500 environments.

## Generation

```
.venv/bin/python scripts/generate_leak_detectable.py \
    [--dataset princeton-nlp/SWE-bench_Verified] \
    --out data/contamination/leak_detectable.json
```

Loads the dataset with gold (`include_gold=True`) — the map is gold-derived and
must never reach the harness tier (ADR-0038 / P-1).  It is committed, never
recomputed per run.

Until it is populated (i.e. this file is `{}`), the workers record
`leaked_node_ids = NULL` (unknown) — honest, not a fabricated clean result.  A
run in which no instance is leak-detectable cannot claim to be clean; it can
only claim that nothing was detectable.
