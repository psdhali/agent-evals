variable "framework_sha" {
  type        = string
  default     = "latest"
  description = <<EOT
    The ECR image TAG the build-tier warm-job (module "image_build", family
    eval-dev-image-build) resolves to a digest at apply time. This is the task that
    BUILDS the -inst images, so a stale warm-job here bakes stale framework code
    into every -inst image it produces (the 2026-09-02 staleness). Default "latest"
    preserves prior behavior (non-breaking). Pass the per-commit tag produced by
    scripts/docker_build_push.sh (`-var framework_sha=<git-sha>`) for a
    deterministic, immutable pin the mutable :latest can never make stale, and to
    force a reused EC2 build host to pull the exact build. Mirrors envs/dev/eval's
    framework_sha knob.
  EOT
}
