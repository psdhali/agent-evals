"""Logging bootstrap for the long-running service entrypoints.

Found live in the 5a-ii consolidated round: the control-plane, harness worker and
eval worker run via ``python -c`` in ``infra/docker/entrypoint.sh`` with NO logging
handler configured. Python's last-resort handler emits **WARNING and above only**,
so every ``logger.info(...)`` decision point (received job, completed job, enqueued
eval, processed result) was silently discarded and the CloudWatch streams stayed
empty unless a traceback landed. Only ``uvicorn`` (which configures logging itself)
had logs; the two ``python -c`` loop services never did. A clean run and a container
that silently did nothing were indistinguishable in CloudWatch.

This is the fix: call ``configure_logging()`` at the very top of each poll-loop
``run_*()`` entrypoint so Python's root logger emits INFO+ to stdout (the awslogs
driver ships stdout to CloudWatch). It is deliberately NOT imported by the package
``__init__`` — local tests keep their own logging behaviour; only the deployed
entrypoints opt in.
"""

from __future__ import annotations

import logging
import sys

# The format mirrors what uvicorn emits, so a mixed stream (uvicorn service +
# a python -c service sharing a log group) reads consistently.
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO, force: bool = False) -> None:
    """Configure the root logger to emit ``level``+ to stdout, idempotently.

    ``force`` re-applies even if ``basicConfig`` has already run — normally not
    needed (basicConfig is a no-op after the first call unless handlers exist);
    kept for tests that need to assert the behaviour cleanly.
    """
    logging.basicConfig(level=level, stream=sys.stdout, format=_FORMAT, force=force)
