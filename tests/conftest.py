"""Authoring-time guard against tests that leak real network I/O.

A test that only passes because ``docker compose up`` is running will fail in
CI — the runner has no Postgres, Redis, ElasticMQ, or MinIO.  Everyone runs
compose locally, so such a test is green here and goes red on some later push;
that exact defect class ran CI red for 17 commits before anyone read it.

This autouse fixture makes the failure happen at authoring time instead: any
test *not* marked ``integration`` that attempts a real outbound connection
fails with a message naming the fix, on the author's machine.

Exemptions — the cases that would also pass in CI, so blocking them is noise:

  - tests marked ``@pytest.mark.integration`` (they need the compose stack and
    are deselected in CI by pyproject's ``addopts``);
  - AF_UNIX sockets (no network);
  - loopback to a port that is NOT a compose-service port — e.g.
    ``test_local_proxy.py``'s own ``ThreadingHTTPServer`` (bound to port 0, so
    it lands in the ephemeral range) or its deliberate connection-refused
    probes against ``127.0.0.1:1``.  Those servers are started by the test
    itself and exist in CI too.  The compose services we leak to sit on fixed
    ports, so those specifically raise.

Two layers are guarded:

  - **Python sockets** — urllib3/httpx/redis/botocore all connect through
    ``socket`` at call time, replaced per-test below; and
  - **the Postgres funnel itself** — psycopg2 connects through libpq in C,
    which never touches those hooks.  ``get_connection`` is the single entry
    point (``connection.py`` defers the ``psycopg2.connect`` import inside the
    function; there is no direct ``psycopg2.connect`` elsewhere in
    ``swebench_eval/`` or ``scripts/``), so the fixture patches it to raise the
    same message.  A test that calls any of the ~15 ``get_connection`` sites
    fails at authoring time instead of silently reaching the real database.

This is a loud authoring-time nudge, not a sandbox — the property is that a
test which would fail in CI fails on the author's machine first.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Generator
from typing import Any
from unittest import mock

import pytest

# The compose-stack services tests historically leak to (docker-compose.yml).
# A test connecting to one passes only because the stack is up; CI has none.
# Every published host port is listed — including the gateway and the web
# consoles, which a harness/shim test would reach for as readily as the data
# services (docker-compose.yml).
_COMPOSE_PORTS = frozenset(
    {
        4000,  # LiteLLM gateway
        5432,  # postgres
        5433,  # LiteLLM's own postgres
        6379,  # redis
        9000,  # minio (S3 API)
        9001,  # minio (web console)
        9324,  # elasticmq (SQS)
        9325,  # elasticmq (web UI)
    }
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# The EC2 metadata service (IMDSv2).  botocore's credential chain probes it as
# a benign fast-fail — connection refused in CI and local dev alike, caught by
# botocore, never a compose-service leak.  Blocking it turns that handled probe
# into an unhandled RuntimeError (test_scale_to_zero_guard module-executes the
# Lambda, whose top-level boto3.client(...) triggers the probe).
_BENIGN_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254"})

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_create_connection = socket.create_connection


def _raise_blocked(address: Any) -> None:
    raise RuntimeError(
        f"test attempted a real network connection to {address!r}. "
        "CI has no Postgres/Redis/SQS/MinIO, so this test would fail there. "
        "Stub the boundary (mock.patch the db/redis/queue client), or if the "
        "test genuinely needs the compose stack mark it @pytest.mark.integration."
    )


def _check_address(address: Any) -> None:
    """Raise unless *address* is one of the self-contained exemptions."""
    if not isinstance(address, tuple):
        return  # AF_UNIX path (or non-tuple form) — no network.
    host = address[0]
    port = address[1] if len(address) >= 2 else None
    if host in _BENIGN_HOSTS:
        return  # EC2 metadata service: a handled fast-fail everywhere.
    if host in _LOOPBACK_HOSTS and port not in _COMPOSE_PORTS:
        return  # the test's own server or refused probe — fine in CI too.
    _raise_blocked(address)


def _blocked_connect(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
    _check_address(address)
    return _orig_connect(self, address, *args, **kwargs)


def _blocked_connect_ex(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
    _check_address(address)
    return _orig_connect_ex(self, address, *args, **kwargs)


def _blocked_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    _check_address(address)
    return _orig_create_connection(address, *args, **kwargs)


@pytest.fixture(autouse=True)
def _pacer_off_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """L1 pacer default-off in tests, same rationale as the socket guard above: the pacer's
    Redis is a compose service, so a unit-test LocalProxy touching it would either leak real
    I/O (here) or crawl through the 2.5s-per-call local fallback (in CI, where the socket guard
    fails the connection). Production workers leave EVAL_PACER_ENABLED unset = on; pacing tests
    construct LocalProxy(pacer=...) explicitly, which overrides this."""
    monkeypatch.setenv("EVAL_PACER_ENABLED", "0")
    # Same rationale for the eval autoscaler's supervisor sub-tick: its AWS/Redis reads are
    # compose-service boundaries, and its clock reads would interleave with tests that drive a
    # fake monotonic clock through the supervisor loop. Off in unit tests; its own suite
    # constructs EvalAutoscaler(mode=...) explicitly. Production default (env unset) = observe.
    monkeypatch.setenv("EVAL_AUTOSCALER_MODE", "off")
    # And the capacity observer's thread, same boundary set (SQS/ECS/Redis/Aurora); its own
    # suite constructs CapacityObserver(...) with injected fakes. Production default = on.
    monkeypatch.setenv("CAPACITY_OBSERVER_ENABLED", "0")


@pytest.fixture(autouse=True)
def _deployment_identity_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adoption Phase 1a: swebench_eval.aws_names derives bucket / registry names from the
    region, prefix and ACCOUNT ID — the last one via STS when AWS_ACCOUNT_ID is unset, which
    a unit test must never reach (no credentials, and the socket guard below blocks it).
    Pin the originating deployment's identity so every expectation that was written against
    the old literals (``eval-dev-dataset-123456789012-us-west-2`` …) still holds; a test
    that wants another identity sets its own env (tests/test_aws_names.py does)."""
    from swebench_eval import aws_names

    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    if not os.environ.get("AWS_REGION") and not os.environ.get("AWS_DEFAULT_REGION"):
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    aws_names.account_id.cache_clear()


@pytest.fixture(autouse=True)
def _no_real_sockets(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Block real sockets + the Postgres funnel unless the test opts into the stack."""
    if request.node.get_closest_marker("integration"):
        # Stack-backed test: leave the real stack reachable.  This MUST yield —
        # a generator fixture that returns without yielding makes pytest raise
        # "ValueError: <fixture> did not yield a value" at setup, which broke
        # every `-m integration` run (the bug was invisible in CI because
        # integration tests are deselected there, so the fixture never ran).
        yield
        return
    # get_connection is the C-level funnel: libpq bypasses the socket hooks, so
    # patch the funnel instead.  Callers resolve the name at call time
    # (functions import it right before use), so this intercepts all ~15 sites.
    with mock.patch(
        "swebench_eval.database.connection.get_connection",
        side_effect=lambda *a, **k: _raise_blocked(("postgres", 5432)),
    ):
        socket.socket.connect = _blocked_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _blocked_connect_ex  # type: ignore[method-assign]
        socket.create_connection = _blocked_create_connection
        yield
        socket.socket.connect = _orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _orig_connect_ex  # type: ignore[method-assign]
        socket.create_connection = _orig_create_connection
