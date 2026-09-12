"""A1 probe judgment logic — the expectations are the test.

The live network truths (github blocked, git/SQS/gateway reachable) are the
bring-up probe's job, not a unit test's. What is unit-testable IS the judgment:
a connect that raises counts as PASS when we EXPECT it blocked (and as FAIL when
we expect it reachable), and a connect that succeeds counts as FAIL when blocked
was expected — the DoD-7 discriminator, which existing name-only checks could not
make. This test pins that classification so the probe cannot silently "pass"
because DNS happened to break instead of the route being gone.
"""

from __future__ import annotations

from unittest import mock

import scripts.verify_harness_isolation as probe


def _args() -> dict[str, str]:
    return {
        "mirror": "git://git-mirror.eval.internal/",
        "repo": "django/django",
        "queue_url": "https://sqs.us-west-2.amazonaws.com/123/eval-dev-harness-jobs",
        "gateway": "http://10.0.0.10:4000/v1",
        "api_key": "k",
        # C2: the dataset B1 probe args are REQUIRED — omitted, run_probe raises.
        "dataset_bucket": "eval-dev-dataset-…",
        "public_object_key": "princeton-nlp/SWE-bench_Lite/test/1234.public.jsonl",
        "gold_object_key": "princeton-nlp/SWE-bench_Lite/test/1234.jsonl",
    }


import contextlib


@contextlib.contextmanager
def _b1_probes_pass():
    """Patch the two B1 S3 probes to their green outcomes (public readable,
    gold blocked) so the other checks run in isolation.  Context-managed so the
    real functions are restored (no leak into other tests)."""
    with (
        mock.patch.object(probe, "_probe_dataset_public_readable", lambda *a, **k: None),
        mock.patch.object(probe, "_probe_dataset_gold_not_readable", lambda *a, **k: None),
    ):
        yield


def test_isolated_network_all_green() -> None:
    """Blocked ones blocked, reachable ones reachable → every key a PASS."""
    with (
        mock.patch(
            "scripts.verify_harness_isolation._connect_https", side_effect=TimeoutError("blocked")
        ),
        mock.patch(
            "scripts.verify_harness_isolation._probe_git_mirror",
            side_effect=RuntimeError("git ls-remote failed: connection timed out"),
        ),
        mock.patch("scripts.verify_harness_isolation._probe_sqs", lambda *a, **k: None),
        mock.patch("scripts.verify_harness_isolation._probe_gateway", lambda *a, **k: None),
        _b1_probes_pass(),
    ):
        results = probe.run_probe(**_args())

    assert all(v.startswith("PASS") for v in results.values()), results
    assert any("raw IP) is BLOCKED" in k for k in results)
    assert any("git clone" in k and "BLOCKED" in k for k in results)
    assert any("gold mirror" in k for k in results)  # the B1 probes actually ran


def test_open_internet_detected_as_failure() -> None:
    """The DoD-7 discriminator: if the raw-IP connect SUCCEEDS the route exists —
    the probe must call that a FAIL, never a green."""
    with (
        mock.patch("scripts.verify_harness_isolation._connect_https", lambda *a, **k: None),
        mock.patch(
            "scripts.verify_harness_isolation._probe_git_mirror",
            side_effect=RuntimeError("git ls-remote failed: connection timed out"),
        ),
        mock.patch("scripts.verify_harness_isolation._probe_sqs", lambda *a, **k: None),
        mock.patch("scripts.verify_harness_isolation._probe_gateway", lambda *a, **k: None),
        _b1_probes_pass(),
    ):
        results = probe.run_probe(**_args())

    failures = [v for v in results.values() if v.startswith("FAIL")]
    # raw-IP + github are reachable here (the DoD-7 discriminator) -> 2 FAILs;
    # the mirror is blocked (G1's required outcome) -> PASS;
    # the B1 probes are stubbed to their green outcomes -> PASS.
    assert len(failures) == 2, results
    assert any("gold mirror" in k for k in results)


