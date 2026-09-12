"""The hostname denylist matches VPC-internal names and AWS endpoints, not code identifiers."""

from swebench_eval.orchestrator import timeline_export as te


def _hits(text: str) -> list[str]:
    return [h for h in te.scrub_hits(text) if h.startswith("hostname:")]


def test_internal_hostnames_are_hits() -> None:
    assert _hits("call gateway.eval.internal:4000")
    assert _hits("host ip-10-0-179-68.us-west-2.compute.internal")
    assert _hits("bucket eval-dev-artifacts-123456789012-us-west-2.s3.amazonaws.com")


def test_code_identifiers_ending_in_internal_are_not_hits() -> None:
    assert not _hits("the agent read UUIDField.internal and models.internal_type")
    assert not _hits("self.internal = True")
