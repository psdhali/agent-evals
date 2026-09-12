"""Dataset mirror wiring (review §2, 2026-08-16).

The seed mirror was write-only: neither the loader nor any task read it, while
the runtime hit HuggingFace unauthenticated on every call — 1,800 times at
Phase 8, rate-limited at n=1.  5b wires the loader to the S3 mirror:

  * ``load()`` default (public) -> ``<rev>.public.jsonl`` — gold patch EXCLUDED,
    so a harness-facing dispatch never holds the answer;
  * ``load(include_gold=True)`` -> ``<rev>.jsonl`` (full row incl. patch) — the
    grading path only;
  * unseeded mirror -> HuggingFace fallback, explicit and logged.

These tests mock ``queue.client.get_s3_client`` so nothing touches S3 or HF in
CI; the S3 shape (keys, rows) and the fallback path are what is pinned.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from swebench_eval.dataset import swebench_loader as loader_mod
from swebench_eval.dataset.base import Instance

_REV = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"


def _full_row(instance_id: str = "django__django-11179") -> dict[str, str]:
    return {
        "instance_id": instance_id,
        "repo": "django/django",
        "base_commit": "abc111",
        "problem_statement": "Fix the thing.",
        "hints": "hint",
        "patch": "diff --git a/thing.py b/thing.py\n+f ix",
        "fail_to_pass": "tests/test_thing.py::test_fix",
        "pass_to_pass": "tests/test_thing.py::test_ok",
        "test_patch": "diff --git a/thing.py b/thing.py",
        "environment_setup_commit": "setup111",
        "version": "1.0.0",
        "created_at": "2026-01-01T00:00:00Z",
        # SWE-bench 5.x columns (ADR-0043).
        "image": "swebench/sweb.eval.x86_64.django_1776_django-11179:latest",
        "eval_script": "#!/bin/bash\ngit apply -v - <<'EOF'\ndiff --git a/thing.py b/thing.py\nEOF\n",
        "log_parser": "parse_log_django",
        "eval_type": "pass_and_fail",
    }


def _public_row(instance_id: str = "django__django-11179") -> dict[str, str]:
    # B1 (dataset-mirror-test-patch-exposure): the public row carries NONE of
    # the answer fields — not just ``patch``/``hints``, but ``test_patch`` (the
    # gold test SOURCE) and ``fail_to_pass``/``pass_to_pass`` (the hidden test
    # NAMES) too, so an agent with the task-role credentials cannot read them.
    # ``environment_setup_commit`` STAYS (a commit SHA, not the answer; found
    # live 2026-08-18: without it, make_test_spec fetches requirements.txt at an
    # empty commit and the django rows are dropped at dispatch).
    row = _full_row(instance_id)
    # ADR-0043: ``eval_script`` embeds the gold test_patch + hidden directives —
    # answer material, so it is stripped with the rest.  ``image``,
    # ``log_parser`` and ``eval_type`` stay (an image name and parser labels).
    for k in (
        "hints",
        "patch",
        "fail_to_pass",
        "pass_to_pass",
        "test_patch",
        "created_at",
        "eval_script",
    ):
        del row[k]
    return row


def test_public_fields_match_the_public_row_and_exclude_eval_script() -> None:
    """The seed writer's PUBLIC_FIELDS is exactly the public row's key set.

    Locks the ADR-0043 split: the 5.x ``eval_script`` (which embeds the gold
    ``test_patch`` verbatim) is on the full row only; ``image``/``log_parser``/
    ``eval_type`` are public.  A future field added to one list but not the
    other fails here rather than at the first dispatch.
    """
    assert set(loader_mod.PUBLIC_FIELDS) == set(_public_row())
    assert "eval_script" not in loader_mod.PUBLIC_FIELDS
    assert "eval_script" in loader_mod.FULL_FIELDS
    assert set(loader_mod.FULL_FIELDS) == set(_full_row())


def test_gold_load_carries_the_eval_script() -> None:
    """The grading row carries the 5.x eval_script; the public row does not."""
    client = _client(public_rows=[_public_row()], full_rows=[_full_row()])
    with mock.patch.object(loader_mod, "get_s3_client", return_value=client):
        gold = loader_mod.load_single_instance("django__django-11179", include_gold=True)
        public = loader_mod.load_single_instance("django__django-11179", include_gold=False)
    assert gold is not None and public is not None
    assert gold.eval_script.startswith("#!/bin/bash")
    assert gold.log_parser == "parse_log_django" and gold.eval_type == "pass_and_fail"
    assert gold.image.endswith("django_1776_django-11179:latest")
    assert public.eval_script == ""
    assert public.image == gold.image


def test_hf_row_lists_become_json_strings() -> None:
    """5.x HF rows type FAIL_TO_PASS/PASS_TO_PASS as lists; the Instance keeps JSON strings.

    ``str(list)`` would be a Python repr (single quotes) that make_test_spec's
    json.loads rejects — found while adopting 78f471bf.
    """
    inst = loader_mod._row_to_instance(
        {
            "instance_id": "a__a-1",
            "repo": "a/a",
            "base_commit": "c",
            "problem_statement": "p",
            "FAIL_TO_PASS": ["t.py::test_a", "t.py::test_b"],
            "PASS_TO_PASS": [],
            "image": "swebench/sweb.eval.x86_64.a_1776_a-1:latest",
        }
    )
    assert json.loads(inst.fail_to_pass) == ["t.py::test_a", "t.py::test_b"]
    assert inst.pass_to_pass == ""
    assert inst.image.endswith(":latest")


def _client(
    public_rows: list[dict[str, str]], full_rows: list[dict[str, str]] | None = None
) -> mock.Mock:
    """Fake boto3 S3 client serving the two mirror objects by their keys."""
    client = mock.Mock()
    full = full_rows if full_rows is not None else public_rows

    def get(Bucket: str = "", Key: str = "", **kwargs: object) -> dict[str, object]:
        rows = full if Key.endswith(".jsonl") and not Key.endswith(".public.jsonl") else public_rows
        body = "\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n"
        return {"Body": mock.Mock(read=lambda: body.encode("utf-8"))}

    client.get_object = mock.Mock(side_effect=get)
    return client


def test_public_load_excludes_gold_patch() -> None:
    """The default harness-facing load reads .public.jsonl and carries no patch.

    The public subset carries ``environment_setup_commit`` (a commit SHA, not
    the answer) because ``make_test_spec`` needs it to derive the env image key
    — an empty value silently drops the row (found live: django-10924 refused
    at dispatch).
    """
    client = _client(public_rows=[_public_row()])
    with mock.patch.object(loader_mod, "get_s3_client", return_value=client):
        instances = loader_mod.SwebenchLiteLoader().load()
    assert len(instances) == 1
    inst = instances[0]
    assert inst.instance_id == "django__django-11179"
    assert inst.patch == ""
    assert inst.hints == ""
    assert inst.environment_setup_commit == "setup111"
    assert inst.problem_statement == "Fix the thing."
    # B1: the answer fields are stripped from the public row AND default to ""
    # in the loaded Instance — an agent cannot read its own gold test source.
    assert inst.fail_to_pass == ""
    assert inst.test_patch == ""
    assert inst.pass_to_pass == ""


def test_gold_load_carries_the_patch() -> None:
    """include_gold=True reads the full mirror and populates the TestSpec fields."""
    client = _client(public_rows=[_public_row()], full_rows=[_full_row()])
    with mock.patch.object(loader_mod, "get_s3_client", return_value=client):
        inst = loader_mod.load_single_instance("django__django-11179", include_gold=True)
    assert inst is not None
    assert inst.patch.startswith("diff --git")
    assert inst.environment_setup_commit == "setup111"


def test_single_public_load_returns_none_for_missing() -> None:
    """A public single-instance load of an absent ID returns None, not a crash."""
    client = _client(public_rows=[_public_row("a__a")])
    with mock.patch.object(loader_mod, "get_s3_client", return_value=client):
        got = loader_mod.load_single_instance("z__z", include_gold=False)
    assert got is None


def test_unseeded_mirror_falls_back_to_huggingface() -> None:
    """GRADING path (include_gold=True): mirror miss -> HF load, explicitly."""
    client = mock.Mock()
    client.get_object = mock.Mock(side_effect=Exception("NoSuchKey"))
    hf_rows = [Instance(instance_id="x__x", repo="r", base_commit="c", problem_statement="p")]
    with (
        mock.patch.object(loader_mod, "get_s3_client", return_value=client),
        mock.patch.object(loader_mod, "_load_from_huggingface", return_value=hf_rows) as m,
    ):
        instances = loader_mod.SwebenchLiteLoader(include_gold=True).load()
    assert m.called
    assert instances[0].instance_id == "x__x"


def test_missing_public_mirror_refuses_to_fallback() -> None:
    """Stage 0.3 hardening: a harness-facing load must NOT fall back to HF.

    HF carries the gold patch and ignores include_gold, so when the public
    mirror is absent the loader must RAISE rather than serve the answer on the
    harness path. A single Instance loaded here would be the leak.
    """
    client = mock.Mock()
    client.get_object = mock.Mock(side_effect=Exception("NoSuchKey"))
    with (
        mock.patch.object(loader_mod, "get_s3_client", return_value=client),
        pytest.raises(RuntimeError, match="gold"),
    ):
        loader_mod.SwebenchLiteLoader().load()  # include_gold=False default
    # And load_single_instance on the harness path, same contract.
    with (
        mock.patch.object(loader_mod, "get_s3_client", return_value=client),
        pytest.raises(RuntimeError, match="gold"),
    ):
        loader_mod.load_single_instance("x__x", include_gold=False)


def test_hf_fallback_raises_when_gold_excluded() -> None:
    """The fallback itself refuses include_gold=False — never serves gold."""
    with (
        mock.patch.object(loader_mod, "get_s3_client", side_effect=Exception("unreachable")),
        pytest.raises(RuntimeError, match="gold"),
    ):
        loader_mod._load_from_huggingface("test", _REV, include_gold=False)


def test_load_sorts_by_instance_id() -> None:
    client = _client(public_rows=[_public_row("b__b"), _public_row("a__a")])
    with mock.patch.object(loader_mod, "get_s3_client", return_value=client):
        ids = [i.instance_id for i in loader_mod.SwebenchLiteLoader().load()]
    assert ids == sorted(ids)


def test_malformed_mirror_row_is_dropped_not_fatal() -> None:
    """A corrupt line must not sink the whole load."""
    client = mock.Mock()
    body = json.dumps({"instance_id": "ok__ok"}) + "\n{not-json\n"
    client.get_object = mock.Mock(
        return_value={"Body": mock.Mock(read=lambda: body.encode("utf-8"))}
    )
    with mock.patch.object(loader_mod, "get_s3_client", return_value=client):
        instances = loader_mod.SwebenchLiteLoader().load()
    assert len(instances) == 1
    assert instances[0].instance_id == "ok__ok"


@pytest.mark.parametrize(
    "dataset_name,expected_prefix",
    [
        ("SWE-bench/SWE-bench_Verified", "SWE-bench/SWE-bench_Verified"),
        ("SWE-bench/SWE-bench_Lite", "SWE-bench/SWE-bench_Lite"),
        # The pre-ADR-0043 org still works as an override (checkpoint revert path).
        ("princeton-nlp/SWE-bench_Verified", "princeton-nlp/SWE-bench_Verified"),
    ],
)
def test_mirror_key_derives_from_single_source_of_truth(
    monkeypatch: pytest.MonkeyPatch, dataset_name: str, expected_prefix: str
) -> None:
    """V1/DoD 5: the mirror key is built from ``_DATASET_NAME``, not a literal.

    Overriding the ONE source of truth must change every S3 key the loader
    reads.  Proved by mutation: if ``_mirror_key`` hardcoded
    ``"princeton-nlp/SWE-bench_Lite"`` back, this test FAILS on the Verified
    case (the key would not start with the Verified prefix).
    """
    monkeypatch.setattr(loader_mod, "_DATASET_NAME", dataset_name)
    key = loader_mod._mirror_key("test", _REV, include_gold=False)
    assert key == f"{expected_prefix}/test/{_REV}.public.jsonl"
    gold_key = loader_mod._mirror_key("test", _REV, include_gold=True)
    assert gold_key == f"{expected_prefix}/test/{_REV}.jsonl"
