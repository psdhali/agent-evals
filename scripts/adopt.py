#!/usr/bin/env python3
"""The operator CLI behind the top-level Makefile (adoption Phase 1c, 2026-09-11).

Every step of docs/SETUP.md that used to be a runbook paragraph is a subcommand
here, so an adopter never opens a runbook:

    doctor      tools, credentials, region, quotas, secrets, state bucket, Hugging Face,
                OpenRouter — pass/fail table, non-zero exit on any FAIL
    bootstrap   state bucket + backend.hcl + terraform.tfvars for every root + the two
                out-of-band secrets + terraform init, all from setup.yaml (idempotent)
    up          ui -> eval (families regenerated first) -> ui again -> persistent
                (dispatch Lambda picks up Valkey) -> roll the three services -> verify
    down        export credentials first, then eval -> ui destroy (each plan checked to be
                a pure destroy of its own root), drain the eval hosts out of their
                termination hook, verify, re-enable the nightly guard
    roll        move orchestrator-api / run-supervisor / results-writer to their latest
                task-definition revision and wait until they run it
    verify      the bring-up checks: services at desired count, families == images
    images      the per-instance image pipeline for a subset or the whole split:
                seed dataset -> snapshot check -> build tier up -> builder task ->
                manifest -> families -> build tier down; prints elapsed time + ECR bytes
    gate        POST /images/validate for the instances (the gold gate) via the tunnel
    tunnel      the SSM port-forward to the API (localhost:8000)
    discover    POST /model-ceilings/<alias>/discover and wait for the result
    pin         register the git-tracked model specs (provider pins) as gateway db-models
    purge       drain stranded queue messages before a first run
    nat         switch private-subnet egress between the NAT gateway and the t3 instance
    guard       pause / resume the nightly scale-to-zero rule

Reads the deployment identity from the environment (AWS_PROFILE, AWS_REGION,
EVAL_ENV_PREFIX) with the same defaults Terraform's settings.tf carries, so the
Makefile, Terraform and the containers agree.  Terraform is driven through
`plan -out` + `apply <plan>`; a plan is shown and confirmed unless --yes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swebench_eval import aws_names

ROOT = Path(__file__).resolve().parent.parent
TF_ROOT = ROOT / "infra" / "terraform" / "envs" / "dev"
TERRAFORM = os.environ.get(
    "TERRAFORM", shutil.which("terraform") or str(Path.home() / "bin" / "terraform")
)
UI_SERVICES = ("orchestrator-api", "run-supervisor", "results-writer")
EVAL_SERVICES = ("gateway", "eval-worker", "harness-dispatcher", "git-mirror")
API_URL = os.environ.get("EVAL_API_URL", "http://localhost:8000")
SETUP_FILE = ROOT / "setup.yaml"
MIN_TERRAFORM = (1, 11)


# ── small helpers ─────────────────────────────────────────────────────────────


def _profile() -> str | None:
    p = os.environ.get("AWS_PROFILE", "").strip()
    return p or None


def _aws_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("AWS_REGION", aws_names.region())
    env.setdefault("AWS_DEFAULT_REGION", env["AWS_REGION"])
    return env


def _run(
    cmd: list[str], *, cwd: Path | None = None, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, check=check, text=True, capture_output=capture, env=_aws_env()
    )


def _aws(*args: str, parse: bool = True) -> Any:
    """`aws ... --output json` parsed (or raw text with parse=False)."""
    cmd = ["aws", *args, "--output", "json" if parse else "text"]
    if _profile():
        cmd += ["--profile", _profile() or ""]
    cmd += ["--region", aws_names.region()]
    out = _run(cmd, capture=True).stdout
    if not parse:
        return out.strip()
    # Some list calls print NOTHING for an empty result (budgets describe-budgets on an
    # account without budgets — the sweep's last call, found on the first empty account):
    # an empty dict, never an empty string, so callers can .get() without checking.
    return json.loads(out) if out.strip() else {}


def _named(suffix: str) -> str:
    return aws_names.named(suffix)


def _say(msg: str) -> None:
    print(f"== {msg}", flush=True)


def _confirm(prompt: str, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise SystemExit(f"{prompt}: refusing without --yes on a non-interactive terminal")
    ans = input(f"{prompt} [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        raise SystemExit("aborted")


# ── terraform ─────────────────────────────────────────────────────────────────


@dataclass
class PlanSummary:
    add: int = 0
    change: int = 0
    destroy: int = 0
    replace: int = 0
    foreign: list[str] = field(
        default_factory=list
    )  # addresses outside module./resource of the root
    actions: dict[str, set[str]] = field(default_factory=dict)  # address -> actions


def plan_summary(plan_json: dict[str, Any]) -> PlanSummary:
    """Summarise `terraform show -json <plan>`: counts + every address with its actions."""
    s = PlanSummary()
    for rc in plan_json.get("resource_changes", []) or []:
        actions = set(rc.get("change", {}).get("actions", []))
        addr = str(rc.get("address", ""))
        s.actions[addr] = actions
        if actions == {"no-op"} or actions == {"read"}:
            continue
        if "create" in actions and "delete" in actions:
            s.replace += 1
        elif "create" in actions:
            s.add += 1
        elif "delete" in actions:
            s.destroy += 1
        elif "update" in actions:
            s.change += 1
    return s


def is_pure_destroy(summary: PlanSummary) -> bool:
    """A destroy plan may only delete (no add, no change, no replace)."""
    return summary.add == 0 and summary.change == 0 and summary.replace == 0


def _tf(root: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return _run([TERRAFORM, *args], cwd=TF_ROOT / root, capture=capture)


def _tf_init(root: str) -> None:
    backend = TF_ROOT / root / "backend.hcl"
    if not backend.exists():
        raise SystemExit(f"{backend} missing — run `make bootstrap` first")
    _tf(root, "init", "-input=false", "-reconfigure", f"-backend-config={backend}", capture=True)


def _state_exists(root: str) -> bool:
    """Whether *root*'s state OBJECT exists in the tfstate bucket (Phase 2 finding: a
    terraform_remote_state read of a missing object errors; try() cannot guard it)."""
    bucket = _tfstate_bucket()
    if not bucket:
        return False
    try:
        _aws(
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            f"envs/dev/{root}/terraform.tfstate",
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _cross_tier_vars(root: str) -> list[str]:
    """-var flags telling *root* which other tiers' states it may read right now."""
    flags: list[str] = []
    if root in ("persistent", "ui"):
        flags.append(f"-var=read_eval_state={'true' if _state_exists('eval') else 'false'}")
    if root == "eval":
        flags.append(f"-var=read_ui_state={'true' if _state_exists('ui') else 'false'}")
    return flags


def _tf_plan(root: str, *extra: str, destroy: bool = False) -> tuple[Path, PlanSummary]:
    out = Path(tempfile.mkdtemp(prefix=f"tf-{root}-")) / "plan.tfplan"
    args = ["plan", "-input=false", f"-out={out}", *_cross_tier_vars(root), *extra]
    if destroy:
        args.append("-destroy")
    _tf(root, *args)
    shown = _tf(root, "show", "-json", str(out), capture=True).stdout
    return out, plan_summary(json.loads(shown))


def _tf_apply_plan(root: str, plan: Path, summary: PlanSummary, yes: bool, label: str) -> None:
    _say(
        f"{label}: {summary.add} to add, {summary.change} to change, {summary.destroy} to destroy, {summary.replace} to replace"
    )
    if summary.add == summary.change == summary.destroy == summary.replace == 0:
        _say(f"{label}: nothing to do")
        return
    _confirm(f"apply the {label} plan above?", yes)
    _tf(root, "apply", "-input=false", str(plan))


def _tf_output(root: str, name: str) -> Any:
    out = _tf(root, "output", "-json", name, capture=True).stdout
    return json.loads(out) if out.strip() else None


# ── doctor ────────────────────────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False


def _tool_version(cmd: list[str]) -> str | None:
    try:
        return (
            subprocess.run(cmd, capture_output=True, text=True, check=False)
            .stdout.strip()
            .splitlines()[0]
        )
    except (OSError, IndexError):
        return None


def _terraform_version_ok(text: str | None) -> bool:
    if not text:
        return False
    import re

    m = re.search(r"v?(\d+)\.(\d+)", text)
    if m is None:
        return False
    return (int(m.group(1)), int(m.group(2))) >= MIN_TERRAFORM


