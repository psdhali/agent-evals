"""§6.7 per-run autoscaler overrides (harness-autoscaler exact-design §8) — the launch-time
fields, their Redis hand-off, and the dispatcher honoring them.

The chain under test: RunLaunchRequest -> RunConfig -> config_snapshot (asdict) AND
run_launch's Redis publish -> the dispatcher's admission cap + the Autoscaler's
ramp/cooldown/enabled knobs. The properties that must hold: overrides can only TIGHTEN
(min-wins vs the static env ceiling; ramp step clamped at the owner-fixed +5% hard max),
enabled=False disables only L2's dynamic gate, and every read/write is guarded so a Redis
failure leaves the static config in force.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest import mock

from swebench_eval.orchestrator.run_config import RunConfig


class FakeRedis:
    """hset/hget/hgetall/expire/delete over one dict-of-dicts."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}

    def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})

    def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl

    def delete(self, key: str) -> None:
        self.hashes.pop(key, None)


# ---------------------------------------------------------------------------
# The RunConfig fields + the launch-time publish.
# ---------------------------------------------------------------------------


class TestPoolSeedCopy:
    """E2E-RUN1 finding #2: discovery seeds pacer:cfg:{pool}; the launch must
    copy it to pacer:cfg:{run alias} (all consumers read the alias key)."""

    def test_launch_copies_pool_seeds_to_the_run_alias_key(self) -> None:
        from swebench_eval.gateway.pacer import pacer_cfg_key
        from swebench_eval.orchestrator.control_plane import run_launch

        fake = FakeRedis()
        fake.hset(
            pacer_cfg_key("laguna-xs-2.1"),
            mapping={"c_burst": "1066079", "r_tok": "11989", "seeded_at": "1756830000.0"},
        )
        config = RunConfig(model_alias="laguna-xs-2.1-mini")
        with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
            run_launch._publish_autoscaler_overrides("run-1", config)
        copied = fake.hashes[pacer_cfg_key("laguna-xs-2.1-mini")]
        assert copied["c_burst"] == "1066079" and copied["r_tok"] == "11989"
        # staleness clock belongs to the DISCOVERY — seeded_at copied verbatim
        assert copied["seeded_at"] == "1756830000.0"

    def test_existing_alias_seeds_are_never_overwritten(self) -> None:
        from swebench_eval.gateway.pacer import pacer_cfg_key
        from swebench_eval.orchestrator.control_plane import run_launch

        fake = FakeRedis()
        fake.hset(pacer_cfg_key("laguna-xs-2.1"), mapping={"r_tok": "11989"})
        fake.hset(pacer_cfg_key("laguna-xs-2.1-mini"), mapping={"r_tok": "555"})
        config = RunConfig(model_alias="laguna-xs-2.1-mini")
        with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
            run_launch._publish_autoscaler_overrides("run-1", config)
        assert fake.hashes[pacer_cfg_key("laguna-xs-2.1-mini")]["r_tok"] == "555"

    def test_unknown_alias_is_a_no_op(self) -> None:
        from swebench_eval.gateway.pacer import pacer_cfg_key
        from swebench_eval.orchestrator.control_plane import run_launch

        fake = FakeRedis()
        config = RunConfig(model_alias="not-a-rotatable-alias")
        with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
            run_launch._publish_autoscaler_overrides("run-1", config)
        assert pacer_cfg_key("not-a-rotatable-alias") not in fake.hashes


class TestConfigAndPublish:
    def test_config_snapshot_carries_the_override_fields(self) -> None:
        """CLAIM writes asdict(config); the five §6.7 fields must be in it — a snapshot
        that omits them cannot explain a run's admission behaviour after the fact."""
        snapshot = dataclasses.asdict(RunConfig())
        assert snapshot["max_parallel_harness_tasks"] == 150
        assert snapshot["initial_budget_override"] is None
        assert snapshot["ramp_step_pct"] == 5.0
        assert snapshot["ramp_cooldown_seconds"] == 60
        assert snapshot["autoscaler_enabled"] is True

    def test_publish_writes_the_hash_with_owner_and_ttl(self) -> None:
        from swebench_eval.orchestrator.control_plane import run_launch

        fake = FakeRedis()
        config = RunConfig(
            model_alias="laguna-xs-2.1", max_parallel_harness_tasks=80, ramp_cooldown_seconds=120
        )
        with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
            run_launch._publish_autoscaler_overrides("run-1", config)
        row = fake.hashes[run_launch.AUTOSCALER_OVERRIDES_KEY]
        assert row["run_id"] == "run-1"
        # §2.4 (wiring review): the run is the authoritative alias source — without this,
        # an unset AUTOSCALER_MODEL_ALIAS ran the planner on generic defaults silently.
        assert row["model_alias"] == "laguna-xs-2.1"
        assert row["max_parallel"] == "80"
        assert row["cooldown_s"] == "120.0"
        assert row["enabled"] == "1"
        assert run_launch.AUTOSCALER_OVERRIDES_KEY in fake.expires

    def test_budget_override_seeds_pacer_cfg_known_fields_only(self) -> None:
        from swebench_eval.orchestrator.control_plane import run_launch

        fake = FakeRedis()
        config = RunConfig(
            model_alias="laguna-xs-2.1",
            initial_budget_override={"c_burst": 1_500_000.0, "r_tok": 40_000.0, "bogus": 1.0},
        )
        with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
            run_launch._publish_autoscaler_overrides("run-1", config)
        cfg = fake.hashes["pacer:cfg:{laguna-xs-2.1}"]
        assert cfg["c_burst"] == "1500000.0"
        assert cfg["r_tok"] == "40000.0"
        assert "bogus" not in cfg
        assert "seeded_at" in cfg  # an operator override IS fresh evidence (F3 clock)

    def test_publish_failure_never_fails_the_launch(self) -> None:
        from swebench_eval.orchestrator.control_plane import run_launch

        with mock.patch(
            "swebench_eval.database.redis_client._get_client",
            side_effect=RuntimeError("redis down"),
        ):
            run_launch._publish_autoscaler_overrides("run-1", RunConfig())  # must not raise

    def test_clear_only_when_still_the_owner(self) -> None:
        from swebench_eval.orchestrator.control_plane import run_launch

        fake = FakeRedis()
        fake.hset(run_launch.AUTOSCALER_OVERRIDES_KEY, mapping={"run_id": "run-2"})
        with mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake):
            run_launch._clear_autoscaler_overrides("run-1")  # not the owner — must not sweep
            assert run_launch.AUTOSCALER_OVERRIDES_KEY in fake.hashes
            run_launch._clear_autoscaler_overrides("run-2")
            assert run_launch.AUTOSCALER_OVERRIDES_KEY not in fake.hashes


