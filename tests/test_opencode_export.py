"""opencode native-export scoped-export tests (builder 3, DoD B.8; revised 2026-09-08).

The opencode DB holds plaintext OAuth tokens (``account``/``control_account``/
``credential`` tables).  The exporter must only ever touch session/message/part
for one session — never the .db, never .dump.  That scope IS the protection.

The former substring guard (any ``access_token`` in the exported text raised)
was removed after run 6 (django-15278): the MODEL wrote the ticket's
``refreshed_access_token`` field into a test script, the guard fired, the
harness crashed and the patch was lost.  Model text is legitimate export
content — the tests below pin both halves: credential TABLES never reach the
export, and model text mentioning a token-shaped word always does.  An export
failure must never propagate out of ``run()`` (STEP 5.1: the patch is the
deliverable, the export is a log).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from swebench_eval.harnesses.base import ModelConfig
from swebench_eval.harnesses.opencode.harness import _export_native_trajectory


def _make_db(
    path: Path,
    *,
    poison_part: bool = False,
    session_dir: str = "/repo",
) -> str:
    """Build a fixture opencode DB in *path*; returns the session id."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE account (id text PRIMARY KEY, email text NOT NULL,
            url text NOT NULL, access_token text NOT NULL,
            refresh_token text NOT NULL);
        CREATE TABLE session (id text PRIMARY KEY, directory text,
            time_created integer);
        CREATE TABLE message (id text PRIMARY KEY, session_id text,
            time_created integer, data text);
        CREATE TABLE part (id text PRIMARY KEY, message_id text,
            session_id text, time_created integer, data text);
        """)
    sid = "ses_test"
    con.execute("INSERT INTO session VALUES (?, ?, ?)", (sid, session_dir, 1000))
    con.execute(
        "INSERT INTO message VALUES ('m1', ?, 1100, ?)",
        (sid, '{"role":"user","content":"problem"}'),
    )
    part_data = '{"type":"reasoning","text":"17*23 = 391."}'
    if poison_part:
        # what the model actually wrote in run 6 / django-15278: the ticket's
        # OAuth-toolkit field name inside a tool-call text — legitimate content.
        part_data = (
            '{"type":"tool","text":"field = models.OneToOneField(Book, '
            "related_name='refreshed_access_token')\\n"
            'oauth_refresh_token = None"}'
        )
    con.execute("INSERT INTO part VALUES ('p1', 'm1', ?, 1200, ?)", (sid, part_data))
    con.execute("INSERT INTO account VALUES ('a1', 'e@x', 'u', 'sk-account', 'rt')")
    con.commit()
    con.close()
    return sid


def test_export_scope_excludes_credential_tables(tmp_path: Path) -> None:
    """The exported JSONL never contains account credentials (DoD B.8)."""
    db = tmp_path / "opencode.db"
    _make_db(db)
    out = tmp_path / "native.jsonl"
    assert _export_native_trajectory(db, str(out), "/repo") is True
    text = out.read_text()
    assert "access_token" not in text, "export leaked access_token"
    assert "refresh_token" not in text, "export leaked refresh_token"
    # the reasoning part IS exported (that's the point of the file)
    assert "391" in text


def test_export_keeps_model_text_that_mentions_tokens(tmp_path: Path) -> None:
    """Model text containing ``access_token``/``refresh_token`` is exported as-is.

    Regression for run 6 / django-15278 (2026-09-08): the old substring guard
    raised on the model's own test script, crashed the harness and lost the
    patch.  The words are content, not credentials — the scoped SELECT (test
    above) is what keeps the credential tables out.
    """
    db = tmp_path / "opencode.db"
    _make_db(db, poison_part=True)
    out = tmp_path / "native.jsonl"
    assert _export_native_trajectory(db, str(out), "/repo") is True
    text = out.read_text()
    assert "refreshed_access_token" in text
    assert "oauth_refresh_token" in text
    # the account table's actual credential value still never appears
    assert "sk-account" not in text


def test_run_survives_native_export_failure(tmp_path, monkeypatch) -> None:
    """An export crash must not propagate out of run() nor cost the patch (STEP 5.1).

    django-15278 in run 6: the export raised AFTER patch.diff was on disk; the
    exception left run(), the worker classified HARNESS_CRASH and the patch
    was never uploaded.  run() must return the patch with an empty
    native_trajectory_path instead.
    """
    from swebench_eval.harnesses.base import HarnessInput
    from swebench_eval.harnesses.opencode import harness as oc
    from swebench_eval.harnesses.opencode.harness import OpenCodeHarness

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(oc, "ensure_prepared_repo", lambda _p: None)
    monkeypatch.setattr(
        oc,
        "git_diff_or_classify",
        lambda _p: type(
            "D", (), {"patch": "--- a\n+++ b\n", "timed_out": False, "patch_extract_s": 0.0}
        )(),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("export exploded")

    monkeypatch.setattr(oc, "_export_native_trajectory", _boom)
    import urllib.request as _ur

    monkeypatch.setattr(_ur, "urlopen", lambda *a, **k: None)
    monkeypatch.setattr(
        oc,
        "run_streaming",
        lambda cmd, cwd, env, timeout, on_line: type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
        )(),
    )
    inp = HarnessInput(
        instance_id="django__django-15278",
        repo_url="https://github.com/django/django",
        base_commit="c",
        problem_statement="fix it",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(tmp_path),
        timeout_seconds=60,
        model_config=ModelConfig(
            gateway_base_url="http://127.0.0.1:4000/v1",
            gateway_api_key="k",
            model_name="cheap-oss-model",
        ),
    )
    out = OpenCodeHarness(api_base_url="http://127.0.0.1:4000/v1", api_key="k").run(inp)
    assert out.patch == "--- a\n+++ b\n"
    assert out.success is True
    assert out.native_trajectory_path == ""
    assert (tmp_path / "patch.diff").exists()


def test_export_matches_session_by_repo_dir(tmp_path: Path) -> None:
    """Only the session whose directory == repo_dir is exported."""
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE session (id text PRIMARY KEY, directory text,
            time_created integer);
        CREATE TABLE message (id text PRIMARY KEY, session_id text,
            time_created integer, data text);
        CREATE TABLE part (id text PRIMARY KEY, message_id text,
            session_id text, time_created integer, data text);
        """)
    con.execute("INSERT INTO session VALUES ('other', '/other', 500)")
    con.execute("INSERT INTO part VALUES ('o1', 'od', 'other', 600, '{}')")
    con.execute("INSERT INTO session VALUES ('mine', '/repo', 900)")
    con.execute("INSERT INTO part VALUES ('m1', 'md', 'mine', 950, '{\"mine\":1}')")
    con.commit()
    con.close()
    out = tmp_path / "native.jsonl"
    assert _export_native_trajectory(db, str(out), "/repo") is True
    assert "mine" in out.read_text()
    assert "other" not in out.read_text()