def _service_quota(service: str, code: str) -> float | None:
    try:
        q = _aws(
            "service-quotas", "get-service-quota", "--service-code", service, "--quota-code", code
        )
        return float(q["Quota"]["Value"])
    except Exception:  # noqa: BLE001 - quota reads are best-effort diagnostics
        return None


def doctor(args: argparse.Namespace) -> int:
    checks: list[Check] = []
    for name, cmd, ok_fn in (
        ("uv", ["uv", "--version"], bool),
        ("docker", ["docker", "--version"], bool),
        ("docker compose", ["docker", "compose", "version"], bool),
        ("terraform >= 1.11", [TERRAFORM, "version"], _terraform_version_ok),
        ("aws cli v2", ["aws", "--version"], lambda v: bool(v) and "aws-cli/2" in v),
        ("session-manager-plugin", ["session-manager-plugin", "--version"], bool),
        ("git", ["git", "--version"], bool),
        ("pre-commit", ["pre-commit", "--version"], bool),
    ):
        v = _tool_version(cmd)
        checks.append(Check(name, bool(ok_fn(v)), v or "not found"))
    try:
        _run(["docker", "info"], capture=True)
        checks.append(Check("docker daemon", True, "reachable"))
    except subprocess.CalledProcessError:
        checks.append(Check("docker daemon", False, "not reachable — is Docker running?"))

    # AWS identity + region
    try:
        ident = _aws("sts", "get-caller-identity")
        checks.append(Check("aws identity", True, f"{ident['Arn']} (account {ident['Account']})"))
        os.environ.setdefault("AWS_ACCOUNT_ID", ident["Account"])
        aws_names.account_id.cache_clear()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check(
                "aws identity",
                False,
                f"{exc} — `aws sso login --profile {_profile() or '<profile>'}`?",
            )
        )
        _print_checks(checks)
        return 1
    checks.append(Check("region", True, aws_names.region()))
    checks.append(Check("name prefix", True, aws_names.name_prefix()))

    # State bucket (from persistent's backend.hcl or setup.yaml)
    bucket = _tfstate_bucket()
    if bucket:
        try:
            _aws("s3api", "head-bucket", "--bucket", bucket)
            checks.append(Check("tfstate bucket", True, bucket))
        except Exception:  # noqa: BLE001
            checks.append(
                Check("tfstate bucket", False, f"{bucket} not found — `make bootstrap` creates it")
            )
    else:
        checks.append(
            Check("tfstate bucket", False, "no backend.hcl / setup.yaml yet — `make bootstrap`")
        )

    # Out-of-band secrets
    for name, shape in (
        (_named("openrouter-management"), "a plain string (the OpenRouter provisioning key)"),
        (
            f"ecr-pullthroughcache/{aws_names.name_prefix()}-dockerhub",
            '{"username": ..., "accessToken": ...}',
        ),
    ):
        try:
            v = _aws("secretsmanager", "get-secret-value", "--secret-id", name)
            s = str(v.get("SecretString", ""))
            if name.startswith("ecr-pullthroughcache/"):
                d = json.loads(s)
                ok = bool(d.get("username")) and bool(d.get("accessToken"))
            else:
                ok = len(s) > 10
            checks.append(
                Check(f"secret {name}", ok, "present" if ok else f"wrong shape, expected {shape}")
            )
        except Exception:  # noqa: BLE001
            checks.append(
                Check(f"secret {name}", False, f"missing — `make bootstrap` creates it ({shape})")
            )

    # Quotas (best-effort). A fresh account starts at 6 Fargate vCPU / 5 Spot vCPU /
    # 5 On-Demand vCPU (measured 2026-09-11): the control-plane services alone need more.
    for label, service, code, need in quota_needs(args.scale):
        have = _service_quota(service, code)
        if have is None:
            checks.append(
                Check(f"quota {label}", True, "could not read (service-quotas)", warn=True)
            )
        else:
            checks.append(Check(f"quota {label}", have >= need, f"{have:g} (need ≥ {need:g})"))

    # Hugging Face: the pinned revision resolves
    from swebench_eval.dataset.swebench_loader import _DATASET_NAME, _PINNED_REVISION

    url = f"https://huggingface.co/api/datasets/{_DATASET_NAME}/revision/{_PINNED_REVISION}"
    checks.append(_http_check("huggingface dataset @ pinned revision", url))

    # OpenRouter inference key (from .env)
    key = _dotenv_value("OPENROUTER_API_KEY")
    if key:
        checks.append(
            _http_check(
                "openrouter key",
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
            )
        )
    else:
        checks.append(Check("openrouter key", False, "OPENROUTER_API_KEY not in .env"))

    _print_checks(checks)
    return 0 if all(c.ok for c in checks) else 1


QUOTA_NEEDS: dict[str, list[tuple[str, str, str, float]]] = {
    # (label, service code, quota code, vCPU needed) — "small" = a 10-instance validation,
    # "full" = the 500-instance split at the dispatcher's static cap.
    "small": [
        # Finding 22 (clean pass): 24, not 32. Measured on the task definitions: the seven
        # services take 6 vCPU (gateway 2 × 2, results-writer 2 × 0.5, four at 0.25), each
        # agent task 1, a judge or discovery task 1 — 17 for ten instances, 24 leaves room for
        # task churn. AWS auto-approved the first pass's raise at 30, and the 10-instance run
        # graded fine on it; a doctor demanding 32 would have parked a stranger on an open case.
        ("Fargate On-Demand vCPU", "fargate", "L-3032A538", 24.0),
        # Finding 17: 32 = exactly four 8-vCPU hosts; hosts parked in the ECS draining hook
        # still count, so any overlap between draining and launching stalls at 32. 64 gives
        # one host-set of headroom for a 10-instance validation.
        ("EC2 Spot vCPU (standard)", "ec2", "L-34B43A08", 64.0),
        ("EC2 On-Demand vCPU (standard)", "ec2", "L-1216C47A", 8.0),
        ("Lambda concurrent executions", "lambda", "L-B99A9384", 10.0),
        ("Elastic IPs", "ec2", "L-0263D0A3", 2.0),
    ],
    "full": [
        ("Fargate On-Demand vCPU", "fargate", "L-3032A538", 145.0),
        ("EC2 Spot vCPU (standard)", "ec2", "L-34B43A08", 192.0),
        ("EC2 On-Demand vCPU (standard)", "ec2", "L-1216C47A", 32.0),
        ("Lambda concurrent executions", "lambda", "L-B99A9384", 10.0),
        ("Elastic IPs", "ec2", "L-0263D0A3", 2.0),
    ],
}


def quota_needs(scale: str) -> list[tuple[str, str, str, float]]:
    return QUOTA_NEEDS["full" if scale == "full" else "small"]


def quotas(args: argparse.Namespace) -> int:
    """Request every quota below its need (service-quotas), then list the request history."""
    for label, service, code, need in quota_needs(args.scale):
        have = _service_quota(service, code)
        if have is None:
            print(f"   {label}: could not read; request it in the console (code {code})")
            continue
        if have >= need:
            print(f"   {label}: {have:g} >= {need:g}, nothing to request")
            continue
        try:
            out = _aws(
                "service-quotas",
                "request-service-quota-increase",
                "--service-code",
                service,
                "--quota-code",
                code,
                "--desired-value",
                str(need),
            )
            print(f"   {label}: {have:g} -> requested {need:g} ({out['RequestedQuota']['Status']})")
        except subprocess.CalledProcessError as exc:
            print(
                f"   {label}: request failed ({exc.stderr.strip()[:200]}) — open a case in the console"
            )
    hist = _aws("service-quotas", "list-requested-service-quota-change-history")
    for r in hist.get("RequestedQuotas", []):
        print(f"   history: {r['QuotaName']}: {r['DesiredValue']:g} {r['Status']}")
    _say(
        "increases are usually approved within minutes to a day; `make doctor` shows when they land"
    )
    return 0


def _http_check(name: str, url: str, headers: dict[str, str] | None = None) -> Check:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return Check(name, 200 <= r.status < 300, f"HTTP {r.status}")
    except urllib.error.HTTPError as exc:
        return Check(name, False, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, str(exc))


def _dotenv_value(key: str) -> str:
    p = ROOT / ".env"
    if not p.exists():
        return os.environ.get(key, "")
    for line in p.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"')
    return os.environ.get(key, "")


