# ADR-0039: the control plane crosses the tier boundary.
#
# The orchestrator (api + control-plane) lives in the ui/ tier but reads and
# writes the control state (pause/abort) that ADR-0034 §2 stores in Valkey,
# which lives in THIS eval/ tier.  ui/ reads the cache endpoint from eval/'s
# remote state with a null-safe fallback (ADR-0039 §Decision 1), so eval/ must
# export the composed redis:// endpoint — the SAME value local.redis_endpoint
# in eval/main.tf builds (rediss://<host>:<port>).  One place knows the scheme.
output "redis_endpoint" {
  value       = local.redis_endpoint
  description = "Valkey endpoint for the control plane (ADR-0039). Empty when eval/ is down (destroyed); ui/ falls back to '' via try()."
}

# run-launch (BUILDER4-RUN-LAUNCH-ORCHESTRATOR-2026-08-26 §5.2): the
# orchestrator (ui/ tier) mints/rotates gateway keys at PROVISION and needs
# LITELLM_BASE_URL, exported here so ui/ can read it cross-tier the same way
# it already reads redis_endpoint.  Empty when eval/ is down; ui/ must fall
# back via try(), same three-tier-independence property as redis_endpoint.
#
# BUILDER4-RECONCILE-GATEWAY-DNS-2026-08-26: reads local.gateway_base_url
# (eval/main.tf:252) — the stable Cloud Map name gateway.eval.internal
# builder 1 landed in 99de159 — NOT a second raw-ALB-DNS construction. A
# second construction would silently diverge from the 40 (later ~500)
# harness families' own gateway_base_url (eval/main.tf:487, also
# local.gateway_base_url) the next time the ALB is replaced, with nothing
# failing until then. Verified exactly one construction exists:
# `grep -rn "gateway_base_url\|alb_dns" infra/terraform/envs/dev/eval/`
# shows local.gateway_base_url assigned once (line 252) and referenced by
# both consumers (line 487, this output) — module.gateway.alb_dns appears
# only inside that one assignment, nowhere else building a URL.
output "gateway_base_url" {
  value       = local.gateway_base_url
  description = "LiteLLM gateway base URL (LITELLM_BASE_URL) — the stable Cloud Map name gateway.eval.internal, not the ALB DNS. Empty when eval/ is down; ui/ falls back to '' via try()."
}