def test_run_invokes_write_opencode_json(tmp_path, monkeypatch) -> None:
    """Regression (deep-investigation root cause): OpenCodeHarness.run() MUST
    write opencode.json (the gateway provider config). Commit bfe23d1 deleted
    the _write_opencode_json call while keeping the empty cfg path, so every
    opencode run had NO litellm provider and died UnknownError at loop step=0
    with zero outbound calls. This asserts the call happens, so a silent
    regression can't return."""

    from swebench_eval.harnesses.base import HarnessInput
    from swebench_eval.harnesses.opencode import harness as oc
    from swebench_eval.harnesses.opencode.harness import OpenCodeHarness

    repo = tmp_path / "repo"
    repo.mkdir()
    # ensure_prepared_repo is called first; make it a no-op on a fake repo.
    monkeypatch.setattr(oc, "ensure_prepared_repo", lambda _p: None)
    monkeypatch.setattr(
        oc,
        "git_diff_or_classify",
        lambda _p: type("D", (), {"patch": None, "timed_out": False, "patch_extract_s": 0.0})(),
    )
    monkeypatch.setattr(oc, "_export_native_trajectory", lambda *a, **k: True)
    # stub the reachability probe — CI blocks real network.
    import urllib.request as _ur

    monkeypatch.setattr(_ur, "urlopen", lambda *a, **k: None)

    written = {}

    def _fake_run_streaming(cmd, cwd, env, timeout, on_line):
        # Capture the config the harness wrote.
        xdg = env.get("XDG_CONFIG_HOME")
        import json as _j

        if xdg:
            p = Path(xdg) / "opencode" / "opencode.json"
            if p.exists():
                written["cfg"] = _j.loads(p.read_text())
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False})()

    monkeypatch.setattr(oc, "run_streaming", _fake_run_streaming)

    inp = HarnessInput(
        instance_id="scikit-learn__scikit-learn-25102",
        repo_url="https://github.com/scikit-learn/scikit-learn",
        base_commit="c",
        problem_statement="fix it",
        attempt_number=1,
        repo_checkout_path=str(repo),
        output_dir=str(tmp_path),
        timeout_seconds=60,
        model_config=ModelConfig(
            gateway_base_url="http://127.0.0.1:4000/v1",
            gateway_api_key="k",
            model_name="cheap-oss-model",
        ),
    )
    OpenCodeHarness(api_base_url="http://127.0.0.1:4000/v1", api_key="k").run(inp)
    assert written.get("cfg"), "opencode.json was NOT written — the provider config call is missing"
    # the provider must point at the gateway base URL
    prov = written["cfg"]["provider"]["litellm"]
    assert prov["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