def _print_checks(checks: list[Check]) -> None:
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "OK  " if c.ok and not c.warn else ("WARN" if c.ok else "FAIL")
        print(f"{mark}  {c.name.ljust(width)}  {c.detail}")
    fails = [c for c in checks if not c.ok]
    print(
        f"\n{len(checks) - len(fails)}/{len(checks)} checks passed"
        + (f", {len(fails)} FAILED" if fails else "")
    )


def _tfstate_bucket() -> str:
    hcl = TF_ROOT / "persistent" / "backend.hcl"
    if hcl.exists():
        for line in hcl.read_text().splitlines():
            if line.strip().startswith("bucket"):
                return line.split("=", 1)[1].strip().strip('"')
    cfg = load_setup()
    return str(cfg.get("tfstate_bucket", "")) if cfg else ""


# ── bootstrap ─────────────────────────────────────────────────────────────────


def load_setup() -> dict[str, Any]:
    """setup.yaml — a flat key: value file (no YAML library needed)."""
    if not SETUP_FILE.exists():
        return {}
    out: dict[str, Any] = {}
    for raw in SETUP_FILE.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def render_tfvars(cfg: dict[str, Any], root: str) -> str:
    """The terraform.tfvars for *root* from setup.yaml."""
    ident = {
        "region": cfg["region"],
        "aws_profile": cfg.get("aws_profile", ""),
        "name_prefix": cfg["name_prefix"],
        "tfstate_bucket": cfg["tfstate_bucket"],
    }
    lines = [f'{k} = "{v}"' for k, v in ident.items()]
    if root == "persistent":
        for k in (
            "master_database_password",
            "litellm_master_key",
            "openrouter_api_key",
            "notification_email",
        ):
            lines.append(f'{k} = "{cfg[k]}"')
    if root == "eval" and cfg.get("reviewer_role_name"):
        lines.append(f'reviewer_role_name = "{cfg["reviewer_role_name"]}"')
    return "\n".join(lines) + "\n"


def render_backend_hcl(cfg: dict[str, Any]) -> str:
    lines = [f'bucket  = "{cfg["tfstate_bucket"]}"', f'region  = "{cfg["region"]}"']
    if cfg.get("aws_profile"):
        lines.append(f'profile = "{cfg["aws_profile"]}"')
    return "\n".join(lines) + "\n"


def bootstrap(args: argparse.Namespace) -> int:
    cfg = load_setup()
    required = (
        "region",
        "name_prefix",
        "tfstate_bucket",
        "notification_email",
        "master_database_password",
        "litellm_master_key",
        "openrouter_api_key",
    )
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise SystemExit(f"setup.yaml is missing {missing} (copy setup.yaml.example)")
    os.environ["AWS_REGION"] = cfg["region"]
    os.environ["EVAL_ENV_PREFIX"] = cfg["name_prefix"]
    if cfg.get("aws_profile"):
        os.environ["AWS_PROFILE"] = cfg["aws_profile"]
    bucket = cfg["tfstate_bucket"]

    _say(f"state bucket {bucket}")
    try:
        _aws("s3api", "head-bucket", "--bucket", bucket)
        print("   exists")
    except Exception:  # noqa: BLE001
        if args.dry_run:
            print("   would create")
        else:
            create = ["s3api", "create-bucket", "--bucket", bucket]
            if cfg["region"] != "us-east-1":
                create += ["--create-bucket-configuration", f"LocationConstraint={cfg['region']}"]
            _aws(*create)
            _aws(
                "s3api",
                "put-bucket-versioning",
                "--bucket",
                bucket,
                "--versioning-configuration",
                "Status=Enabled",
            )
            _aws(
                "s3api",
                "put-bucket-encryption",
                "--bucket",
                bucket,
                "--server-side-encryption-configuration",
                '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}',
            )
            _aws(
                "s3api",
                "put-public-access-block",
                "--bucket",
                bucket,
                "--public-access-block-configuration",
                "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
            )
            print("   created (versioned, AES256, public access blocked)")

    _say("backend.hcl + terraform.tfvars for every root")
    for root in ("persistent", "ui", "eval", "build"):
        d = TF_ROOT / root
        for name, content in (
            ("backend.hcl", render_backend_hcl(cfg)),
            ("terraform.tfvars", render_tfvars(cfg, root)),
        ):
            p = d / name
            if args.dry_run:
                print(f"   would write {p.relative_to(ROOT)}")
                continue
            if p.exists() and p.read_text() != content:
                p.with_suffix(p.suffix + ".bak").write_text(p.read_text())
            p.write_text(content)
            print(f"   wrote {p.relative_to(ROOT)}")

    _say("out-of-band secrets")
    secrets = (
        (_named("openrouter-management"), cfg.get("openrouter_provisioning_key", ""), False),
        (
            f"ecr-pullthroughcache/{cfg['name_prefix']}-dockerhub",
            json.dumps(
                {
                    "username": cfg.get("dockerhub_username", ""),
                    "accessToken": cfg.get("dockerhub_token", ""),
                }
            ),
            True,
        ),
    )
    for name, value, is_json in secrets:
        present = True
        try:
            _aws("secretsmanager", "describe-secret", "--secret-id", name)
        except Exception:  # noqa: BLE001
            present = False
        if present:
            print(f"   {name}: exists (left as is)")
            continue
        if not value or (is_json and not all(json.loads(value).values())):
            print(
                f"   {name}: NOT created — fill openrouter_provisioning_key / dockerhub_* in setup.yaml"
            )
            continue
        if args.dry_run:
            print(f"   would create {name}")
            continue
        _aws("secretsmanager", "create-secret", "--name", name, "--secret-string", value)
        print(f"   created {name}")

    if args.dry_run:
        return 0
    _say("terraform init (all roots)")
    for root in ("persistent", "ui", "eval", "build"):
        _tf_init(root)
        print(f"   {root}: initialised")
    _say(
        "bootstrap done — next: `make up-persistent`, then `make service-images`, `make images SUBSET=10`, `make up`"
    )
    return 0


# ── up / down / roll / verify ─────────────────────────────────────────────────


def _families() -> None:
    _say("regenerating the harness task families from the cache manifest")
    _run([sys.executable, str(ROOT / "scripts" / "gen_harness_task_families.py")])