# ---------------------------------------------------------------------------
# The dispatcher honoring them.
# ---------------------------------------------------------------------------


def _overrides_hash(**fields: str) -> FakeRedis:
    from swebench_eval.orchestrator.control_plane.run_launch import AUTOSCALER_OVERRIDES_KEY

    fake = FakeRedis()
    fake.hset(AUTOSCALER_OVERRIDES_KEY, mapping={"run_id": "run-1", **fields})
    return fake


class TestAdmissionCap:
    def test_run_cap_tightens_the_static_ceiling(self) -> None:
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
            _DispatcherAdmission,
        )

        admission = _DispatcherAdmission(ceiling=200)
        fake = _overrides_hash(max_parallel="80")
        with (
            mock.patch.object(admission, "running_count", return_value=0),
            mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake),
        ):
            admission._refresh()
        assert admission.effective_ceiling() == 80

    def test_run_cap_never_widens(self) -> None:
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
            _DispatcherAdmission,
        )

        admission = _DispatcherAdmission(ceiling=50)
        fake = _overrides_hash(max_parallel="500")
        with (
            mock.patch.object(admission, "running_count", return_value=0),
            mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake),
        ):
            admission._refresh()
        assert admission.effective_ceiling() == 50

    def test_absent_or_bad_cap_leaves_static_ceiling(self) -> None:
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import (
            _DispatcherAdmission,
        )

        admission = _DispatcherAdmission(ceiling=150)
        for fake in (FakeRedis(), _overrides_hash(max_parallel="not-a-number")):
            with (
                mock.patch.object(admission, "running_count", return_value=0),
                mock.patch("swebench_eval.database.redis_client._get_client", return_value=fake),
            ):
                admission._refresh()
            assert admission.effective_ceiling() == 150


class TestAutoscalerKnobs:
    def _scaler(self, fake: FakeRedis) -> Any:
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import Autoscaler

        return Autoscaler(model_alias="laguna-xs-2.1", mode="live", redis_client=fake)

    def test_ramp_step_clamped_at_the_owner_fixed_max(self) -> None:
        scaler = self._scaler(_overrides_hash(ramp_step_pct="10"))
        scaler._apply_run_overrides()
        assert scaler._growth_step == 0.05  # 10% requested, 5% is the hard max

    def test_ramp_step_can_shrink(self) -> None:
        scaler = self._scaler(_overrides_hash(ramp_step_pct="2"))
        scaler._apply_run_overrides()
        assert scaler._growth_step == 0.02

    def test_cooldown_override_applies(self) -> None:
        scaler = self._scaler(_overrides_hash(cooldown_s="180"))
        scaler._apply_run_overrides()
        assert scaler._stabilization_window_s == 180.0

    def test_disabled_gate_never_blocks_even_live(self) -> None:
        """autoscaler_enabled=false: L2's dynamic ceiling must not block, whatever the
        decision says — the static ceiling and the L1 pacer stay in force elsewhere."""
        scaler = self._scaler(_overrides_hash(enabled="0"))
        scaler._apply_run_overrides()
        assert scaler._enabled is False

        class _Blocking:
            binding_constraint = "cooldown"
            desired_ceiling = 0

        with mock.patch.object(scaler, "maybe_tick", return_value=_Blocking()):
            allowed, _reason = scaler.gate(in_flight_tasks=10)
        assert allowed is True

    def test_run_alias_wins_over_static_and_falls_back(self) -> None:
        """§2.4 (wiring review): the launched run's alias is authoritative; the env/ctor
        alias is the fallback when no run has one published — never the other way round."""
        from swebench_eval.orchestrator.control_plane.harness_dispatcher import Autoscaler

        scaler = Autoscaler(
            model_alias="static-alias",
            mode="observe",
            redis_client=_overrides_hash(model_alias="laguna-xs-2.1"),
        )
        scaler._apply_run_overrides()
        assert scaler.model_alias == "laguna-xs-2.1"
        scaler._redis = FakeRedis()  # the run closed; its key is gone
        scaler._apply_run_overrides()
        assert scaler.model_alias == "static-alias"

    def test_defaults_restored_when_overrides_vanish(self) -> None:
        scaler = self._scaler(_overrides_hash(ramp_step_pct="2", cooldown_s="180"))
        scaler._apply_run_overrides()
        scaler._redis = FakeRedis()  # the run closed; the key is gone
        scaler._apply_run_overrides()
        assert scaler._growth_step == 0.05
        assert scaler._stabilization_window_s == 60.0
        assert scaler._enabled is True
