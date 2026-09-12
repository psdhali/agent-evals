/**
 * modules/ecs-cluster-build — a SEPARATE ECS cluster for image builds only
 * (builder5-image-build-tier Stage 2, revised).
 *
 * Why a whole separate cluster rather than a second capacity provider on the
 * existing eval cluster (modules/ecs-cluster): that cluster's
 * `aws_ecs_cluster_capacity_providers` resource already lists
 * `aws_ecs_capacity_provider.ec2`, which references `aws_autoscaling_group.
 * eval` — so ANY edit to that shared list forces Terraform to re-plan that
 * whole dependency chain, including the eval ASG. Live drift was found this
 * way while planning that approach (min_size/desired 1/1 live vs 0/0 in
 * config — builder 1 holding a build host up between retry attempts); the
 * full apply would have "corrected" that drift and terminated their host as
 * a side effect of a change that had nothing to do with them. A wholly
 * separate cluster has its OWN capacity-provider-list resource with zero
 * reference to anything in the eval cluster's graph, so nothing here can ever
 * touch the eval ASG, no matter what state it drifts into.
 *
 * Deliberately simpler than modules/ecs-cluster: this cluster never runs
 * Fargate tasks, so it registers only the EC2 capacity provider — no
 * FARGATE/FARGATE_SPOT registration, no purchase-option weighting to manage.
 * Its own IAM role/instance-profile and security group (not shared with the
 * eval cluster's) for the same reason: true independence, not just a
 * separate ASG under a shared blast radius.
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

variable "vpc_id" {
  type        = string
  description = "VPC ID (network module)."
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for the build ASG."
}

variable "public_subnet_ids" {
  type        = list(string)
  default     = []
  description = "Public subnet IDs (one per AZ) — used instead of private_subnet_ids when public_hosts is true."
}

variable "public_hosts" {
  type        = bool
  default     = false
  description = "Launch the build HOSTS in the public subnets with a public IPv4 (owner decision 2026-09-06). The docker daemon that pulls SWE-bench's official base images from Docker Hub (~1 TB for the 500-instance build) and pushes the -inst images to ECR runs on the HOST and uses the host's primary ENI — so with this on, that bulk traffic goes straight out the internet gateway instead of through NAT (NAT gateway: $0.045/GB ≈ $45 for the build; t3 NAT instance: 64 Mbps baseline ≈ 35 h for 1 TB). The warm-job TASK keeps awsvpc mode; its own ENI is placed by the launcher in the PRIVATE subnet of the host's AZ (its AWS API calls are small and go via the private route table + the S3 gateway endpoint). The host SG stays egress-only; a public IP with no inbound rule exposes nothing. Note map_public_ip_on_launch is false on the public subnets (they were carved for the NAT only), hence the explicit associate_public_ip_address on the launch template."
}

variable "asg_max_size" {
  type        = number
  default     = 4
  description = "Build ASG max size. builder5-image-build-tier §3: 'prove at n=2, run at n=3 or n=4' — the hard cliff is at 7 (sympy's 75-instance env can't be split further), but 4 covers the recommended run."
}

variable "asg_desired" {
  type        = number
  default     = 0
  description = "Build ASG desired. Always starts at 0 — scaled up on demand only immediately before a build run, scaled back to 0 after (docs/runbooks/dev-env-bring-up-and-tear-down.md §8). NOT covered by the nightly scale-to-zero guard today (see the comment on guard_asg_arns in envs/dev/persistent/main.tf) — this is the only thing standing between 'forgot to scale down' and a night of idle EC2 spend, so treat 0 as the default to return to, not an option."
}

variable "instance_types" {
  type        = list(string)
  default     = ["c5d.2xlarge"]
  description = "NVMe instance-store family for image builds. builder5-image-build-tier §3's m5d.2xlarge was a guess against an assumed memory-OOM risk; the reviewer corrected it (2026-08-27): the build host is ALREADY c5d.2xlarge, chosen after a REAL recorded disk-full (commit 11a6dec, c5d.xlarge's 75 GB NVMe filled by 5 env images + build cache) — disk, not memory, is the documented failure mode, and it's already fixed at this size. c5d.2xlarge is the baseline; Stage 1 measured ~2.7 GB single-build peak memory on it (light env) with disk headroom never remotely tight. T1 (matplotlib, W=4, the real C-compile stress case) is what proves or overturns this — change here if it does. Single type in dev, matches the eval pool's pattern."
}

variable "asg_spot" {
  type        = bool
  default     = true
  description = "Build pool on Spot — builds are restartable via ECR (resume-from-ECR-state, builder1-per-instance-images-build-plan.md §6), the same trade the eval build host already makes."
}

variable "data_volume_size_gb" {
  type        = number
  default     = 20
  description = "Bottlerocket DATA volume. Docker's data-root moves onto NVMe instance-store via the bootstrap-commands below, so this stays at the AMI baseline (matches the eval pool's asg_data_volume_size_gb reasoning)."
}

data "aws_ssm_parameter" "bottlerocket_ecs" {
  name = "/aws/service/bottlerocket/aws-ecs-2/x86_64/latest/image_id"
}

resource "aws_ecs_cluster" "build" {
  name = "${var.name_prefix}-build-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name    = "${var.name_prefix}-build-cluster"
    purpose = var.purpose
  }
}

# --- IAM: dedicated, not shared with the eval cluster's ecs_agent role -------
resource "aws_iam_instance_profile" "ecs_agent" {
  name = "${var.name_prefix}-build-ecs-agent-profile"
  role = aws_iam_role.ecs_agent.name
}

resource "aws_iam_role" "ecs_agent" {
  name = "${var.name_prefix}-build-ecs-agent"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_agent" {
  role       = aws_iam_role.ecs_agent.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

# --- Host SG: dedicated, not shared with eval_host_sg_id ---------------------
resource "aws_security_group" "build_host" {
  name_prefix = "${var.name_prefix}-build-host-"
  vpc_id      = var.vpc_id
  description = "image-build host: can egress anywhere (ECR, S3, install_repo_script internet); no inbound exposed"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name    = "${var.name_prefix}-build-host"
    purpose = var.purpose
  }
}

resource "aws_launch_template" "build" {
  name                   = "${var.name_prefix}-build-lt"
  update_default_version = true
  image_id               = data.aws_ssm_parameter.bottlerocket_ecs.value
  instance_type          = var.instance_types[0]

  # Docker's data-root is rehomed onto instance-store NVMe by the
  # bootstrap-commands below, so xvdb only ever holds Bottlerocket's own
  # runtime state (same reasoning as the eval pool's launch template).
  block_device_mappings {
    device_name = "/dev/xvdb"
    ebs {
      volume_size           = var.data_volume_size_gb
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  dynamic "instance_market_options" {
    for_each = var.asg_spot ? [1] : []
    content {
      market_type = "spot"
    }
  }

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_agent.name
  }
  # A network_interfaces block replaces vpc_security_group_ids (the two are
  # mutually exclusive on a launch template): it is the only place a launch
  # template can say associate_public_ip_address (see var.public_hosts).
  network_interfaces {
    device_index                = 0
    associate_public_ip_address = var.public_hosts
    security_groups             = [aws_security_group.build_host.id]
    delete_on_termination       = true
  }

  user_data = base64encode(<<-EOT
    [settings.ecs]
    cluster = "${var.name_prefix}-build-cluster"
    allow-privileged-containers = true

    [settings.bootstrap-commands.ecs-ephemeral-storage]
    commands = [
      ["apiclient", "ephemeral-storage", "init"],
      ["apiclient", "ephemeral-storage", "bind", "--dirs", "/var/lib/containerd", "/var/lib/docker", "/var/log/ecs"]
    ]
    essential = true
    mode = "always"
  EOT
  )

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name      = "${var.name_prefix}-build-host"
      purpose   = var.purpose
      role      = "image-build"
      ManagedBy = "terraform"
    }
  }
}

resource "aws_autoscaling_group" "build" {
  name = "${var.name_prefix}-build-asg"

  vpc_zone_identifier = var.public_hosts ? var.public_subnet_ids : var.private_subnet_ids
  min_size            = 0
  max_size            = var.asg_max_size
  desired_capacity    = var.asg_desired

  launch_template {
    id      = aws_launch_template.build.id
    version = "$Latest"
  }

  # AZRebalance suspended permanently (2026-09-01): this ASG is only ever
  # scaled manually around a discrete build run, so steady-state AZ balancing
  # buys nothing here — and with managed_draining disabled on the capacity
  # provider (below), an AZRebalance-initiated shrink terminates a host with a
  # build actively running (happened for real 2026-09-01: both hosts landed in
  # one AZ, AZRebalance launched a 3rd elsewhere, then killed an in-flight
  # build shrinking 3→2). Resume-from-ECR-state made it a re-run, not
  # corruption, but the churn is pure waste. Was suspended manually via the
  # API that day; this makes it durable.
  suspended_processes = ["AZRebalance"]

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-build-asg"
    propagate_at_launch = true
  }
  tag {
    key                 = "purpose"
    value               = var.purpose
    propagate_at_launch = true
  }
  # ECS stamps this tag (empty value, propagate=true) onto any ASG attached to
  # a capacity provider. Declaring it exactly as ECS writes it stops the
  # perpetual plan drift — undeclared, terraform wants to remove it and ECS
  # would just re-add it.
  tag {
    key                 = "AmazonECSManaged"
    value               = ""
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "build_ec2" {
  name = "${var.name_prefix}-build-capacity-provider"

  auto_scaling_group_provider {
    auto_scaling_group_arn = aws_autoscaling_group.build.arn
    # DISABLED, same reasoning as the eval provider: the build tier is scaled
    # up/down explicitly around a build run, never left ambient.
    managed_scaling {
      maximum_scaling_step_size = 4
      minimum_scaling_step_size = 1
      status                    = "DISABLED"
      target_capacity           = 80
    }
    # DISABLED (investigated live 2026-09-01, see the runbook §8 addendum):
    # AWS's default ecs-managed-draining-termination-hook doesn't actually
    # protect what matters here — a real spot reclaim kills a build anyway
    # (a 2-min hard EC2 deadline the hook can't extend), so the only thing it
    # ever intercepted in practice was an AZRebalance-initiated termination,
    # and losing that build just costs one re-run (resume-from-ECR-state
    # already makes that safe and cheap). Its own AWS-side auto-completion
    # was unreliable in this account — every teardown needed a manual
    # `aws autoscaling complete-lifecycle-action` — so it was pure teardown
    # friction for close to zero real protection on THIS tier. Plain
    # attribute, not a nested block (unlike managed_scaling above) — schema
    # confirmed via `terraform providers schema -json` against 5.100.0.
    managed_draining = "DISABLED"
  }

  tags = {
    Name    = "${var.name_prefix}-build-capacity-provider"
    purpose = var.purpose
  }
}

# This cluster never runs Fargate tasks — only the EC2 provider is registered.
resource "aws_ecs_cluster_capacity_providers" "build" {
  cluster_name = aws_ecs_cluster.build.name

  capacity_providers = [aws_ecs_capacity_provider.build_ec2.name]

  default_capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.build_ec2.name
    weight            = 1
  }
}

output "cluster_name" {
  value = aws_ecs_cluster.build.name
}
output "cluster_id" {
  value = aws_ecs_cluster.build.id
}
output "ec2_capacity_provider" {
  value = aws_ecs_capacity_provider.build_ec2.name
}
output "build_host_sg_id" {
  value = aws_security_group.build_host.id
}
output "build_asg_arn" {
  value = aws_autoscaling_group.build.arn
}
output "build_asg_name" {
  value = aws_autoscaling_group.build.name
}