def up(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    if args.persistent:
        # `make up-persistent` = the persistent tier ONLY (SETUP.md step 6). The session
        # tiers come later with `make up`, after the images exist and the quotas are in.
        #
        # Phase 2 finding 5: the dispatch Lambda in this tier is created FROM the ECR image
        # <prefix>-dispatch:latest, and the ECR repositories are created by this same tier —
        # on a fresh account neither exists. So: repositories first (targeted), the service
        # images if the dispatch image is absent, then everything else.
        _tf_init("persistent")
        if not _ecr_repo_exists(_named("dispatch")):
            plan, s = _tf_plan("persistent", "-target=module.ecr")
            _tf_apply_plan("persistent", plan, s, args.yes, "persistent (ECR repositories first)")
        if not _ecr_image_exists(_named("dispatch"), "latest"):
            _say(
                "the service images are not in ECR yet — building/pushing them (scripts/docker_build_push.sh)"
            )
            _service_images()
        plan, s = _tf_plan("persistent")
        _tf_apply_plan("persistent", plan, s, args.yes, "persistent")
        _say(f"persistent tier applied in {(time.monotonic() - t0) / 60:.1f} min")
        _say(
            "next: confirm the SNS subscription email AWS just sent, then `make images SUBSET=10`, then `make up`"
        )
        return 0
    _tf_init("ui")
    plan, s = _tf_plan("ui")
    _tf_apply_plan("ui", plan, s, args.yes, "ui (first pass)")
    if not args.skip_families:
        _families()
    _tf_init("eval")
    plan, s = _tf_plan("eval")
    _tf_apply_plan("eval", plan, s, args.yes, "eval")
    plan, s = _tf_plan("ui")
    _tf_apply_plan("ui", plan, s, args.yes, "ui (second pass: Valkey endpoint + gateway URL)")
    # eval's api tunnel document needs ui's ALB; persistent's dispatch Lambda needs eval's Valkey.
    plan, s = _tf_plan(
        "eval", "-target=aws_ssm_document.api_tunnel", "-target=aws_iam_policy.reviewer_tunnel"
    )
    _tf_apply_plan("eval", plan, s, args.yes, "eval (api tunnel document)")
    _tf_init("persistent")
    plan, s = _tf_plan("persistent", "-target=module.dispatch")
    _tf_apply_plan("persistent", plan, s, args.yes, "persistent (dispatch Lambda REDIS_URL)")
    roll(args)
    rc = verify(args)
    _say(f"bring-up finished in {(time.monotonic() - t0) / 60:.1f} min")
    return rc


def _ecr_repo_exists(repo: str) -> bool:
    try:
        _aws("ecr", "describe-repositories", "--repository-names", repo)
        return True
    except subprocess.CalledProcessError:
        return False


def _ecr_image_exists(repo: str, tag: str) -> bool:
    try:
        out = _aws(
            "ecr", "describe-images", "--repository-name", repo, "--image-ids", f"imageTag={tag}"
        )
        return bool(out.get("imageDetails"))
    except subprocess.CalledProcessError:
        return False


def _service_images() -> None:
    """Build + push the seven service images; push-only when every :latest is already local.

    Clean-pass finding 26: "already local" is not "built from HEAD". The build script now
    compares each local image's baked FRAMEWORK_SHA with HEAD under --push-only and rebuilds
    the ones that differ, so the :<sha> tag it pushes is always the content it names.
    """
    registry = aws_names.ecr_registry()
    local = all(
        subprocess.run(
            ["docker", "image", "inspect", f"{registry}/{_named(img)}:latest"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
        for img in (
            "orchestrator",
            "harness-worker",
            "eval-worker",
            "warm-job",
            "gateway",
            "dispatch",
        )
    )
    cmd = ["bash", str(ROOT / "scripts" / "docker_build_push.sh")] + (
        ["--push-only"] if local else []
    )
    _run(cmd, cwd=ROOT)


def _configure_get(key: str) -> str:
    """`aws configure get <key>` for the active profile ('' when unset)."""
    r = subprocess.run(
        ["aws", "configure", "get", key, "--profile", _profile() or ""],
        capture_output=True,
        text=True,
        check=False,
        env=_aws_env(),
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _export_credentials() -> None:
    """Export the profile's cached credentials into a temporary config so a destroy that
    outlives an SSO token keeps working (found live 2026-09-10)."""
    if not _profile():
        return
    creds: dict[str, Any] | None = None
    # Finding 20: `export-credentials` hands back the CLI's *cached* role session, however
    # little of its hour is left (a teardown launched 35 min after the first command of the
    # session got 25 min of credentials for a 30-min destroy). A role profile is re-assumed
    # for a fresh hour instead; a plain profile falls through to the cached export.
    role_arn = _configure_get("role_arn")
    if role_arn:
        source = _configure_get("source_profile")
        cmd = ["aws", "sts", "assume-role", "--role-arn", role_arn]
        cmd += ["--role-session-name", "adopt", "--duration-seconds", "3600"]
        cmd += ["--output", "json"]
        if source:
            cmd += ["--profile", source]
        try:
            creds = json.loads(_run(cmd, capture=True).stdout)["Credentials"]
        except (subprocess.CalledProcessError, ValueError, KeyError):
            creds = None
    if creds is None:
        try:
            creds = json.loads(
                _run(
                    [
                        "aws",
                        "configure",
                        "export-credentials",
                        "--profile",
                        _profile() or "",
                        "--format",
                        "process",
                    ],
                    capture=True,
                ).stdout
            )
        except (subprocess.CalledProcessError, ValueError):
            _say("could not export cached credentials; continuing with the live session")
            return
    assert creds is not None
    d = Path(tempfile.mkdtemp(prefix="aws-creds-"))
    (d / "credentials").write_text(
        f"[{_profile()}]\naws_access_key_id = {creds['AccessKeyId']}\naws_secret_access_key = {creds['SecretAccessKey']}\n"
        + (f"aws_session_token = {creds['SessionToken']}\n" if creds.get("SessionToken") else "")
    )
    (d / "config").write_text(f"[profile {_profile()}]\nregion = {aws_names.region()}\n")
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(d / "credentials")
    os.environ["AWS_CONFIG_FILE"] = str(d / "config")
    _say(f"cached credentials exported (valid until {creds.get('Expiration', '?')})")


def _drain_asg(name: str, timeout_s: int = 900) -> None:
    """Scale an EC2 Auto Scaling group to zero and release every host from its termination
    lifecycle hook, waiting until the group is empty.

    Phase 2 finding 19: the eval hosts terminate through the ECS managed-draining hook
    (heartbeat 3600 s) and, once the eval-worker service is gone, nothing completes it —
    run-supervisor (which releases drained hosts, finding 17) lives in the ui tier and is
    destroyed first, and the persistent destroy removes the capacity provider before the
    group. Terraform's own drain wait is 10 min, so the persistent destroy failed with the
    four hosts still in Terminating:Wait and the cluster refusing to delete. Idle hosts in
    that state are also billed until the hook times out, which is why `make down` drains
    too. The group itself stays (it belongs to the persistent tier); only its hosts go.
    """
    try:
        groups = _aws(
            "autoscaling",
            "describe-auto-scaling-groups",
            "--auto-scaling-group-names",
            name,
        ).get("AutoScalingGroups", [])
    except subprocess.CalledProcessError:
        return
    if not groups:
        return
    _aws(
        "autoscaling",
        "update-auto-scaling-group",
        "--auto-scaling-group-name",
        name,
        "--min-size",
        "0",
        "--desired-capacity",
        "0",
    )
    hooks = [
        str(h["LifecycleHookName"])
        for h in _aws(
            "autoscaling", "describe-lifecycle-hooks", "--auto-scaling-group-name", name
        ).get("LifecycleHooks", [])
        if str(h.get("LifecycleTransition", "")).endswith("TERMINATING")
    ]
    deadline = time.monotonic() + timeout_s
    released: set[str] = set()
    while True:
        instances = _aws(
            "autoscaling",
            "describe-auto-scaling-groups",
            "--auto-scaling-group-names",
            name,
        )["AutoScalingGroups"][0].get("Instances", [])
        if not instances:
            _say(f"{name}: no hosts left" + (f" ({len(released)} released)" if released else ""))
            return
        for inst in instances:
            iid = str(inst["InstanceId"])
            if not str(inst.get("LifecycleState", "")).startswith("Terminating:Wait"):
                continue
            for hook in hooks:
                try:
                    _aws(
                        "autoscaling",
                        "complete-lifecycle-action",
                        "--auto-scaling-group-name",
                        name,
                        "--lifecycle-hook-name",
                        hook,
                        "--instance-id",
                        iid,
                        "--lifecycle-action-result",
                        "CONTINUE",
                    )
                except subprocess.CalledProcessError:
                    pass  # already past the hook — the next describe shows it
            if iid not in released:
                released.add(iid)
                print(f"   {name}: released {iid} from its termination hook")
        if time.monotonic() > deadline:
            states = ", ".join(f"{i['InstanceId']}={i.get('LifecycleState')}" for i in instances)
            raise SystemExit(f"{name}: still has hosts after {timeout_s}s: {states}")
        time.sleep(15)


def down(args: argparse.Namespace) -> int:
    _export_credentials()
    for root in ("eval", "ui"):
        _tf_init(root)
        plan, s = _tf_plan(root, destroy=True)
        if not is_pure_destroy(s):
            raise SystemExit(
                f"{root}: destroy plan is not a pure destroy ({s.add} add / {s.change} change / {s.replace} replace) — stop and look"
            )
        _tf_apply_plan(root, plan, s, args.yes, f"{root} DESTROY")
    _drain_asg(_named("eval-asg"))  # finding 19: no host left billing in Terminating:Wait
    if args.nat_instance:
        nat(argparse.Namespace(mode="instance", yes=args.yes))
    guard(argparse.Namespace(mode="resume", yes=args.yes))
    return verify_down()


def roll(args: argparse.Namespace) -> int:
    """Move every service with ignore_changes=[task_definition] to its latest revision.

    Phase 2 finding 12: the eval-tier services (gateway, harness-dispatcher, eval-worker,
    git-mirror) carry the same ignore_changes as the three ui services, so an apply that
    changes their task definition leaves them on the old revision too — roll all seven.
    """
    cluster = _named("cluster")
    for svc in UI_SERVICES + EVAL_SERVICES:
        family = _named(svc)
        out = _aws(
            "ecs",
            "update-service",
            "--cluster",
            cluster,
            "--service",
            svc,
            "--task-definition",
            family,
            "--force-new-deployment",
        )
        print(
            f"   {svc} -> {out['service']['deployments'][0]['taskDefinition'].rsplit('/', 1)[-1]}"
        )
    _say("waiting for the services to become stable (up to 10 min)")
    _run(
        [
            "aws",
            "ecs",
            "wait",
            "services-stable",
            "--cluster",
            cluster,
            "--services",
            *UI_SERVICES,
            *EVAL_SERVICES,
        ]
        + (["--profile", str(_profile())] if _profile() else [])
        + ["--region", aws_names.region()]
    )
    return 0


def verify(args: argparse.Namespace) -> int:
    cluster = _named("cluster")
    ok = True
    svcs = _aws(
        "ecs", "describe-services", "--cluster", cluster, "--services", *UI_SERVICES, *EVAL_SERVICES
    )["services"]
    for s in svcs:
        good = s.get("runningCount") == s.get("desiredCount") and s.get("desiredCount", 0) > 0
        ok &= good
        print(
            f"   {'OK  ' if good else 'FAIL'} {s['serviceName']}: {s.get('runningCount')}/{s.get('desiredCount')}"
        )
    fams = _aws(
        "ecs",
        "list-task-definition-families",
        "--status",
        "ACTIVE",
        "--family-prefix",
        _named("harness-"),
    )["families"]
    fams = [f for f in fams if f != _named("harness-dispatcher")]
    imgs = _aws("ecr", "list-images", "--repository-name", _named("harness-worker"))["imageIds"]
    inst = {i.get("imageTag", "") for i in imgs if str(i.get("imageTag", "")).endswith("-inst")}
    good = len(fams) == len(inst) and len(fams) > 0
    ok &= good
    print(
        f"   {'OK  ' if good else 'FAIL'} harness families {len(fams)} vs -inst images {len(inst)}"
    )
    return 0 if ok else 1


def verify_down() -> int:
    cluster = _named("cluster")
    c = _aws("ecs", "describe-clusters", "--clusters", cluster)["clusters"][0]
    asg = _aws(
        "autoscaling",
        "describe-auto-scaling-groups",
        "--auto-scaling-group-names",
        _named("eval-asg"),
    )["AutoScalingGroups"]
    caches = _aws("elasticache", "describe-serverless-caches")["ServerlessCaches"]
    lbs = _aws("elbv2", "describe-load-balancers")["LoadBalancers"]
    vpce = _aws(
        "ec2",
        "describe-vpc-endpoints",
        "--filters",
        "Name=vpc-endpoint-type,Values=Interface",
        f"Name=tag:Name,Values={_named('vpce-*')}",
    )["VpcEndpoints"]
    rows = [
        ("active services", c.get("activeServicesCount", 0)),
        ("running tasks", c.get("runningTasksCount", 0)),
        ("eval ASG desired", asg[0]["DesiredCapacity"] if asg else 0),
        ("serverless caches", len(caches)),
        ("load balancers", len(lbs)),
        ("eval interface endpoints", len(vpce)),
    ]
    ok = all(v == 0 for _, v in rows)
    for k, v in rows:
        print(f"   {'OK  ' if v == 0 else 'FAIL'} {k}: {v}")
    print(
        "   "
        + (
            "teardown verified — only the persistent tier remains"
            if ok
            else "something survived the teardown; investigate before leaving it"
        )
    )
    return 0 if ok else 1


# ── images / gate ─────────────────────────────────────────────────────────────


def _ecr_bytes(repo: str) -> int:
    total = 0
    token: str | None = None
    while True:
        args = ["ecr", "describe-images", "--repository-name", repo, "--max-results", "1000"]
        if token:
            args += ["--next-token", token]
        page = _aws(*args)
        total += sum(int(i.get("imageSizeInBytes", 0)) for i in page.get("imageDetails", []))
        token = page.get("nextToken")
        if not token:
            return total


def _all_instance_ids() -> list[str]:
    from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader

    return [r.instance_id for r in SwebenchLiteLoader(include_gold=True).load()]


def _missing_inst_images(instance_ids: list[str]) -> list[str]:
    """The requested ids whose promoted ``<ver>-<id>-inst`` tag is NOT in ECR."""
    from swebench_eval.evaluation.env_image import inst_tag

    present: set[str] = set()
    token: str | None = None
    while True:
        cmd = [
            "ecr",
            "list-images",
            "--repository-name",
            _named("harness-worker"),
            "--max-results",
            "1000",
        ]
        if token:
            cmd += ["--next-token", token]
        page = _aws(*cmd)
        present |= {str(i.get("imageTag", "")) for i in page.get("imageIds", [])}
        token = page.get("nextToken")
        if not token:
            break
    return [i for i in instance_ids if inst_tag(i) not in present]


def _build_host_private_subnet(cluster: str) -> str:
    """Wait for a registered build host and return the PRIVATE subnet of its AZ.

    Phase 2 finding 6: the build hosts sit in PUBLIC subnets (their docker daemon pulls from
    Docker Hub on a public IP); an awsvpc task ENI placed in the host's own subnet with no
    public IP has no route out — its first STS/ECR call timed out.
    """
    _say("waiting for a container instance")
    host_az = None
    for _ in range(60):
        arns = _aws("ecs", "list-container-instances", "--cluster", cluster)[
            "containerInstanceArns"
        ]
        if arns:
            ci = _aws(
                "ecs",
                "describe-container-instances",
                "--cluster",
                cluster,
                "--container-instances",
                arns[0],
            )["containerInstances"][0]
            host_az = next(
                (
                    a["value"]
                    for a in ci.get("attributes", [])
                    if a["name"] == "ecs.availability-zone"
                ),
                None,
            )
            if host_az:
                break
        time.sleep(10)
    if not host_az:
        raise SystemExit("no build host registered in 10 minutes")
    private_ids = _tf_output("persistent", "private_subnet_ids") or []
    subnets = _aws("ec2", "describe-subnets", "--subnet-ids", *private_ids)["Subnets"]
    subnet = next((sn["SubnetId"] for sn in subnets if sn["AvailabilityZone"] == host_az), None)
    if not subnet:
        raise SystemExit(f"no private subnet in the build host's AZ {host_az}")
    return str(subnet)


def images(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    only = [i.strip() for i in (args.only or "").split(",") if i.strip()]
    if args.subset and not only:
        from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader

        rows = SwebenchLiteLoader(include_gold=True).load()
        only = [r.instance_id for r in rows[: args.subset]]
    _say(f"instances: {len(only) if only else 'the whole split'}")

    _say("dataset mirror")
    _run([sys.executable, str(ROOT / "scripts" / "seed_dataset.py")])
    _say("image-digest snapshot check")
    # Docker Hub's anonymous tags API cannot take 500 lookups; the bootstrap secret carries the PAT.
    os.environ.setdefault(
        "DOCKERHUB_SECRET_ID", f"ecr-pullthroughcache/{aws_names.name_prefix()}-dockerhub"
    )
    check = [sys.executable, str(ROOT / "scripts" / "snapshot_image_digests.py"), "--check"]
    if only:
        check += ["--only", ",".join(only)]  # a subset build verifies only its own pins
    _run(check)

    _tf_init("build")
    plan, s = _tf_plan("build")
    _tf_apply_plan("build", plan, s, args.yes, "build tier")
    asg = _named("build-asg")
    cluster = _named("build-cluster")
    _say(f"build ASG {asg} -> {args.hosts}")
    _aws(
        "autoscaling",
        "set-desired-capacity",
        "--auto-scaling-group-name",
        asg,
        "--desired-capacity",
        str(args.hosts),
        parse=False,
    )
    try:
        # Phase 2 finding 11: the build hosts are Spot and CAN be reclaimed mid-build (it
        # happened at 18:23Z on the validation: six of ten builds got SIGTERM, the task
        # stopped with a TerminationNotice and a null exit code, and the run looked green).
        # So: after every builder run, ask ECR which requested images carry a promoted
        # -inst tag, and relaunch the builder for the rest (it resumes from ECR state),
        # up to three rounds. A missing image is a failure, never a silent shortfall.
        requested = list(only) if only else _all_instance_ids()
        for round_no in range(1, 4):
            missing = _missing_inst_images(requested)
            if not missing:
                break
            if round_no > 1:
                _say(
                    f"round {round_no}: {len(missing)} image(s) still missing — relaunching the builder"
                )
            subnet = _build_host_private_subnet(cluster)
            sg = _tf_output("persistent", "task_security_group_id")
            targets = missing if only or round_no > 1 else []
            shards = 1 if targets else max(1, args.hosts)
            tasks = []
            for shard in range(shards):
                overrides: dict[str, Any] = {"containerOverrides": [{"name": "warm-job"}]}
                if targets:
                    # --promote (finding 8): the builder pushes -inst-official; only the promoted
                    # -inst tag is what the families, the gold gate and the dispatcher resolve,
                    # and only promotion writes the per-instance manifest record.
                    overrides["containerOverrides"][0]["command"] = [
                        "phase0-instances-v2",
                        "--promote",
                        "--only",
                        ",".join(targets),
                    ]
                else:
                    overrides["containerOverrides"][0]["command"] = [
                        "phase0-instances-v2",
                        "--promote",
                    ]
                    overrides["containerOverrides"][0]["environment"] = [
                        {"name": "ENV_SHARD", "value": f"{shard}/{shards}"}
                    ]
                out = _aws(
                    "ecs",
                    "run-task",
                    "--cluster",
                    cluster,
                    "--task-definition",
                    _named("image-build"),
                    "--count",
                    "1",
                    "--capacity-provider-strategy",
                    f"capacityProvider={_named('build-capacity-provider')},weight=1,base=1",
                    "--network-configuration",
                    f"awsvpcConfiguration={{subnets=[{subnet}],securityGroups=[{sg}],assignPublicIp=DISABLED}}",
                    "--overrides",
                    json.dumps(overrides),
                )
                if out.get("failures"):
                    raise SystemExit(f"run-task failed: {out['failures']}")
                tasks.append(out["tasks"][0]["taskArn"])
                print(f"   launched shard {shard}/{shards}: {tasks[-1].rsplit('/', 1)[-1]}")
            _say(
                f"waiting for the builder task(s); logs: aws logs tail /aws/ecs/{_named('image-build')} --follow"
            )
            while True:
                desc = _aws("ecs", "describe-tasks", "--cluster", cluster, "--tasks", *tasks)[
                    "tasks"
                ]
                if all(t["lastStatus"] == "STOPPED" for t in desc):
                    for t in desc:
                        code = t.get("stopCode")
                        exits = [c.get("exitCode") for c in t.get("containers", [])]
                        print(
                            f"   task {t['taskArn'].rsplit('/', 1)[-1]}: {code} exit={exits} "
                            f"({t.get('stoppedReason', '')})"
                        )
                    break
                time.sleep(30)
        missing = _missing_inst_images(requested)
        if missing:
            raise SystemExit(
                f"{len(missing)} of {len(requested)} requested image(s) never got a promoted -inst "
                f"tag after 3 builder rounds: {missing[:10]} — see the log group"
            )
        _say(f"all {len(requested)} requested image(s) carry a promoted -inst tag")
        _say("cache manifest + task families")
        _run([sys.executable, str(ROOT / "scripts" / "warm_image_cache.py"), "--manifest"])
        _families()
    finally:
        _say(f"build ASG {asg} -> 0")
        _aws(
            "autoscaling",
            "set-desired-capacity",
            "--auto-scaling-group-name",
            asg,
            "--desired-capacity",
            "0",
            parse=False,
        )
    size = _ecr_bytes(_named("harness-worker"))
    _say(
        f"images done: {len(only) or 'all'} instance(s) in {(time.monotonic() - t0) / 60:.1f} min; ECR {_named('harness-worker')} now {size / 1e9:.1f} GB"
    )
    return 0


def _api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        API_URL + path,
        method=method,
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"API {API_URL}{path} unreachable ({exc}) — is `make tunnel` running?"
        ) from exc


def gate(args: argparse.Namespace) -> int:
    ids = [i.strip() for i in args.instances.split(",") if i.strip()]
    if not ids:
        raise SystemExit("gate needs INSTANCES=id1,id2,...")
    _say(f"gold gate for {len(ids)} instance(s) — every image must grade the gold patch RESOLVED")
    if args.run_id:
        run_id = args.run_id  # watch a validation run that was already enqueued
    else:
        report = _api("POST", "/images/validate", {"instance_ids": ids, "actor": "make gate"})
        run_id = report["run_id"]
        skipped = report.get("skipped") or []
        if skipped:
            print(f"   skipped (already validated or not launchable): {skipped}")
    # Phase 2 finding 14: the API enqueues the grades and returns at once; the gate is
    # only passed when every image has graded the gold patch RESOLVED. Poll the run.
    _say(f"grades enqueued as run {run_id}; waiting for the eval tier (a Spot host must come up)")
    deadline = time.monotonic() + 90 * 60
    verdicts: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        rows = _api("GET", f"/runs/{run_id}/instances?limit=500").get("items") or []
        for row in rows:
            state = str(row.get("state") or "")
            if row.get("verdict") or state.endswith(("FAILED", "ABANDONED", "INVALID")):
                verdicts[str(row["instance_id"])] = row
        done = [i for i in ids if i in verdicts]
        print(f"   {len(done)}/{len(ids)} graded", flush=True)
        if len(done) == len(ids):
            break
        time.sleep(30)
    bad = []
    for i in ids:
        row = verdicts.get(i) or {}
        verdict = row.get("verdict")
        ok = verdict == "resolved"
        print(
            f"   {'OK  ' if ok else 'FAIL'} {i}: {verdict or row.get('state') or 'no verdict in 90 min'}"
        )
        if not ok:
            bad.append(i)
    if bad:
        raise SystemExit(f"gold gate FAILED for {len(bad)} image(s): {bad}")
    _say("gold gate passed — every image grades the gold patch RESOLVED")
    return 0


def tunnel(args: argparse.Namespace) -> int:
    nat_id = _tf_output("persistent", "nat_instance_id")
    # --gateway: the LiteLLM gateway ALB on localhost:4000 (what `make pin` talks to);
    # default: the API ALB on localhost:8000 (the UI, `make gate`, `make discover`).
    doc = _named("tunnel-gateway-4000") if args.gateway else _named("tunnel-api-8000")
    port = 4000 if args.gateway else 8000
    where = (
        f"http://localhost:{port}/ui/ (the dashboard) and /docs (the API)"
        if not args.gateway
        else f"http://localhost:{port}"
    )
    _say(f"SSM port-forward {doc} via {nat_id}: {where} (Ctrl-C to stop)")
    cmd = [
        "aws",
        "ssm",
        "start-session",
        "--target",
        str(nat_id),
        "--document-name",
        doc,
        "--region",
        aws_names.region(),
    ]
    if _profile():
        cmd += ["--profile", _profile() or ""]
    return subprocess.call(cmd, env=_aws_env())


def discover(args: argparse.Namespace) -> int:
    before = {r["model_alias"]: r.get("discovered_at") for r in _api("GET", "/model-ceilings")}
    _api(
        "POST",
        f"/model-ceilings/{args.alias}/discover",
        {"target_concurrency": args.target, "triggered_by": "make discover"},
    )
    _say(f"discovery started for {args.alias}; polling GET /model-ceilings")
    for _ in range(120):
        time.sleep(15)
        rows = {r["model_alias"]: r for r in _api("GET", "/model-ceilings")}
        r = rows.get(args.alias)
        if r and r.get("discovered_at") and r.get("discovered_at") != before.get(args.alias):
            print(json.dumps(r, indent=2))
            _say("now set the pacer rate and ceiling in the UI (Limits) before launching")
            return 0
    raise SystemExit("discovery did not finish in 30 minutes")


def pin(args: argparse.Namespace) -> int:
    _say(
        "registering the git-tracked model specs (swebench_eval/gateway/rotatable_models.py, provider pins included) as gateway db-models"
    )
    base = os.environ.get("LITELLM_BASE_URL") or "http://localhost:4000/v1"
    # Clean-pass finding 24: the reconcile script authenticates with LITELLM_MASTER_KEY and
    # refuses to run without it; nothing in SETUP.md exports it, and it is already in
    # setup.yaml (the key the gateway was deployed with). Read it from there when unset —
    # never printed, never written anywhere else.
    if not os.environ.get("LITELLM_MASTER_KEY"):
        key = load_setup().get("litellm_master_key", "")
        if not key:
            raise SystemExit("LITELLM_MASTER_KEY is unset and setup.yaml has no litellm_master_key")
        os.environ["LITELLM_MASTER_KEY"] = key
    return _run(
        [sys.executable, str(ROOT / "scripts" / "reconcile_gateway_models.py"), "--base-url", base]
        + (["--dry-run"] if args.dry_run else []),
        check=False,
    ).returncode


def purge(args: argparse.Namespace) -> int:
    for q in ("harness-jobs", "eval-jobs", "results", "llm-calls"):
        url = _aws("sqs", "get-queue-url", "--queue-name", _named(q))["QueueUrl"]
        a = _aws(
            "sqs",
            "get-queue-attributes",
            "--queue-url",
            url,
            "--attribute-names",
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        )["Attributes"]
        n = int(a.get("ApproximateNumberOfMessages", 0)) + int(
            a.get("ApproximateNumberOfMessagesNotVisible", 0)
        )
        print(f"   {q}: {n} message(s)")
        if n and args.yes:
            _aws("sqs", "purge-queue", "--queue-url", url, parse=False)
            print("      purged")
    if not args.yes:
        print("   (re-run with --yes to purge non-empty queues)")
    return 0


# ── teardown-all / account-empty (the clean-pass precondition) ───────────────────────


def _delete_bucket_completely(bucket: str) -> None:
    """Every object version and delete marker, then the bucket."""
    import boto3

    session = boto3.session.Session(profile_name=_profile(), region_name=aws_names.region())
    s3 = session.client("s3")
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])]
        objs += [
            {"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])
        ]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs[i : i + 1000], "Quiet": True})
    s3.delete_bucket(Bucket=bucket)


