"""ADR-0038 — contamination detector (pure logic, no dataset/repo).

Covers the per-attempt node-id string search and the artifact load, per the
design's honest-limits framing: a hit is evidence for review, not a verdict,
and a missing artifact means unknown (Trap 3), never a fabricated all-clear.
"""

from __future__ import annotations

from swebench_eval.evaluation import leak_detection


def test_detect_finds_a_node_id_in_patch() -> None:
    absent = ["django__django/tests/test_filepathfield.py::test_abs"]
    # The model names the exact test it could not have read at base_commit.
    patch = "diff --git a/tests/model_fields/test_filepathfield.py\n" "+def test_abs(self):\n"
    hits = leak_detection.detect_leaked_node_ids(patch, absent)
    assert "django__django/tests/test_filepathfield.py::test_abs" in hits


def test_detect_does_not_match_partial_identifier() -> None:
    # `test_abs` must not match inside `test_absolute` or `test_abstraction`.
    absent = ["tests/test_x.py::test_abs"]
    text = "def test_absolute(): pass\ndef test_abstraction(): pass"
    assert leak_detection.detect_leaked_node_ids(text, absent) == []


def test_detect_ignores_none_and_empty_text() -> None:
    assert leak_detection.detect_leaked_node_ids("", ["a::b"]) == []
    assert leak_detection.detect_leaked_node_ids("anything", None) == []
    assert leak_detection.detect_leaked_node_ids("anything", []) == []


def test_load_leak_detectable_static_artifact(tmp_path) -> None:
    import json

    p = tmp_path / "leak.json"
    p.write_text(json.dumps({"i1": ["tests/a.py::t1"], "i2": ["tests/b.py::t2"]}))
    loaded = leak_detection.load_leak_detectable(p)
    assert loaded["i1"] == ["tests/a.py::t1"]
    assert loaded["i2"] == ["tests/b.py::t2"]


def test_load_leak_detectable_absent_is_unknown_not_clean(tmp_path) -> None:
    # No artifact -> {} (the worker then records leaked_node_ids=None, i.e.
    # unknown — NOT a fabricated all-clear).
    missing = tmp_path / "does-not-exist.json"
    assert leak_detection.load_leak_detectable(missing) == {}


def test_compute_leak_detectable_marks_absent_node_ids() -> None:
    from swebench_eval.dataset.base import Instance

    inst = Instance(
        instance_id="i1",
        repo="repo/x",
        base_commit="abc",
        problem_statement="p",
        fail_to_pass="a::t1\na::t2\na::t3",
    )
    # a::t2 is NOT in the repo at base_commit -> leak-detectable for it.
    probe = lambda repo, commit: {"a::t1", "a::t3"}
    result = leak_detection.compute_leak_detectable([inst], probe)
    assert result["i1"] == ["a::t2"]


# --- B6: diff-parse absence from the gold test_patch (brief §1.2) ----------


def test_leak_detectable_via_test_patch_pytest_parametrized() -> None:
    """A pytest-def added by the test_patch is absent at base (P-4 handles suffix)."""
    test_patch = (
        "diff --git a/astropy/modeling/tests/test_separable.py b/astropy/modeling/tests/test_separable.py\n"
        "--- a/astropy/modeling/tests/test_separable.py\n"
        "+++ b/astropy/modeling/tests/test_separable.py\n"
        "@@ -1,3 +1,4 @@\n"
        " def test_separability():  # context, present at base\n"
        "     pass\n"
        "+def test_separable(c):\n"
        "+    assert c\n"
    )
    fail_to_pass = (
        '["astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]", '
        '"astropy/modeling/tests/test_separable.py::test_separability"]'
    )
    absent = leak_detection.leak_detectable_via_test_patch(test_patch, fail_to_pass)
    # test_separable is added -> absent (leak-detectable); test_separability is
    # context (present) -> not absent.
    assert absent == [
        "astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]"
    ]
    assert not any("test_separability" in a for a in absent)


def test_leak_detectable_via_test_patch_unittest_class() -> None:
    """unittest-style test added in a class def is absent at base."""
    test_patch = (
        "diff --git a/tests/test_filepathfield.py b/tests/test_filepathfield.py\n"
        "--- a/tests/test_filepathfield.py\n"
        "+++ b/tests/test_filepathfield.py\n"
        "@@ -9,0 +10,4 @@\n"
        "+class TestFilePathField:\n"
        "+    def test_abs(self):\n"
        "+        pass\n"
    )
    fail_to_pass = "tests/test_filepathfield.py::test_abs\n"
    assert leak_detection.leak_detectable_via_test_patch(test_patch, fail_to_pass) == [
        "tests/test_filepathfield.py::test_abs"
    ]


