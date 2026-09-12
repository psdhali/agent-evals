/**
 * modules/database — ONE Aurora Serverless v2 cluster with TWO databases (ADR-0022)
 *
 * The only resident of envs/dev/persistent/ — the sole teardown exception
 * (ADR-0014). Built to ADR-0022:
 *   - one cluster, databases litellm_spend + app_control_plane, two roles
 *   - min_capacity = 0, SecondsUntilAutoPause = 300 (the minimum)
 *   - standard storage (NOT I/O-Optimized: +33% ACU, +$0.225/GB-mo, no benefit
 *     at ~130 writes/sec peak)
 *   - `skip_final_snapshot = true` is DEV ONLY; `deletion_protection = true`
 *     by default (PA-2 — the record is not dev-disposable, so it is ON; only
 *     envs/scale overrides to false).
 *
 * min_capacity=0 was the unspecified setting that put two clusters at
 * $263/quarter (263% of the budget) while idle (review C-1). DoD 6 verifies the
 * applied state.
 *
 * M-1 (mid-phase review): an Aurora Serverless v2 cluster is NOT enough — it is
 * the storage/endpoint layer. It needs at least one `aws_rds_cluster_instance`
 * with `instance_class = "db.serverless"` to provide compute; without one the
 * cluster applies cleanly, resolves in DNS, and accepts NO connection. The
 * instance is what `serverlessv2_scaling_configuration` scales.
 *
 * M-2 (mid-phase review): the second database (litellm_spend) is NOT created by
 * Terraform. The earlier local-exec psql was removed — a laptop `psql` into a
 * private Aurora with no bastion cannot work (review M-2). The second database
 * is created by the APPLICATION on startup (the control-plane, inside the VPC,
 * with credentials from Secrets Manager), the same way migrations already run.
 * Terraform provisions the cluster + cluster instance + the app_control_plane
 * database (via `database_name`); the app owns the litellm_spend schema.
 *
 * M-4 (mid-phase review): Aurora gets a DEDICATED DB security group here (in
 * persistent/, the same state as the cluster) with NO ingress rules. The ingress
 * rule itself is authored in ephemeral/ against this SG id from remote state,
 * sourced from the ephemeral task SG — so the rule is created/destroyed with
 * ephemeral/, which is the correct lifecycle and avoids the circular-dependency
 * of ephemeral reading persistent.
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
  description = "Resource name prefix, e.g. 'eval-dev'."
}

variable "purpose" {
  type        = string
  description = "'dev' or 'scale'."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs (network module)."
}

variable "master_password_secret_arn" {
  type        = string
  description = "ARN of the Secrets Manager secret holding the master password."
}

variable "skip_final_snapshot" {
  type        = bool
  default     = true
  description = "dev only (F-9): destroy must not hang on a snapshot prompt. Safe ONLY because PA-2 sets backup_retention_period=7 (automated backups exist). scale/ sets false."
}

variable "deletion_protection" {
  type        = bool
  default     = true
  description = "PA-2: protects the publishable record from a mistaken destroy. Previously defaulted false 'for dev'; the record is not dev-disposable, so it is ON."
}

variable "backup_retention_period" {
  type        = number
  default     = 7
  description = "PA-2: Aurora's default is 1 day, too thin for published data. 7 days near-free at this volume."
}

variable "max_capacity" {
  type        = number
  default     = 4
  description = "Aurora Serverless v2 max ACU. A-7b (post-apply review): dev was 8 (a ~$700/month ceiling at $0.12/ACU-hr vs the $33/month budget; measured peak ~2 ACU). 4 for dev; 8-16 belongs in envs/scale (size is a variable, ADR-0018)."
}

locals {
  min_capacity  = 0
  max_capacity  = var.max_capacity
  pause_seconds = 300
}

data "aws_secretsmanager_secret_version" "master" {
  secret_id = var.master_password_secret_arn
}

# --- Dedicated DB security group (M-4): no ingress here; ephemeral adds it -----
resource "aws_security_group" "db" {
  name        = "${var.name_prefix}-aurora-db-sg"
  description = "Aurora: no ingress defined here - the ingress rule is authored in envs/dev/ephemeral against this SG id (M-4), sourced from the task SG, so it is created and destroyed with ephemeral/."
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name    = "${var.name_prefix}-aurora-db-sg"
    purpose = var.purpose
  }
}

variable "vpc_id" {
  type        = string
  description = "VPC id (for the dedicated DB SG)."
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.name_prefix}-aurora-subnet-group"
  subnet_ids = var.subnet_ids

  tags = {
    Name    = "${var.name_prefix}-aurora-subnet-group"
    purpose = var.purpose
  }
}

# S-1: the Aurora engine version is validated by the RDS API only at CREATE,
# so `validate`/`plan` can't catch a bad pin — the apply would fail AFTER the
# VPC/subnets/NAT/EIP already exist. Specifying the version via this data source
# makes an invalid version fail at PLAN instead. 16.13 was re-checked against
# `aws rds describe-db-engine-versions --engine aurora-postgresql` on
# 2026-08-13 (16.8/16.9/16.10/16.11/16.13/16.14 available in us-west-2).
data "aws_rds_engine_version" "aurora" {
  engine  = "aurora-postgresql"
  version = "16.13" # pinned; fail at plan if it ever drops out of service
}

resource "aws_rds_cluster" "main" {
  cluster_identifier = "${var.name_prefix}-aurora-v2"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = data.aws_rds_engine_version.aurora.version
  database_name      = "app_control_plane" # DB 1 (control-plane) is the cluster's default DB
  master_username    = "eval"
  master_password    = data.aws_secretsmanager_secret_version.master.secret_string

  serverlessv2_scaling_configuration {
    min_capacity             = local.min_capacity
    max_capacity             = local.max_capacity
    seconds_until_auto_pause = local.pause_seconds
  }

  storage_type          = "aurora" # standard, not aurora-iopt1 (I/O-Optimized)
  skip_final_snapshot   = var.skip_final_snapshot
  deletion_protection   = var.deletion_protection
  copy_tags_to_snapshot = true

  # enable_http_endpoint enables the RDS Data API on this cluster, used by the
  # nightly scale-to-zero Lambda's guarded Postgres check (G-1/teardown-guard):
  # the guard queries `runs` over HTTP via rds-data:ExecuteStatement, so the
  # Lambda needs no psycopg2 wheel and no VPC attachment. A PAUSED cluster makes
  # that call error -> the guard refuses to tear down (fail-safe). Data API is
  # free; it is gated on the cluster being awake to answer. Default ON here (dev
  # needs it); scale/ can leave it on harmlessly.
  enable_http_endpoint = true

  # PA-1 (persistent-actions): encryption at rest — cannot be enabled on an
  # existing cluster (snapshot→restore→repoint), so it must be set before the
  # first apply. Same class as the VPC /16: free now, painful later. An
  # AWS-managed KMS key costs nothing extra.
  storage_encrypted = true

  # PA-2 (persistent-actions): the cluster holds every publishable result —
  # 7-day automated backups (Aurora's default is 1 day) so the record is
  # recoverable, not just 'we don't run destroy in persistent/'. Rendered safe
  # with skip_final_snapshot only because automated backups now exist.
  backup_retention_period = var.backup_retention_period

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]

  lifecycle {
    ignore_changes = [master_password]
  }

  tags = {
    Name    = "${var.name_prefix}-aurora-v2"
    purpose = var.purpose
  }
}

# M-1: the compute that Serverless v2 scales. Without this the cluster applies,
# resolves in DNS, and accepts no connection (min_capacity=0 scales nothing).
resource "aws_rds_cluster_instance" "main" {
  count = 1 # a variable at scale/

  cluster_identifier           = aws_rds_cluster.main.id
  instance_class               = "db.serverless"
  engine                       = aws_rds_cluster.main.engine
  engine_version               = aws_rds_cluster.main.engine_version
  performance_insights_enabled = false # dev; it bills
  db_subnet_group_name         = aws_db_subnet_group.main.name

  tags = {
    Name    = "${var.name_prefix}-aurora-v2-instance"
    purpose = var.purpose
  }
}

output "cluster_id" {
  value = aws_rds_cluster.main.id
}
output "cluster_arn" {
  value = aws_rds_cluster.main.arn
}
output "endpoint" {
  value = aws_rds_cluster.main.endpoint
}
output "reader_endpoint" {
  value = aws_rds_cluster.main.reader_endpoint
}
output "port" {
  value = aws_rds_cluster.main.port
}
output "db_security_group_id" {
  value = aws_security_group.db.id
}