def teardown(args: argparse.Namespace) -> int:
    """Remove EVERYTHING this framework put in the account (the clean-pass precondition).

    Terraform destroy alone leaves: the state bucket, the two out-of-band secrets, the
    pull-through-cache ECR repositories, stray log groups — and refuses Aurora (deletion
    protection) and the durable buckets (force_destroy=false) by design. This does all of it,
    in order, then runs the sweep. The IAM role you log in with and the service quotas stay.
    """
    prefix = aws_names.name_prefix()
    bucket = _tfstate_bucket()
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("teardown needs --yes on a non-interactive terminal")
        phrase = f"DESTROY {prefix}"
        if (
            input(
                f"This removes every {prefix}-* resource incl. Aurora, the results and the state bucket. Type '{phrase}' to continue: "
            )
            != phrase
        ):
            raise SystemExit("aborted")
    _export_credentials()
    # 1. the session tiers (eval, ui), pure-destroy checked
    for root in ("eval", "ui", "build"):
        if not _state_exists(root):
            print(f"   {root}: no state — skipped")
            continue
        _tf_init(root)
        plan, s = _tf_plan(root, destroy=True)
        if not is_pure_destroy(s):
            raise SystemExit(f"{root}: destroy plan is not a pure destroy — stop and look")
        _tf_apply_plan(root, plan, s, True, f"{root} DESTROY")
    # 2. persistent: empty both EC2 groups (finding 19), lift the two guards, then destroy
    for group in ("eval-asg", "build-asg"):
        _drain_asg(_named(group))
    if _state_exists("persistent"):
        _tf_init("persistent")
        guards = [
            "-var=aurora_deletion_protection=false",
            "-var=durable_buckets_force_destroy=true",
        ]
        # Finding 20: lifting the guards used to be a plain apply. On a PARTIAL state (a
        # teardown that stopped mid-destroy) that apply REBUILT the tier — 134 resources,
        # Aurora included — before the destroy. The lift is now a targeted apply of the
        # resources the guard variables actually change, and must not create anything.
        plan, s = _tf_plan("persistent", *guards)
        lifts = sorted(a for a, acts in s.actions.items() if acts == {"update"})
        if s.add or s.replace:
            _say(
                f"persistent: state is partial ({s.add + s.replace} resource(s) would be "
                "re-created by a full apply) — lifting the guards by targeted apply only"
            )
        if lifts:
            plan, s = _tf_plan("persistent", *guards, *[f"-target={a}" for a in lifts])
            if s.add or s.replace or s.destroy:
                raise SystemExit(
                    "persistent: the guard lift would create or destroy resources — stop and look"
                )
            _tf_apply_plan(
                "persistent",
                plan,
                s,
                True,
                "persistent (lift deletion protection + force_destroy)",
            )
        else:
            _say("persistent: guards already lifted")
        plan, s = _tf_plan("persistent", *guards, destroy=True)
        if not is_pure_destroy(s):
            raise SystemExit("persistent: destroy plan is not a pure destroy — stop and look")
        _tf_apply_plan("persistent", plan, s, True, "persistent DESTROY")
    # 3. what Terraform never owned
    secrets = (_named("openrouter-management"), f"ecr-pullthroughcache/{prefix}-dockerhub")
    for name in secrets:
        try:
            _aws(
                "secretsmanager",
                "delete-secret",
                "--secret-id",
                name,
                "--force-delete-without-recovery",
            )
            print(f"   deleted secret {name}")
        except subprocess.CalledProcessError:
            print(f"   secret {name}: already gone")
    # Clean-pass finding 27: a force delete is asynchronous — the sweep, seconds later, listed
    # the Docker Hub secret as "scheduled for deletion" although it was gone a moment after.
    # Wait for Secrets Manager to stop knowing the names before anything reads them back.
    deadline = time.monotonic() + 120
    pending = list(secrets)
    while pending and time.monotonic() < deadline:
        for name in list(pending):
            try:
                _aws("secretsmanager", "describe-secret", "--secret-id", name)
            except subprocess.CalledProcessError:
                pending.remove(name)  # ResourceNotFound: the delete has landed
        if pending:
            time.sleep(5)
    if pending:
        print(f"   secrets still visible after 120 s (the sweep will list them): {pending}")
    repos = _aws("ecr", "describe-repositories").get("repositories", [])
    for r in repos:
        n = str(r["repositoryName"])
        if n.startswith((f"{prefix}-", f"{prefix}/", "docker-hub/")):
            _aws("ecr", "delete-repository", "--repository-name", n, "--force")
            print(f"   deleted ECR repository {n}")
    for lg in _aws("logs", "describe-log-groups").get("logGroups", []):
        n = str(lg["logGroupName"])
        if prefix in n:
            _aws("logs", "delete-log-group", "--log-group-name", n)
            print(f"   deleted log group {n}")
    # ECS creates `ecs-managed-capacity-provider-rule` (ManagedBy ecs.amazonaws.com) the
    # first time a capacity provider is attached and never removes it — the one thing the
    # sweep found after the first full teardown. Free, and recreated by ECS on the next
    # bring-up; deleted here so the sweep can stay strict.
    for rule in _aws("events", "list-rules", "--name-prefix", "ecs-managed-").get("Rules", []):
        if rule.get("ManagedBy") != "ecs.amazonaws.com":
            continue
        n = str(rule["Name"])
        ids = [
            str(t["Id"])
            for t in _aws("events", "list-targets-by-rule", "--rule", n).get("Targets", [])
        ]
        if ids:
            _aws("events", "remove-targets", "--rule", n, "--ids", *ids, "--force")
        _aws("events", "delete-rule", "--name", n, "--force")
        print(f"   deleted ECS-managed EventBridge rule {n}")
    if bucket:
        try:
            _delete_bucket_completely(bucket)
            print(f"   deleted state bucket {bucket} (all versions)")
        except Exception as exc:  # noqa: BLE001 - report, then let the sweep show it
            print(f"   state bucket {bucket}: {exc}")
    for root in ("persistent", "ui", "eval", "build"):
        for f in ("backend.hcl", "terraform.tfvars"):
            (TF_ROOT / root / f).unlink(missing_ok=True)
        shutil.rmtree(TF_ROOT / root / ".terraform", ignore_errors=True)
    print("   removed backend.hcl / terraform.tfvars / .terraform from every root")
    _say("teardown done — running the sweep")
    return sweep(args)


