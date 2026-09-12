/**
 * envs/dev/build — the image-build tier (builder5-image-build-tier Stage 2).
 *
 * A standalone root so building images no longer requires bringing up the
 * entire eval tier (cache, gateway, git-mirror, eval-worker, dispatcher) —
 * the build code touches only ECR and S3 (§2 of the brief: grep for psycopg /
 * DATABASE_URL / SQS / redis / LITELLM across build_phase0_instances.py and
 * warm_image_cache.py returns zero), and needs its own EC2 pool sized
 * independently from eval grading (today both share ONE ASG, max_size=1).
 *
 * `terraform destroy` on THIS state brings the build tier down to zero while
 * envs/dev/eval's `module "warm_job"` keeps working through the ORIGINAL pool
 * — ADDITIVE, not a replacement, until this tier is proven (T1-T6) and the
 * owner signs off on removing the old path.
 */

provider "aws" {
  region  = var.region
  profile = local.aws_profile
  default_tags {
    tags = {
      App       = "swe-bench-eval-framework"
      Env       = var.purpose
      purpose   = var.purpose
      ManagedBy = "terraform"
    }
  }
}
