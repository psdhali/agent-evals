# agent-evals

A public, open-source evaluation framework for SWE-bench that lets you pick any agent harness and
any model independently, run the grading at scale on AWS, and reproduce results others can trust.

- **Set it up:** `docs/SETUP.md` — from a laptop-only graded instance (`make local-smoke`, no
  AWS, cents) to a full deployment in any AWS account (`make doctor`, `make bootstrap`,
  `make up-persistent`, `make images SUBSET=10`, `make up`, `make down`). `make help` lists
  every target.
- **What a deployment consists of:** `docs/INVENTORY.md`.
- **Why it is built this way:** the Design pages on the project site; the design record itself is not in this repository.
- **How it was validated in a fresh account:** `docs/FRESH-ACCOUNT-LOG.md`.