def _sweep_region(region: str, prefix: str) -> list[str]:
    """Everything of ours that still exists in *region*."""
    out: list[str] = []

    def aws(*a: str) -> Any:
        cmd = ["aws", *a, "--output", "json", "--region", region]
        if _profile():
            cmd += ["--profile", _profile() or ""]
        r = subprocess.run(cmd, capture_output=True, text=True, env=_aws_env(), check=False)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}

    def ours(name: str) -> bool:
        return prefix in name

    for v in aws("ec2", "describe-vpcs").get("Vpcs", []):
        if not v.get("IsDefault"):
            out.append(f"vpc {v['VpcId']}")
    for r in aws(
        "ec2",
        "describe-instances",
        "--filters",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
    ).get("Reservations", []):
        for i in r.get("Instances", []):
            out.append(f"ec2 {i['InstanceId']} {i.get('InstanceType')}")
    for a in aws("ec2", "describe-addresses").get("Addresses", []):
        out.append(f"eip {a.get('PublicIp')}")
    for g in aws("autoscaling", "describe-auto-scaling-groups").get("AutoScalingGroups", []):
        out.append(f"asg {g['AutoScalingGroupName']}")
    for t in aws("ec2", "describe-launch-templates").get("LaunchTemplates", []):
        out.append(f"launch-template {t['LaunchTemplateName']}")
    for n in aws(
        "ec2", "describe-nat-gateways", "--filter", "Name=state,Values=pending,available"
    ).get("NatGateways", []):
        out.append(f"nat-gateway {n['NatGatewayId']}")
    for c in aws("ecs", "list-clusters").get("clusterArns", []):
        out.append(f"ecs-cluster {c.rsplit('/', 1)[-1]}")
    for r in aws("ecr", "describe-repositories").get("repositories", []):
        out.append(f"ecr {r['repositoryName']}")
    for c in aws("rds", "describe-db-clusters").get("DBClusters", []):
        out.append(f"rds {c['DBClusterIdentifier']}")
    for c in aws("elasticache", "describe-serverless-caches").get("ServerlessCaches", []):
        out.append(f"elasticache {c['ServerlessCacheName']}")
    for lb in aws("elbv2", "describe-load-balancers").get("LoadBalancers", []):
        out.append(f"alb {lb['LoadBalancerName']}")
    for q in aws("sqs", "list-queues").get("QueueUrls", []):
        out.append(f"sqs {q.rsplit('/', 1)[-1]}")
    for t in aws("sns", "list-topics").get("Topics", []):
        out.append(f"sns {t['TopicArn'].rsplit(':', 1)[-1]}")
    for f in aws("lambda", "list-functions").get("Functions", []):
        out.append(f"lambda {f['FunctionName']}")
    for s in aws("secretsmanager", "list-secrets", "--include-planned-deletion").get(
        "SecretList", []
    ):
        out.append(
            f"secret {s['Name']}" + (" (scheduled for deletion)" if s.get("DeletedDate") else "")
        )
    for lg in aws("logs", "describe-log-groups").get("logGroups", []):
        if ours(lg["logGroupName"]):
            out.append(f"log-group {lg['logGroupName']}")
    for r in aws("events", "list-rules").get("Rules", []):
        out.append(f"event-rule {r['Name']}")
    for a in aws("cloudwatch", "describe-alarms").get("MetricAlarms", []):
        out.append(f"alarm {a['AlarmName']}")
    for e in aws("ec2", "describe-vpc-endpoints").get("VpcEndpoints", []):
        out.append(f"vpc-endpoint {e['VpcEndpointId']}")
    for d in aws("ssm", "list-documents", "--filters", "Key=Owner,Values=Self").get(
        "DocumentIdentifiers", []
    ):
        out.append(f"ssm-document {d['Name']}")
    for n in aws("servicediscovery", "list-namespaces").get("Namespaces", []):
        out.append(f"cloud-map {n['Name']}")
    return out