def test_reachable_git_mirror_is_a_failure() -> None:
    """G1 (HARNESS-ISOLATION-AUDIT-2026-09-05 §3): a mirror the harness CAN
    clone from is the leak — the full-history mirror holds the gold fix.  The
    probe must FAIL on a successful ls-remote even with everything else green.
    (Before G1 this test asserted the opposite, DoD-8 / A1-2.)"""
    with (
        mock.patch(
            "scripts.verify_harness_isolation._connect_https", side_effect=TimeoutError("blocked")
        ),
        mock.patch("scripts.verify_harness_isolation._probe_git_mirror", lambda *a, **k: None),
        mock.patch("scripts.verify_harness_isolation._probe_sqs", lambda *a, **k: None),
        mock.patch("scripts.verify_harness_isolation._probe_gateway", lambda *a, **k: None),
        _b1_probes_pass(),
    ):
        results = probe.run_probe(**_args())

    assert results
    git = [v for k, v in results.items() if "git clone" in k]
    assert git and git[0].startswith("FAIL"), results


# --- C2 (stage-c-round-review): the B1 probes must RAN and be able to FAIL ---


def test_probe_without_dataset_args_raises() -> None:
    """C2: omitting the B1 dataset args must FAIL LOUDLY, never a silent green —
    a gate invocation that tests nothing must not report success."""
    import pytest

    args = _args()
    for k in ("dataset_bucket", "public_object_key", "gold_object_key"):
        del args[k]
    with (
        mock.patch(
            "scripts.verify_harness_isolation._connect_https", side_effect=TimeoutError("blocked")
        ),
        pytest.raises(RuntimeError, match="dataset-mirror B1 probe requires"),
    ):
        probe.run_probe(**args)


def test_gold_not_readable_denies_access() -> None:
    """C2: AccessDenied on the gold object -> the green B1 classifies the gold
    mirror as blocked (no raise = pass)."""
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
    with mock.patch.object(probe, "boto3") as b3:
        client = mock.Mock()
        client.get_object.side_effect = err
        b3.client.return_value = client
        probe._probe_dataset_gold_not_readable("bucket", "gold.jsonl")  # no raise = pass


def test_gold_missing_object_is_a_failure_not_a_pass() -> None:
    """C2: a missing gold object (NoSuchKey — a typo'd key or bumped revision)
    is a misconfiguration, NOT a pass; the gold-mirror assertion must fail."""
    import pytest
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "NoSuchKey", "Message": "nope"}}, "GetObject")
    with mock.patch.object(probe, "boto3") as b3:
        client = mock.Mock()
        client.get_object.side_effect = err
        b3.client.return_value = client
        with pytest.raises(RuntimeError, match="inconclusive"):
            probe._probe_dataset_gold_not_readable("bucket", "gold.jsonl")


def test_gold_readable_reports_the_exposure() -> None:
    """C2: if the gold object is actually READABLE, the probe must fail loudly
    (the security assertion can fail — C2's requirement)."""
    import pytest

    with mock.patch.object(probe, "boto3") as b3:
        client = mock.Mock()
        client.get_object.return_value = {"Body": mock.Mock(read=lambda n: b"gold")}
        b3.client.return_value = client
        with pytest.raises(RuntimeError, match="READABLE"):
            probe._probe_dataset_gold_not_readable("bucket", "gold.jsonl")


def test_public_probe_confirms_the_mirror_is_readable() -> None:
    """C2: the public-mirror check passes when the harness CAN read it (dispatch
    depends on it), proving the allow-list did not over-strip."""
    with mock.patch.object(probe, "boto3") as b3:
        client = mock.Mock()
        client.get_object.return_value = {"Body": mock.Mock(read=lambda n: b"{}")}
        b3.client.return_value = client
        probe._probe_dataset_public_readable("bucket", "public.jsonl")  # no raise = pass
        # An empty public object IS a finding, though.
        client2 = mock.Mock()
        client2.get_object.return_value = {"Body": mock.Mock(read=lambda n: b"")}
        b3.client.side_effect = None
        b3.client.return_value = client2
        import pytest

        with pytest.raises(RuntimeError, match="empty"):
            probe._probe_dataset_public_readable("bucket", "public.jsonl")
