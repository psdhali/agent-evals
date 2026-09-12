"""A1 readiness probe — verify the harness-isolated network end to end.

Runs from INSIDE a real Fargate task placed on the harness-isolated subnets
(ADR-0033): the only way to prove the control is to exercise it where the
untrusted agent will run. This is the bring-up gate — the E9 control exists
only when this goes fully green. Do NOT dispatch a scored run without it.

The probe asserts BOTH sides of the ADR-0033 claim "harness workers had no
route to the internet; the only reachable endpoints were the model gateway and
the git mirror service":

  must FAIL  (the route is gone, not just DNS):
    - an HTTPS connect to a raw IP (1.1.1.1) — the DoD-7 discriminator. A name
      check alone fails identically whether DNS broke or the route is absent;
      the raw-IP check is what proves the *route* is gone.
    - an HTTPS connect to https://github.com (the reviewed failure mode,
      verbatim — this is what fetched the gold test file on django-10924).

  must SUCCEED (what the harness legitimately reaches, per the owner's steer):
    - clone from the git mirror over git:// (DoD-8 — a real clone, not a DNS
      ping; the ingress side of the mirror SG is exactly what A1-2 fixed).
    - an SQS receive on the harness-jobs queue (the harness polls/acks it).
    - a model call through the LiteLLM gateway (the ADR's "model call must
      still succeed" side; internal ALB + VPC-local routing).

Usage (from the laptop, driving a one-off Fargate task on the ISOLATED network):

    python scripts/verify_harness_isolation.py \
        --mirror git://git-mirror.eval.internal/ \
        --repo django/django \
        --queue-url <harness-jobs queue url> \
        --gateway http://<internal-alb>:4000/v1 \
        --api-key <LITELLM_MASTER_KEY>

The probe deliberately does NOT take the full mirror/repo set: one repo proves
the git:// path (the 12 repos differ only by object store, served by the same
daemon on the same port). Exit code is non-zero if ANY expectation is violated.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import sys
import urllib.request
from collections.abc import Callable

# Hoisted to module scope so the B1/C2 probe tests can patch it and so
# `probe.boto3` exists (the script runs in the harness image where boto3 is
# installed; testability is the reason it is not a lazy local import).
import boto3


def _task_region() -> str:
    """The task's region from its own environment (the task definition sets it)."""
    import os

    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    if not region:
        raise RuntimeError("AWS_DEFAULT_REGION is not set in this task's environment")
    return region


# A blocked destination must fail its check; a reachable one must succeed.
BlockedCheck = tuple[str, str, Callable[[], None], bool]
# (label, detail, fn, expect_success)

_RAW_IP = "1.1.1.1"
_GITHUB = "https://github.com"
_HTTP_TIMEOUT_SECONDS = 8


def _connect_https(host: str, port: int = 443) -> None:
    """Open an HTTPS connection; raise on ANY failure (DNS, route, timeout, RST)."""
    ctx = ssl.create_default_context()
    with (
        socket.create_connection((host, port), timeout=_HTTP_TIMEOUT_SECONDS) as sock,
        ctx.wrap_socket(sock, server_hostname=host),
    ):
        pass


def _probe_raw_ip_blocked() -> None:
    """An HTTPS connect to a raw IP must FAIL. If it succeeds the route exists."""
    _connect_https(_RAW_IP)


def _probe_github_blocked() -> None:
    """An HTTPS connect to github.com must FAIL (the E9 fetch path)."""
    _connect_https("github.com")


