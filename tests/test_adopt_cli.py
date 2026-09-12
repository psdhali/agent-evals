"""scripts/adopt.py — the pure helpers behind the Makefile (adoption Phase 1c)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import adopt


def _plan(*changes: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "resource_changes": [{"address": a, "change": {"actions": acts}} for a, acts in changes]
    }


def test_plan_summary_counts_each_action_kind() -> None:
    s = adopt.plan_summary(
        _plan(
            ("module.cache.aws_x.a", ["delete"]),
            ("module.gateway.aws_y.b", ["create"]),
            ("module.ui.aws_z.c", ["update"]),
            ("module.ui.aws_z.d", ["delete", "create"]),
            ("data.aws_caller_identity.current", ["read"]),
            ("module.ui.aws_z.e", ["no-op"]),
        )
    )
    assert (s.add, s.change, s.destroy, s.replace) == (1, 1, 1, 1)
    assert s.actions["module.ui.aws_z.d"] == {"delete", "create"}


def test_pure_destroy_rejects_any_add_change_or_replace() -> None:
    assert adopt.is_pure_destroy(
        adopt.plan_summary(_plan(("m.a", ["delete"]), ("m.b", ["delete"])))
    )
    assert not adopt.is_pure_destroy(
        adopt.plan_summary(_plan(("m.a", ["delete"]), ("m.b", ["create"])))
    )
    assert not adopt.is_pure_destroy(adopt.plan_summary(_plan(("m.a", ["update"]))))
    assert not adopt.is_pure_destroy(adopt.plan_summary(_plan(("m.a", ["delete", "create"]))))
    assert adopt.is_pure_destroy(adopt.plan_summary(_plan()))  # nothing to do is fine


def test_terraform_version_gate() -> None:
    assert adopt._terraform_version_ok("Terraform v1.13.3")
    assert adopt._terraform_version_ok("Terraform v1.11.0\non darwin_arm64")
    assert not adopt._terraform_version_ok("Terraform v1.9.8")
    assert not adopt._terraform_version_ok(None)


def test_setup_yaml_is_flat_key_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "setup.yaml"
    f.write_text(
        "# comment\nregion: us-east-2\nname_prefix: acme\naws_profile:\n"
        'tfstate_bucket: "b"   # trailing\nnotification_email: a@b.c\n'
    )
    monkeypatch.setattr(adopt, "SETUP_FILE", f)
    cfg = adopt.load_setup()
    assert cfg == {
        "region": "us-east-2",
        "name_prefix": "acme",
        "aws_profile": "",
        "tfstate_bucket": "b",
        "notification_email": "a@b.c",
    }


def test_render_backend_and_tfvars() -> None:
    cfg = {
        "region": "us-east-2",
        "name_prefix": "acme",
        "aws_profile": "",
        "tfstate_bucket": "acme-tfstate",
        "master_database_password": "pw",
        "litellm_master_key": "sk-m",
        "openrouter_api_key": "sk-or",
        "notification_email": "a@b.c",
        "reviewer_role_name": "",
    }
    assert adopt.render_backend_hcl(cfg) == 'bucket  = "acme-tfstate"\nregion  = "us-east-2"\n'
    cfg["aws_profile"] = "acme-admin"
    assert 'profile = "acme-admin"' in adopt.render_backend_hcl(cfg)
    persistent = adopt.render_tfvars(cfg, "persistent")
    assert 'region = "us-east-2"' in persistent
    assert 'aws_profile = "acme-admin"' in persistent
    assert 'notification_email = "a@b.c"' in persistent
    assert 'master_database_password = "pw"' in persistent
    ui = adopt.render_tfvars(cfg, "ui")
    assert "master_database_password" not in ui and 'tfstate_bucket = "acme-tfstate"' in ui
    assert "reviewer_role_name" not in adopt.render_tfvars(cfg, "eval")
    cfg["reviewer_role_name"] = "ro"
    assert 'reviewer_role_name = "ro"' in adopt.render_tfvars(cfg, "eval")