def sweep(args: argparse.Namespace) -> int:
    """Read-only: list everything of ours left in the account; exit 1 if anything remains."""
    prefix = aws_names.name_prefix()
    regions = sorted({aws_names.region(), "us-east-1"})  # the billing alarm lives in us-east-1
    left: list[str] = []
    for region in regions:
        for item in _sweep_region(region, prefix):
            left.append(f"{region}: {item}")
    # global (region-less) services
    for b in _aws("s3api", "list-buckets").get("Buckets", []):
        left.append(f"global: s3 {b['Name']}")
    for r in _aws("iam", "list-roles").get("Roles", []):
        if prefix in r["RoleName"]:
            left.append(f"global: iam-role {r['RoleName']}")
    for p in _aws("iam", "list-policies", "--scope", "Local").get("Policies", []):
        if prefix in p["PolicyName"]:
            left.append(f"global: iam-policy {p['PolicyName']}")
    for z in _aws("route53", "list-hosted-zones").get("HostedZones", []):
        left.append(f"global: route53 {z['Name']}")
    for b in _aws("budgets", "describe-budgets", "--account-id", aws_names.account_id()).get(
        "Budgets", []
    ):
        left.append(f"global: budget {b['BudgetName']}")
    if left:
        print(f"{len(left)} resource(s) still in the account:")
        for item in left:
            print("   " + item)
        print(
            "(kept on purpose and not listed: your login role, service quotas, Cost Explorer history)"
        )
        return 1
    _say(f"account is empty in {', '.join(regions)} and globally — nothing of ours remains")
    return 0


