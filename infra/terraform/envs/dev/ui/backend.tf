/**
 * Remote state: S3 + native S3 locking (use_lockfile, Terraform 1.11+).
 * ADR-0014 makes remote state a real requirement.  The bucket, region and
 * profile are NOT literals here: a backend block cannot read variables, so they
 * come from `backend.hcl` (gitignored; `make bootstrap` writes it, see
 * backend.hcl.example) via `terraform init -backend-config=backend.hcl`.
 */
terraform {
  backend "s3" {
    key          = "envs/dev/ui/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
