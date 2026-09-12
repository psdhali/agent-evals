/**
 * modules/ecr — single repos per image family, NOT repo-per-image (5b forward-compat)
 *
 * 5b needs one env-images repo carrying 61 tags and one harness-worker repo
 * carrying 61 tags, because ECR deduplicates layers WITHIN a repository, not
 * across (image-environment-pipeline.md §4). So 5a creates FOUR single repos,
 * and 5b's 61-tag picture is additive on them — never repo-per-instance
 * (ADR-0020's superseded 2,294-repo path).
 *
 * This is a DELIBERATE DEVIATION from architecture §6.6's `ecr/ (x5)` +
 * `eval-worker`, which predates the 61-image design. Recorded in the Phase 5a
 * summary (rule 7); §6.6 is not edited (rule 1).
 *
 * force_delete = true (F-9): repos hold images; destroy must not fail on
 * non-empty repos. Lifecycle policy expires untagged beyond a small buffer (N7).
 *
 * TAGGED-IMAGE PRUNING IS OFF BY DEFAULT (R-1). A rule that can expire a tag the
 * system still needs is worse than no rule. The 61 harness-worker / env-images
 * tags are CONTENT IDENTIFIERS, not versions — current until deliberately
 * rebuilt; no elapsed time stales them. So the earlier age-based
 * `sinceImagePushed/90d` rule (which would expire all 61 on day 91, silently,
 * asynchronously, surfacing a quarter later as a mysteriously slow run) is
 * REMOVED as a default. Rule 1 (untagged beyond a small buffer) always applies
 * — untagged images are the layers that actually accumulate, and ECS task
 * definitions reference images by TAG, so an untagged image is by definition
 * orphaned. The buffer is 2 (review F8): the 08-18 prune found 61 untagged
 * orphans (~44 GB nominal) across the repos; 20 was never a "small" buffer. A
 * repo may OPT IN to a deliberate tagged prune via `tagged_prune` when a real,
 * distinct tag scheme exists (5b defines it); until then a timer cannot tell a
 * stale tag from a required one.
 *
 * Buffer 2 is SAFE because the repos carry NO image indexes (all single
 * manifests, verified 2026-08-18). A multi-arch index's child manifests are
 * UNTAGGED and referenced only by the index, so an aggressive untagged rule
 * would delete them and break the tag that still points at the index. Revisit
 * this rule if builds move to `docker buildx` (OCI image index by default) or
 * SOCI is adopted (its index is a referrer, untagged by design).
 */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix."
}

variable "purpose" {
  type        = string
  description = "'dev' or 'scale'."
}

# env-images and harness-worker will each carry 61 tags in 5b; base + eval-worker
# are single images. All are single repos (the 5b-compatible shape).
variable "repos" {
  type        = list(string)
  default     = ["base", "env-images", "harness-worker", "eval-worker", "orchestrator", "warm-job", "gateway", "dispatch", "git-mirror"]
  description = "ECR repositories to create (single repos, many tags at 5b). Post-5: added orchestrator + warm-job — 5a-i DoD 10 needs all four service images pushed, and ui/ references the orchestrator repo. 5a-ii: added gateway (LiteLLM WITH config baked — the stock image has no /app/config.yaml) and dispatch (ADR-0024: the in-VPC dispatch Lambda)."
}

# R-1: per-repository DELIBERATE tagged-image pruning, DEFAULT OFF.
#   DEFAULT (empty map): no tagged-expiry rule — 61 content-ID tags persist until
#     someone removes them on purpose. NO timer is capable of deleting a tag the
#     system still needs. This is the safe default for 5a/5b.
#   by_age_days > 0 (per repo): expire every tag older than N days. Use ONLY for
#     repos whose tags are true versions (rebuildable), never for content-ID repos.
#   tag_prefixes (non-empty, per repo): expire tags matching these prefixes. The
#     intended seam for 5b's real tag scheme: if transient build tags are
#     distinguishable from the required tags by prefix, prune only the transient
#     ones. The 61 content-ID tags MUST NOT match any prefix in this list.
# Storage growth with no tagged expiry is handled by measurement + a deliberate
# prune (Phase 8 item), not a timer that cannot tell stale from required (R-1).
variable "tagged_prune" {
  type = map(object({
    by_age_days  = number       # >0 activates an age rule for THIS repo only
    tag_prefixes = list(string) # non-empty activates a prefix rule for THIS repo only
  }))
  default     = {}
  description = "Opt-in tagged-image expiry per repo. DEFAULTS OFF (R-1): tagged images are content IDs, not versions."
}

resource "aws_ecr_repository" "this" {
  for_each = toset(var.repos)

  name                 = "${var.name_prefix}-${each.key}"
  image_tag_mutability = "MUTABLE" # 5b tags many versions under one name
  force_delete         = true      # F-9: destroy must not fail on a non-empty repo

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name    = "${var.name_prefix}-${each.key}"
    purpose = var.purpose
  }
}

# N7 / ADR-0014 + R-1: lifecycle. Rule 1 (untagged beyond a small buffer)
# applies to every repo — untagged images are the layers that genuinely
# accumulate, and ECS references images by tag, so untagged = orphaned (F8
# narrowed the buffer from 20 to 2 after the 08-18 prune). Rule 2 (a tagged
# prune) is appended ONLY for repos that opt in via `tagged_prune`, so no
# default policy can expire a tag the system still needs.
locals {
  # Build rule 2 for each repo that opted in (empty map => only rule 1 applies).
  # `by_age_days` and `tag_prefixes` are mutually exclusive; age wins if both are
  # set (never do that). The age form expires true *versions* (rebuildable); the
  # prefix form prunes only tags a scheme says are transient (5b). Neither is the
  # default (see header + the `tagged_prune` variable).
  tagged_rules = {
    for k, pr in var.tagged_prune : k =>
    (pr.by_age_days > 0 ? {
      rulePriority = 2
      description  = "DELIBERATE age-based expiry of tagged images (>${pr.by_age_days}d) — versions only"
      selection = {
        tagStatus   = "tagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = pr.by_age_days
      }
      action = { type = "expire" }
      } : length(pr.tag_prefixes) > 0 ? {
      rulePriority = 2
      description  = "DELIBERATE expiry of tagged images by prefix (transient only, 5b scheme)"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = pr.tag_prefixes
        countType     = "imageCountMoreThan"
        countNumber   = 1
      }
      action = { type = "expire" }
    } : {})
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = aws_ecr_repository.this

  repository = each.value.name

  policy = jsonencode({
    rules = concat(
      [
        {
          rulePriority = 1
          description  = "Expire untagged images beyond a small buffer (2)"
          selection = {
            tagStatus   = "untagged"
            countType   = "imageCountMoreThan"
            countNumber = 2
          }
          action = { type = "expire" }
        }
      ],
      # R-1: a tagged prune ONLY where a repo explicitly opted in (default: none).
      # The comprehension filters to repos that actually carry a rule, so an
      # opted-in-but-empty entry contributes nothing.
      lookup(local.tagged_rules, each.key, null) == null ? [] :
      [lookup(local.tagged_rules, each.key, {})]
    )
  })
}

output "repository_urls" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}