def nat(args: argparse.Namespace) -> int:
    _tf_init("persistent")
    flag = "true" if args.mode == "gateway" else "false"
    plan, s = _tf_plan("persistent", f"-var=nat_gateway_enabled={flag}", "-target=module.network")
    if any(
        "aws_instance.nat" in a and ("delete" in acts or "create" in acts)
        for a, acts in s.actions.items()
    ):
        raise SystemExit("the plan would replace the NAT/tunnel instance — stop and look")
    _tf_apply_plan("persistent", plan, s, args.yes, f"NAT egress -> {args.mode}")
    return 0


def guard(args: argparse.Namespace) -> int:
    _tf_init("persistent")
    paused = "true" if args.mode == "pause" else "false"
    plan, s = _tf_plan(
        "persistent",
        f"-var=nightly_scale_to_zero_paused={paused}",
        "-target=module.observability.aws_cloudwatch_event_rule.nightly_scale_to_zero[0]",
    )
    _tf_apply_plan("persistent", plan, s, args.yes, f"nightly guard -> {args.mode}")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    # Every script this CLI runs talks to the REAL account, never the compose stack (finding 9).
    os.environ.setdefault("EVAL_REAL_AWS", "1")
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Any, **kw: Any) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=kw.pop("help", None))
        sp.add_argument("-y", "--yes", action="store_true", help="apply without confirming")
        sp.set_defaults(fn=fn)
        return sp

    add("doctor", doctor).add_argument("--scale", choices=("small", "full"), default="small")
    add("quotas", quotas).add_argument("--scale", choices=("small", "full"), default="small")
    add("bootstrap", bootstrap).add_argument("--dry-run", action="store_true")
    sp = add("up", up)
    sp.add_argument(
        "--persistent", action="store_true", help="apply persistent/ first (first bring-up)"
    )
    sp.add_argument("--skip-families", action="store_true")
    add("down", down).add_argument(
        "--nat-instance", action="store_true", help="also switch NAT egress to the t3 instance"
    )
    add("roll", roll)
    add("verify", verify)
    sp = add("images", images)
    sp.add_argument("--subset", type=int, default=0, help="first N instances of the split")
    sp.add_argument("--only", default="", help="comma-separated instance ids")
    sp.add_argument("--hosts", type=int, default=1, help="build hosts (one shard each)")
    sp = add("gate", gate)
    sp.add_argument("--instances", required=True)
    sp.add_argument(
        "--run-id",
        default=None,
        help="watch an existing image-validation run instead of enqueueing",
    )
    add("tunnel", tunnel).add_argument(
        "--gateway",
        action="store_true",
        help="the gateway ALB on :4000 instead of the API on :8000",
    )
    sp = add("discover", discover)
    sp.add_argument("alias")
    sp.add_argument("--target", type=int, default=150)
    add("pin", pin).add_argument("--dry-run", action="store_true")
    add("purge", purge)
    add("teardown", teardown)
    add("sweep", sweep)
    add("nat", nat).add_argument("mode", choices=("gateway", "instance"))
    add("guard", guard).add_argument("mode", choices=("pause", "resume"))
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