def test_leak_detectable_via_test_patch_none_added_is_not_detectable() -> None:
    """A test_patch that adds no tests -> nothing is absent (leak_detectable=False)."""
    # The patch only ADDS an assertion body inside an already-present test_abs
    # (a `+    return 1` line, not a `def`), so no test is newly introduced.
    test_patch = (
        "diff --git a/tests/test_abs.py b/tests/test_abs.py\n"
        "--- a/tests/test_abs.py\n"
        "+++ b/tests/test_abs.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def test_abs():\n"
        "     pass\n"
        "+    assert True\n"
    )
    assert (
        leak_detection.leak_detectable_via_test_patch(test_patch, "tests/test_abs.py::test_abs")
        == []
    )


def test_leak_detectable_format_mismatch_is_unknown_not_all_absent() -> None:
    """P-3: patch adds tests but NO FAIL_TO_PASS id names one -> UNKNOWN (omit)."""
    test_patch = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -1,0 +1,3 @@\n"
        "+def test_added_new():\n"
        "+    pass\n"
    )
    # the FAIL_TO_PASS id is in a format that matches NONE of the added names
    fail_to_pass = "some/weird/format::totally_different_test"
    assert leak_detection.leak_detectable_via_test_patch(test_patch, fail_to_pass) is None


def test_normalize_node_id_strips_parametrized_suffix() -> None:
    """P-4: a parametrized id normalises to the bare test name."""
    assert (
        leak_detection._normalize_node_id(
            "astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]"
        )
        == "test_separable"
    )


def test_parse_fail_to_pass_newline_and_json() -> None:
    assert leak_detection.parse_fail_to_pass("a::t1\na::t2") == ["a::t1", "a::t2"]
    assert leak_detection.parse_fail_to_pass('["a::t1", "a::t2"]') == ["a::t1", "a::t2"]
    assert leak_detection.parse_fail_to_pass("") == []


def test_synthetic_patch_with_absent_node_sets_leaked_node_ids() -> None:
    """DoD #7: a synthetic patch naming an absent node id must set leaked_node_ids."""
    absent_ids = ["tests/test_filepathfield.py::test_abs"]
    patch = (
        "diff --git a/tests/test_filepathfield.py\n"
        "+        def test_abs(self):\n"  # the model reproduced the absent test
    )
    leaked = leak_detection.detect_leaked_node_ids(patch, absent_ids)
    assert leaked == ["tests/test_filepathfield.py::test_abs"]


def test_normalize_node_id_unittest_django_form() -> None:
    """B6 review MUST-2: the unittest/django `test_x (Class)` form must normalise
    to the bare test name.

    This branch moves ~172 Verified instances out of UNKNOWN (the map goes 303 -> 475).
    It is the highest-leverage line in B6, so it must be guarded — deleting it
    (which removes those instances from the map as a silent UNKNOWN re-widening)
    must fail this test rather than pass while the headline number silently drops.
    """
    assert (
        leak_detection._normalize_node_id(
            "test_token_with_different_secret (auth_tests.test_tokens.TokenGeneratorTest)"
        )
        == "test_token_with_different_secret"
    )
    # and a pytest-style id is unaffected
    assert leak_detection._normalize_node_id("repo/mod.py::test_x") == "test_x"
    assert leak_detection._normalize_node_id("repo/mod.py::test_x[params]") == "test_x"


def test_leak_detectable_via_test_patch_django_class_form() -> None:
    """B6 review MUST-2: a django `test_name (module.Class)` FAIL_TO_PASS whose
    test the patch ADDS must be leak-detectable, not UNKNOWN.

    This is the end-to-end behaviour behind the 172-instance move: without the
    unittest/django normalisation, this id's bare name never matches the added
    `def` and the instance lands UNKNOWN (omitted).  Guard the behavioural
    consequence, not just the helper.
    """
    test_patch = (
        "diff --git a/auth_tests/test_tokens.py b/auth_tests/test_tokens.py\n"
        "--- a/auth_tests/test_tokens.py\n"
        "+++ b/auth_tests/test_tokens.py\n"
        "@@ -1,3 +1,4 @@\n"
        " class TokenGeneratorTest:\n"
        "     pass\n"
        "+    def test_token_with_different_secret(self):\n"
        "+        pass\n"
    )
    fail_to_pass = "test_token_with_different_secret (auth_tests.test_tokens.TokenGeneratorTest)"
    assert leak_detection.leak_detectable_via_test_patch(test_patch, fail_to_pass) == [fail_to_pass]
