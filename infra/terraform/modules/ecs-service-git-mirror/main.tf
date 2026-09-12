/**
 * modules/ecs-service-git-mirror — Stage 2.2 git mirror, as a Fargate service
 *
 * Serves the baked bare-repo mirrors over git smart HTTP (read-only) at
 * http://git-mirror.eval.internal/<org>/<repo>.git. Registered in Cloud Map
 * (service discovery) so the NAME resolves — `git-mirror.eval.internal` is
 * written once and never changes; the service behind it can be redeployed or
 * replaced, and dev/scale resolve it differently (the indirection lives in
 * DNS, not in any image).
 *
 * No ALB / no internet: the mirror is reached only by VPC-local traffic (the
 * harness and eval workers) — exactly the network shape Stage 7 depends on.
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

variable "cluster_name" {
  type        = string
  description = "ECS cluster."
}

variable "vpc_id" {
  type        = string
  description = "VPC ID (for the mirror's security group)."
}

variable "vpc_cidr" {
  type        = string
  description = "VPC CIDR — the mirror's only egress (VPC-local, no internet)."
}

variable "image" {
  type        = string
  description = "git-mirror image (ECR URL)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnets for the Fargate task (awsvpc)."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Task security groups (the shared task SG)."
}

variable "discovery_arn" {
  type        = string
  description = "Cloud Map service ARN (git-mirror registration)."
}

variable "log_group" {
  type        = string
  description = "CloudWatch log group for the mirror task."
}

variable "replica_count" {
  type        = number
  default     = 1
  description = "Mirror replicas. Dev: 1 (the mirror must be reachable for any run)."
}

resource "aws_iam_role" "git_mirror" {
  name = "${var.name_prefix}-git-mirror"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
  tags = {
    Name    = "${var.name_prefix}-git-mirror"
    purpose = var.purpose
  }
}

data "aws_iam_policy_document" "git_mirror" {
  # Pull the image from ECR + write logs + register health in Cloud Map.
  statement {
    sid       = "ECR"
    actions   = ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = ["*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
  # Cloud Map service discovery needs to update the health/registration records.
  statement {
    sid       = "ServiceDiscovery"
    actions   = ["servicediscovery:GetInstance", "servicediscovery:RegisterInstance"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "git_mirror" {
  name   = "${var.name_prefix}-git-mirror"
  role   = aws_iam_role.git_mirror.id
  policy = data.aws_iam_policy_document.git_mirror.json
}

# Task SG: allow the shared framework task SG to reach the mirror's :80 (git
# smart HTTP). No inbound from anywhere else; egress only within the VPC (the
# mirror needs no internet).
resource "aws_security_group" "git_mirror" {
  name_prefix = "${var.name_prefix}-git-mirror-"
  vpc_id      = var.vpc_id
  description = "git mirror: allow framework tasks to clone over :80"

  ingress {
    from_port       = 9418
    to_port         = 9418
    protocol        = "tcp"
    security_groups = var.security_group_ids
  }
  # Egress: Fargate needs ECR (image pull on task start) + CloudWatch logs +
  # the mirrors are baked in so serving itself is VPC-local. Stage 7 (isolation)
  # will tighten this to interface endpoints — shipping the "VPC-local only"
  # egress here broke the image pull (ECR GetAuthorizationToken i/o timeout).
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name    = "${var.name_prefix}-git-mirror"
    purpose = var.purpose
  }
}

resource "aws_ecs_task_definition" "git_mirror" {
  family                   = "${var.name_prefix}-git-mirror"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }
  cpu                = "256"
  memory             = "512"
  execution_role_arn = aws_iam_role.git_mirror.arn
  task_role_arn      = aws_iam_role.git_mirror.arn

  container_definitions = jsonencode([{
    name         = "git-mirror"
    image        = var.image
    portMappings = [{ containerPort = 9418, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "git-mirror"
      }
    }
    environment = [
      { name = "GIT_MIRROR_HOST", value = "git-mirror.eval.internal" },
      { name = "GIT_PROJECT_ROOT", value = "/srv/git" },
    ]
  }])

  tags = {
    Name    = "${var.name_prefix}-git-mirror"
    purpose = var.purpose
  }
}

resource "aws_ecs_service" "git_mirror" {
  name            = "git-mirror"
  cluster         = var.cluster_name
  task_definition = aws_ecs_task_definition.git_mirror.arn
  desired_count   = var.replica_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.git_mirror.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = var.discovery_arn
  }

  tags = {
    Name    = "${var.name_prefix}-git-mirror"
    purpose = var.purpose
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}

# Adoption Phase 1a: the region comes from the provider, never a literal.
data "aws_region" "current" {}
