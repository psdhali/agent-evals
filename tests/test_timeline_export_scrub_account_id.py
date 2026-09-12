"""The account-id denylist matches bare 12-digit runs, not the last group of a UUID."""

from swebench_eval.orchestrator import timeline_export as te


def _hits(text: str) -> list[str]:
    return [h for h in te.scrub_hits(text) if h.startswith("aws_account_id:")]


def test_bare_twelve_digit_runs_are_hits() -> None:
    assert _hits("account 123456789012 owns the bucket")
    assert _hits("arn-less mention: 123456789012")
    assert _hits("(123456789012)")


def test_uuid_last_group_is_not_a_hit() -> None:
    # Django's admin test fixtures use this UUID; a judge quoting it must not refuse the export.
    assert not _hits("From `.../user/22222222-3333-4444-5555-666677778888/change/`")
    assert not _hits("id=a1b2c3d4-e5f6-7890-abcd-123456789012")


def test_digits_inside_longer_tokens_are_not_hits() -> None:
    assert not _hits("sha 123456789012abcdef")
    assert not _hits("v1.123456789012.2")