def _probe_git_mirror(mirror: str, repo: str) -> None:
    """A real `git ls-remote` over git://.  Raises when the mirror is
    unreachable — which, since G1 (HARNESS-ISOLATION-AUDIT-2026-09-05 §3), is
    the REQUIRED outcome: the mirror is a full-history copy of every repo (the
    gold fix is one `git log` away), the pre-baked -inst never clones, so the
    harness network must not reach it at all.  Before G1 this probe had to
    SUCCEED (DoD-8 / A1-2); a probe that still passes on success would bless
    the leak."""
    proc = subprocess.run(
        ["git", "ls-remote", f"{mirror}{repo}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,  # we inspect the return code and surface stderr ourselves
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {proc.stderr.strip()}")
    if not proc.stdout.strip():
        raise RuntimeError("git ls-remote returned no refs")


def _probe_sqs(queue_url: str, region: str | None = None) -> None:
    """A real SQS receive on the harness queue — proves the sqs endpoint path."""
    import boto3

    client = boto3.client("sqs", region_name=region or _task_region())
    resp = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=0)
    # Receiving zero messages is a SUCCESS (the queue is empty); only a network/
    # auth failure should raise. Never delete what we receive.
    for _ in resp.get("Messages", []):
        pass


def _probe_gateway(gateway_base: str, api_key: str) -> None:
    """A model call through the internal LiteLLM gateway — the ADR's 'the gateway
    is the one path that cannot break' side. Uses a trivial models/list call so
    no tokens are spent: what must work is the ROUTE, not the completion."""
    url = f"{gateway_base.rstrip('/')}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        # Read the WHOLE body: this used to cap at 2048 bytes, which parsed fine
        # with a handful of models and became a false "FAIL: Unterminated
        # string" once the gateway listed 26+ (found live 2026-09-05, G1 probe
        # run). The route is what is under test, not the body size.
        body = resp.read()
    json.loads(body)  # must be parseable JSON (a proxy error would fail this)


def _probe_dataset_public_readable(bucket: str, object_key: str) -> None:
    """B1 (dataset exposure): the harness task role MUST be able to GetObject the
    PUBLIC mirror (it drives dispatch + repo prep).  If it cannot, the allow-list
    over-stripped something and dispatch breaks — read the public file to prove
    the harness legitimately can."""
    client = boto3.client("s3", region_name=_task_region())
    obj = client.get_object(Bucket=bucket, Key=object_key)
    if not obj["Body"].read(64):
        raise RuntimeError("public mirror object is empty")


def _probe_dataset_gold_not_readable(bucket: str, gold_key: str) -> None:
    """B1 + C2 (dataset-mirror-test-patch-exposure): the harness task role MUST
    NOT be able to read the FULL-gold mirror object — which carries test_patch +
    fail_to_pass.  The S3DatasetReadPublicOnly IAM grant is scoped to
    *.public.jsonl; a GetObject of the .jsonl full file must fail with
    AccessDenied.  Proves the gold source cannot reach the harness tier.

    C2 (review): ONLY AccessDenied counts as a pass.  NoSuchKey/404 means the
    probe is pointed at the WRONG object (a typo'd key, a bumped revision, a
    changed bucket) — in the correctly-configured run S3 returns AccessDenied,
    never NoSuchKey — so a missing object is a LOUD FAILURE (misconfiguration),
    not a silent green.
    """
    from botocore.exceptions import ClientError

    client = boto3.client("s3", region_name=_task_region())
    try:
        client.get_object(Bucket=bucket, Key=gold_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AccessDenied", "PermissionDenied"):
            return  # the only pass: the role cannot read the gold object
        # NoSuchKey / 404 / anything else = the probe is misconfigured or the
        # failure is not an IAM denial — fail loudly, never a silent green.
        raise RuntimeError(
            f"gold mirror check inconclusive: GetObject failed with {code} "
            f"(expected AccessDenied) on {bucket}/{gold_key}"
        )
    raise RuntimeError("gold mirror object is READABLE — the exposure is open")


def run_probe(
    mirror: str,
    repo: str,
    queue_url: str,
    gateway: str,
    api_key: str,
    dataset_bucket: str | None = None,
    public_object_key: str | None = None,
    gold_object_key: str | None = None,
) -> dict[str, str]:
    """Run every check; return {label: "PASS"|"FAIL: <reason>"}."""
    checks: list[BlockedCheck] = [
        (f"https://{_RAW_IP} (raw IP) is BLOCKED", "DoD-7", _probe_raw_ip_blocked, False),
        (f"{_GITHUB} is BLOCKED", "E9", _probe_github_blocked, False),
        (
            f"git clone from {mirror}{repo} is BLOCKED",
            "G1 (inverts DoD-8 / A1-2)",
            lambda: _probe_git_mirror(mirror, repo),
            False,
        ),
        ("SQS receive on harness-jobs WORKS", "owner steer", lambda: _probe_sqs(queue_url), True),
        (
            f"gateway model route {gateway} WORKS",
            "ADR-0033 §1",
            lambda: _probe_gateway(gateway, api_key),
            True,
        ),
    ]
    # B1 + C2 (dataset-mirror-test-patch-exposure): the harness must be able to
    # read the PUBLIC mirror but MUST NOT be able to read the full-gold one
    # (which carries test_patch + fail_to_pass).  C2: the three dataset args are
    # REQUIRED — a gate invocation that omits them must fail loudly (reporting
    # success while testing nothing is a meaningless green, the A1-7 pattern),
    # never be silently skipped.
    if not (dataset_bucket and public_object_key and gold_object_key):
        raise RuntimeError(
            "dataset-mirror B1 probe requires --dataset-bucket, --public-object-key "
            "and --gold-object-key; refusing to run the harness isolation probe "
            "without them (an omitted gold check is not a pass)"
        )
    results: dict[str, str] = {}
    for label, detail, fn, expect_success in checks:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — every failure mode is a finding
            results[label] = (
                "FAIL"
                if expect_success
                else f"PASS (blocked as required: {exc.__class__.__name__})"
            )
            if expect_success:
                results[label] = f"FAIL: {exc}"
        else:
            results[label] = (
                "PASS" if expect_success else "FAIL: connect succeeded — the route EXISTS"
            )

    # B1 (C2): the two dataset probes.  Run them EXPLICITLY, not through the
    # generic BlockedCheck bridge above, because their failure semantics differ:
    # the gold probe RETURNS normally when it confirms AccessDenied (blocked =
    # good) and RAISES when the object is readable (exposure) or the check is
    # inconclusive (NoSuchKey/misconfiguration) — the generic
    # expect_success=False bridge would mislabel a return as "reachable".
    try:
        _probe_dataset_public_readable(dataset_bucket, public_object_key)
    except Exception as exc:  # noqa: BLE001
        results[f"s3 public mirror {public_object_key} IS readable"] = f"FAIL: {exc}"
    else:
        results[f"s3 public mirror {public_object_key} IS readable"] = "PASS"

    try:
        _probe_dataset_gold_not_readable(dataset_bucket, gold_object_key)
    except Exception as exc:  # noqa: BLE001
        results[f"s3 gold mirror {gold_object_key} is BLOCKED"] = f"FAIL: {exc}"
    else:
        results[f"s3 gold mirror {gold_object_key} is BLOCKED"] = "PASS"

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mirror", required=True, help="git mirror base, e.g. git://git-mirror.eval.internal/"
    )
    parser.add_argument(
        "--repo", default="django/django", help="one repo to prove the git:// clone path"
    )
    parser.add_argument("--queue-url", required=True, help="harness-jobs SQS queue URL")
    parser.add_argument(
        "--gateway", required=True, help="LiteLLM gateway base URL, e.g. http://<alb>:4000/v1"
    )
    parser.add_argument("--api-key", required=True, help="LiteLLM master key")
    parser.add_argument(
        "--dataset-bucket",
        default=None,
        help="dataset mirror bucket (B1): checks PUBLIC readable / GOLD blocked",
    )
    parser.add_argument(
        "--public-object-key",
        default=None,
        help="a public mirror object key (B1), e.g. princeton-nlp/<dataset>/test/<rev>.public.jsonl",
    )
    parser.add_argument(
        "--gold-object-key",
        default=None,
        help="the full-gold mirror object key (B1), e.g. princeton-nlp/<dataset>/test/<rev>.jsonl",
    )
    args = parser.parse_args()

    results = run_probe(
        args.mirror,
        args.repo,
        args.queue_url,
        args.gateway,
        args.api_key,
        dataset_bucket=args.dataset_bucket,
        public_object_key=args.public_object_key,
        gold_object_key=args.gold_object_key,
    )
    failures = [k for k, v in results.items() if v.startswith("FAIL")]
    for label, status in results.items():
        print(f"{status:>5}  {label}")
    print()
    if failures:
        print(
            f"ISOLATION PROBE FAILED — {len(failures)}: E9's control is NOT proven. Do not dispatch."
        )
        return 1
    print(
        "ISOLATION PROBE GREEN — the E9 control exists: no route to the internet; git / SQS / gateway reachable."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
