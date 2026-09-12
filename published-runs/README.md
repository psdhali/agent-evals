# Published runs

One directory per run published on https://evals.preetdhaliwal.dev, named by run id. Each holds
the run's `preds.jsonl`: the predictions in SWE-bench format, one row per instance, with the
agent's patch exactly as the harness produced it (an empty `model_patch` means the agent
produced no patch). Extra fields (`run_id`, `attempt`, `verdict`) are ours and are ignored by the
official harness.

Re-grade a run with no infrastructure (step 0 of `docs/SETUP.md`):

```bash
uv run python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Verified --split test \
  --predictions_path published-runs/<run_id>/preds.jsonl --run_id verify --max_workers 4
```

The full per-attempt artifacts of every run (trajectories, per-call ledgers, the official
harness's eval reports and test logs, every judge pass) are release assets, one bundle per run,
under the `v1.0-data` release of this repository; the site's Downloads page lists them with sizes
and checksums.

| run id | harness | model | instances | resolved |
|---|---|---|---|---|
| 01788723779415700648-b4338f67 | mini-swe-agent | MiniMax M2.5 | 500 | 379 |
| 01788739268680819707-05bd05b8 | custom minimal agent | MiniMax M2.5 | 500 | 384 |
| 01788744084885192755-c10df654 | Codex | MiniMax M2.5 | 500 | 383 |
| 01788799888585639836-1f722383 | Claude Code | MiniMax M2.5 | 500 | 384 |
| 01788838711744541446-020169d5 | OpenCode | MiniMax M2.5 | 500 | 388 |
| 01789087148400722169-0c9303b3 | OpenCode (re-run after the search-tool fix) | MiniMax M2.5 | 500 | 385 |
| 01788910552328915713-21ebff89 | OpenCode | Laguna xs 2.1 | 78 | 58 |
| 01788984707065955750-e95ccde6 | OpenCode, efficiency prompt | Laguna xs 2.1 | 78 | 55 |
| 01788991399526532442-883d2571 | OpenCode | MiniMax M2.5 | 78 | 59 |
| 01788993822253232992-39ff3720 | OpenCode, efficiency prompt | MiniMax M2.5 | 78 | 60 |
| 01788995535869021545-f8f44aeb | mini-swe-agent | MiniMax M2.5 | 78 | 58 |
| 01788997363735762375-c40be1f7 | mini-swe-agent | Laguna xs 2.1 | 78 | 55 |
