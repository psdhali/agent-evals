/**
 * modules/security-groups — SG wiring for the service layer
 *
 * Split into the groups that services and ALBs consume:
 *   - alb:     inbound 4000 (gateway) + 8000 (api) from 0.0.0.0/0 (dev), egress all
 *   - task:    ALL services talk EACH OTHER + the DB (5432) + Redis (6379);
 *              egress all (out to the NAT for model APIs / providers)
 *   - cache:   inbound 6379 from the task SG (Valkey)
 *
 * The Aurora DB security group is NOT here. It lives in the database module
 * (persistent/, the same state as the cluster) and its ingress rule is authored
 * in envs/dev/ephemeral (M-4). A prior `db` SG in this module was unused and is
 * removed (M-9).
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

variable "vpc_id" {
  type        = string
  description = "VPC id (network module)."
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR (local to the network module) — the harness SG's VPC-local egress rule and the ALB ingress narrowing."
}

variable "region" {
  type        = string
  description = "AWS region (for the S3 managed prefix list the harness uses for the gateway endpoint)."
}

variable "purpose" {
  type        = string
  description = "'dev' or 'scale'."
}

# The harness reaches S3 exclusively via the FREE gateway endpoint; its SG must
# admit the S3 prefix list so egress to it is allowed at the SG layer as well as
# by route (A1 / ADR-0033). Non-harness services keep NAT egress and need neither.
data "aws_ec2_managed_prefix_list" "s3" {
  name = "com.amazonaws.${var.region}.s3"
}

resource "aws_security_group" "task" {
  name        = "${var.name_prefix}-tasks-sg"
  description = "All framework service tasks: full internal + egress, no external inbound."
  vpc_id      = var.vpc_id

  # internal: allow all traffic between framework resources.
  # protocol -1 (ALL) requires from_port AND to_port both 0 — 0/65535 is
  # rejected by EC2 ("must both be 0 to use the 'ALL' '-1' protocol", 1st apply).
  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }
  # 5a-ii: the ALBs are the framework's only external face (the API ALB is
  # internal, the gateway ALB is public) yet the task SG only allowed self
  # ingress — ALB→task health-check/target traffic would be blocked and every
  # service would register UNHEALTHY. Deploying with no tasks (5a-i) hid it.
  ingress {
    from_port       = 4000
    to_port         = 4000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  # egress: everything out (via NAT) — models, providers, AWS APIs
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name    = "${var.name_prefix}-tasks-sg"
    purpose = var.purpose
  }
}

# A1 / ADR-0033: the HARNESS security group. The harness runs untrusted agent
# code and must have NO path to the internet. Its egress allowlist is explicit
# and small, and is the defence-in-depth layer UNDER the route table (the
# isolated subnets' route table has no default route — that is the primary
# control). Every rule here names exactly one thing the harness legitimately
# reaches; there is no 0.0.0.0/0.
#
#   • VPC CIDR            → gateway ALB, git mirror, Valkey, all interface
#                           endpoint ENIs (implicit `local` routing)
#   • S3 prefix list      → the free S3 gateway endpoint (ECR layer data,
#                           dataset mirror, artifact upload)
#   • 169.254.169.253    → the VPC resolver (Cloud Map + internal ALB names)
#   • 169.254.170.2      → the task-role credentials endpoint
#                         (AWS_CONTAINER_CREDENTIALS_RELATIVE_URI)
#   • 169.254.169.254    → IMDS/metadata (defensive)
resource "aws_security_group" "harness" {
  name        = "${var.name_prefix}-harness-sg"
  description = "Isolated harness tasks: VPC + S3-gateway + DNS/creds egress only; no 0.0.0.0/0 (A1/ADR-0033)."
  vpc_id      = var.vpc_id

  # no ingress — the harness is a client of everything, nothing dials in.

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }
  # S3 via the gateway endpoint (HTTPS). The GET/OE/PUT/List that S3 needs.
  egress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.s3.id]
  }
  # VPC resolver (UDP + TCP 53)
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["169.254.169.253/32"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["169.254.169.253/32"]
  }
  # Task-role credentials + metadata (TCP 80, link-local)
  egress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["169.254.170.2/32", "169.254.169.254/32"]
  }

  tags = {
    Name    = "${var.name_prefix}-harness-sg"
    purpose = var.purpose
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-albs-sg"
  description = "ALBs: inbound HTTP from anywhere (dev), outbound to tasks." # unchanged — SG description is immutable (a change force-replaces the SG)
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  # 5a-ii: the actual listener ports. The api ALB listens on 8000, the gateway
  # ALB on 4000 (both in this VPC for dev). Without these the ALBs accept
  # connections but forward nothing, and health checks fail — invisible until a
  # task actually runs (5a-i had none). Both are internal, so the VPC scope is
  # exactly the client population: framework tasks + SSM-forwarded operators.
  ingress {
    from_port   = 4000
    to_port     = 4000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name    = "${var.name_prefix}-albs-sg"
    purpose = var.purpose
  }
}

resource "aws_security_group" "cache" {
  name        = "${var.name_prefix}-cache-sg"
  description = "Valkey: inbound 6379 from framework tasks." # unchanged — immutable
  vpc_id      = var.vpc_id

  # A1-2 / ADR-0033 consequences: moving the harness to its own SG broke every
  # ingress rule that named the task SG by reference. The harness's live-progress
  # writes go to Valkey, so it MUST be admitted here in the same change.
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id, aws_security_group.harness.id]
  }
  tags = {
    Name    = "${var.name_prefix}-cache-sg"
    purpose = var.purpose
  }
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}
output "alb_security_group_id" {
  value = aws_security_group.alb.id
}
output "cache_security_group_id" {
  value = aws_security_group.cache.id
}
output "harness_security_group_id" {
  value = aws_security_group.harness.id
}
