"""The ECS task-id denylist matches ids in ARNs and log-stream names, not bare MD5 hashes."""

from swebench_eval.orchestrator import timeline_export as te

_ID = "ff1d6379965c4153a724c6d49f4700b0"


def _hits(text: str) -> list[str]:
    return [h for h in te.scrub_hits(text) if h.startswith("ecs_task_id:")]


def test_task_ids_in_arns_and_log_streams_are_hits() -> None:
    assert _hits(f"arn:aws:ecs:us-west-2:123456789012:task/eval-dev-cluster/{_ID}")
    assert _hits(f"stream llm-judge/llm-judge/{_ID}")
    assert _hits(f"s3://bucket/runs/x/{_ID}")


def test_bare_md5_is_not_a_hit() -> None:
    # An HTTP Digest response quoted from a Django test; a judge citing it must not refuse the export.
    assert not _hits('response="c549dd8c8e15bcce57c8e2b5e5437218", qop="auth"')
    assert not _hits("md5 c549dd8c8e15bcce57c8e2b5e5437218 of the fixture")
    assert not _hits(f"id={_ID}")
