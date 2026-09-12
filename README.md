# agent-evals

Any coding-agent harness, any model, measured the same way on SWE-bench Verified. Five harnesses
(Claude Code, Codex CLI, OpenCode, mini-swe-agent, and a two-tool control agent) on one model and
the full 500-instance split land within two points of each other in resolve rate while their
bills differ 1.5×; a model 5 to 10× cheaper per token came out 4.4× dearer per resolved task on the
same harness; an LLM judge that must cite a turn found the rig's own bug before it found any
contamination. Every number on the results site is pinned to a framework commit, an image digest,
a dataset revision and a gateway configuration hash, and can be re-graded from a downloaded
bundle with the official harness.

Results, findings and design: **https://evals.preetdhaliwal.dev** · per-instance explorer:
https://evalsui.preetdhaliwal.dev

What the framework does: hard cost and turn limits enforced outside the agent, through a local
proxy and per-run provider keys; official SWE-bench grading in the official images at pinned
digests; per-call metering through one inference gateway (tokens, cache reads, cost, latency,
paced wait) so harnesses are compared on the same ledger; an evidence-required LLM judge over
every attempt; full provenance on every result; a fresh-account setup validated end to end.

- **Reproduce a published number, no infrastructure:** `docs/SETUP.md` step 0 with
  `published-runs/<run_id>/preds.jsonl` (committed here) or a bundle from the
  [`v1.0-data` release](https://github.com/psdhali/agent-evals/releases/tag/v1.0-data):
  patches, trajectories, per-call ledgers, the official harness's eval reports and test logs,
  every judge pass, per run.
- **Set it up:** `docs/SETUP.md` — from a laptop-only graded instance (`make local-smoke`, no
  AWS, cents) to a full deployment in any AWS account (`make doctor`, `make bootstrap`,
  `make up-persistent`, `make images SUBSET=10`, `make up`, `make down`). `make help` lists
  every target. Measured: 72 minutes from an empty account to run-ready.
- **What a deployment consists of:** `docs/INVENTORY.md`.
- **Why it is built this way:** the Design pages on the project site; the design record itself is
  not in this repository.
- **How it was validated in a fresh account:** `docs/FRESH-ACCOUNT-LOG.md`, 27 findings with
  their fixes.

Benchmarked: SWE-bench Verified via `swebench 5.0.2`. The harness and grading seams are the
places a second benchmark would plug in; the package is still named `swebench_eval` until one does.

License: MIT. Cite: `CITATION.cff`.
