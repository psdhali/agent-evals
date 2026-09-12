#!/usr/bin/env python3
"""Apply our indexes to LiteLLM's spend-log table (manual run).

The API does this itself at startup (``llm_live.ensure_spend_log_indexes_in_background``);
this is the same call for a shell, e.g. from a one-off task inside the VPC or against the
local compose stack::

    LITELLM_SPEND_DATABASE_URL=postgresql://litellm:litellm@localhost:5433/litellm \\
        uv run python scripts/ensure_spend_db_indexes.py

Statements come from ``infra/docker/spend_db_indexes.sql`` (the record). Idempotent.
Exit 0 when every statement ran, 1 when the DB was unreachable or a statement failed
(details are logged).
"""

from __future__ import annotations

import logging
import sys

from swebench_eval.logging_bootstrap import configure_logging
from swebench_eval.orchestrator.api import llm_live


def main() -> int:
    configure_logging()
    log = logging.getLogger("ensure_spend_db_indexes")
    path = llm_live._INDEX_SQL_PATH
    expected = [
        s.strip()
        for s in "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("--")
        ).split(";")
        if s.strip()
    ]
    done = llm_live.ensure_spend_log_indexes()
    log.info("%d of %d statements applied from %s", len(done), len(expected), path)
    return 0 if len(done) == len(expected) else 1


if __name__ == "__main__":
    sys.exit(main())
