/**
 * modules/ecs-cluster — one cluster, BOTH capacity providers (ADR-0021)
 *
 * ECS has exactly two built-in Fargate capacity providers: FARGATE and
 * FARGATE_SPOT (reserved names; custom providers are EC2-ASG only). So the
 * cluster registers:
 *   - FARGATE       (baseline, On-Demand) — harness workers (weight (1,0))
 *   - FARGATE_SPOT                         — registered, weight 0 (ADR-0021)
 *   - EC2 (via ASG)                        — eval workers (privileged DinD)
 *
 * The harness-worker purchase option is a WEIGHT, not a provider swap
 * (ADR-0018 "size is a variable"): FARGATE weight 1 / FARGATE_SPOT weight 0 =
 * On-Demand. Flip to Spot later = change the weight in the harness-worker
 * service to (0,1). Eval stays on EC2 (fixed).
 *
 * The EC2 capacity provider's ASG hosts the eval workers:
 *   - Bottlerocket AMI, NVMe instance-store (c5d/m5d families), small EBS boot
 *   - spot/mixed instance types for the eval fleet (ADR-0021 Consequences)
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
  description = "Private subnet IDs for the EC2 ASG + Fargate auvs."
}

variable "asg_max_size" {
  type        = number
  default     = 1
  description = "Dev: ASG max 1-2, no spot diversification (ADR-0014). Scale raises it."
}

variable "asg_desired" {
  type        = number
  default     = 0
  description = "Eval host ASG desired. Dev: 0 (OFF by default — scaled up by envs/dev/eval/, PA-10). Scale raises it. Post-apply A-2: default was 1, which left a c5d.large permanently on (~$81/month)."
}

variable "asg_instance_types" {
  type        = list(string)
  default     = ["c5d.large"]
  description = "NVMe instance-store types for eval (arch §6.1). One type = plain launch template; more = mixed-instances policy over ALL of them (spot when asg_spot). Every type listed must admit EXACTLY EVAL_TASKS_PER_HOST eval tasks under the eval task reservation (2048 CPU / 3584 MiB): the scaler packs by that constant, not per type. Today CPU is the binding axis — 4 x 2048 = 8192 = an 8-vCPU host — so an 8-vCPU type with >= 16 GiB qualifies even if its memory would admit more (m5d.2xlarge's 32 GiB admits eight by memory; the CPU axis still pins it to four). A type with fewer vCPUs or < 14,336 MiB registered breaks the contract silently (ECS places three, the scaler divides by four)."
  validation {
    condition     = length(var.asg_instance_types) >= 1
    error_message = "asg_instance_types must list at least one instance type."
  }
}

variable "asg_spot" {
  type        = bool
  default     = false
  description = "Launch the eval pool as SPOT. Dev image-build host opts in for cost; an interruption mid-build is the trade and warm restartability (partial-warm, ECR) absorbs it. Scale env stays mixed-spot per ADR-0021."
}

# Stage 1 (agreed-architecture-changes §1): docker's data-root moved OFF the
# EBS DATA volume onto instance-store NVMe via [settings.bootstrap-commands]
# (see user_data below). xvdb therefore returns to the Bottlerocket baseline —
# it only holds the OS runtime state now, not the docker store. The 200 GB grow
# was the workaround for the namespace-scoped bootstrap-container disproof,
# which Stage 1 replaces with the native ephemeral-storage API.
variable "asg_data_volume_size_gb" {
  type        = number
  default     = 20
  description = "Size (GB) of the Bottlerocket DATA (/dev/xvdb) EBS volume. With docker's data-root on instance-store NVMe (Stage 1) this holds only OS runtime state; 20 = the AMI default. Scale may raise it."
}

# Reviewer F4: explicit SG for the eval build host. Before this, hosts had no
# SG (nil -> VPC default), which (a) is worth closing on its own and (b) meant
# the git mirror's ingress (task-SG-only) denied the host's build containers.
resource "aws_security_group" "eval_host" {
  name_prefix = "${var.name_prefix}-eval-host-"
  vpc_id      = var.vpc_id
  description = "eval build host: can egress anywhere (image pulls, mirror, ECR, logs); no inbound exposed"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = {
    Name    = "${var.name_prefix}-eval-host"
    purpose = var.purpose
  }
}

resource "aws_ecs_cluster" "main" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name    = "${var.name_prefix}-cluster"
    purpose = var.purpose
  }
}

# --- EC2 capacity provider (eval workers, privileged DinD) --------------------
# B-1 (A7-A8 review): the old AMI filter selected a Bottlerocket KUBERNETES
# variant (bottlerocket-aws-k8s-*-x86_64-*), which has NO ECS agent — the host
# never registered (the real A-3 cause) and the [settings.ecs] block had nothing
# to act on. The maintained, deterministic pointer is the SSM parameter for the
# aws-ecs-2 x86_64 variant (v1.64.0, verified 2026-08-14; an ECS agent is
# present and it supports privileged DinD). `most_recent` on a name filter was
# also non-deterministic (which k8s minor / GPU / FIPS release Amazon happened
# to publish latest).
data "aws_ssm_parameter" "bottlerocket_ecs" {
  name = "/aws/service/bottlerocket/aws-ecs-2/x86_64/latest/image_id"
}

resource "aws_iam_instance_profile" "ecs_agent" {
  name = "${var.name_prefix}-ecs-agent-profile"
  role = aws_iam_role.ecs_agent.name
}

resource "aws_iam_role" "ecs_agent" {
  name = "${var.name_prefix}-ecs-agent"

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

# dev/BUILDER3B-EVAL-RESOURCE-INSTRUMENTATION-2026-08-31.md "Separate, tiny,
# different owner": SSM access to the eval host.  Without this the instance
# profile has ONLY the ECS-agent role (no AmazonSSMManagedInstanceCore), so
# nobody can get onto the eval box to run `docker stats` / debug a grade —
# the instrumentation doc was written precisely because that visibility did
# not exist.  Bottlerocket's SSM agent + the ssmmessages endpoint need this
# policy; the ECS role is untouched.
resource "aws_iam_role_policy_attachment" "ecs_agent_ssm" {
  role       = aws_iam_role.ecs_agent.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_launch_template" "eval" {
  name = "${var.name_prefix}-eval-lt"
  # Item 2 (5a-i close): the template's DEFAULT version had drifted to an old,
  # broken build (B-1's K8s AMI + no instance profile) while the ASG used
  # $Latest, hiding it. update_default_version keeps the newest version the
  # default on every apply, so the footgun cannot come back from code.
  update_default_version = true
  image_id               = data.aws_ssm_parameter.bottlerocket_ecs.value
  instance_type          = var.asg_instance_types[0]

  # 5b disk (2026-08-17): the eval build host's docker data-root lives on
  # Bottlerocket's DATA volume (AMI default /dev/xvdb = 20 GB), which is the
  # ~18.6 GB ceiling that made the warm-job disk-gate fail. Bottlerocket AUTO-
  # GROWS xvdb's data partition to the EBS volume size on boot (GH #3613:
  # repart-local + systemd-growfs), so sizing xvdb down here IS the lever —
  # no fragile bootstrap-container mount needed (that approach's `mount`
  # proved namespace-scoped: mkfs on the NVMe persisted, the bind did not).
  # gp3 200 GB ~ $0.02/hr while the build host is up (it scales to zero).
  block_device_mappings {
    device_name = "/dev/xvdb"
    ebs {
      volume_size           = var.asg_data_volume_size_gb
      volume_type           = "gp3"
      delete_on_termination = true
      encrypted             = true
    }
  }

  # Spot for the dev image-build pool (asg_spot). Overrides the on-demand
  # default without touching mixed-instances/scale posture.
  # BUILDER4-EVAL-PACKING-2026-09-03: only on the SINGLE-type path — a launch
  # template that carries InstanceMarketOptions is rejected inside a
  # MixedInstancesPolicy; with >1 types the market comes from the policy's
  # instances_distribution below (on-demand percentage 0 = all spot).
  dynamic "instance_market_options" {
    for_each = var.asg_spot && length(var.asg_instance_types) == 1 ? [1] : []
    content {
      market_type = "spot"
    }
  }

  # A-3 root cause (closed 2026-08-14): the ECS-agent instance profile was
  # defined but never attached here, so the agent had no credentials to
  # register (live instance showed IamInstanceProfile=null). The ECS agent on
  # Bottlerocket NEEDS this profile to authenticate with the cluster.
  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_agent.name
  }
  # Reviewer F4: give the eval host an EXPLICIT SG (not the VPC default). The
  # git mirror admits this SG so the eval-side instance-image build (which runs
  # on the host daemon bridge, sourced from the host ENI) can reach it.
  vpc_security_group_ids = [aws_security_group.eval_host.id]
  # Bottlerocket aws-ecs-2 variant (B-1). Stage 1 (agreed-architecture-changes
  # §1): docker's data-root goes on instance-store NVMe via Bottlerocket's
  # NATIVE ephemeral-storage API — NOT a hand-rolled bootstrap-container. The
  # 5b attempt hand-rolled it: "mkfs persisted, the /var/lib/docker bind did
  # not" is precisely the signature of missing `mount --make-rshared`
  # propagation, and the grown-EBS workaround that followed is what it was.
  # [settings.bootstrap-commands] is the documented path (>= 1.22.x): `init`
  # discovers the ephemeral disks and RAID-0s them automatically; `bind`
  # rehomes the runtime state. The disk fix ("grown EBS") is thereby retired —
  # asg_data_volume_size_gb returns to the baseline 20 GB.
  # user_data [settings.ecs]: `cluster` makes the host join the cluster (A-3);
  # `allow-privileged-containers` defaults to false and MUST be true for the eval
  # worker's privileged DinD (B-1). Verified against the Bottlerocket ECS settings
  # reference — there is no `enable` key on this variant (the earlier user_data
  # had an invalid one).
  user_data = base64encode(<<-EOT
    [settings.ecs]
    cluster = "${var.name_prefix}-cluster"
    allow-privileged-containers = true
    # BUILDER4-EVAL-PACKING-2026-09-03 §6: on the Spot two-minute interruption
    # notice the agent sets this host DRAINING, so its tasks get a SIGTERM (the
    # eval worker hands a mid-grade job straight back to the queue on it)
    # instead of vanishing when the instance is reclaimed.
    enable-spot-instance-draining = true

    # Stage 1: rehome the container/daemon runtime to the NVMe instance store.
    # `init` RAID-0s the ephemeral disks; `bind` moves containerd, docker and
    # the ECS logs onto them. `mode = "always"` re-applies on every boot (e.g.
    # after a spot interruption / instance replacement).
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
      Name      = "${var.name_prefix}-eval-host"
      purpose   = var.purpose
      ManagedBy = "terraform"
    }
  }
}

resource "aws_autoscaling_group" "eval" {
  name = "${var.name_prefix}-eval-asg"

  vpc_zone_identifier = var.private_subnet_ids
  min_size            = 0
  max_size            = var.asg_max_size
  desired_capacity    = var.asg_desired

  # AZRebalance suspended (2026-09-06, owner decision; same as the build ASG in
  # modules/ecs-cluster-build): the eval autoscaler owns desired capacity and
  # the hosts run in-flight grades, so an AZRebalance-initiated launch+terminate
  # only ever churns a host mid-grade (a redelivered re-grade, cheap but not
  # free) and wastes the replacement's warm-up. Seen during the 500-instance
  # gold gate on 2026-09-06: no failures, pure waste. Spot interruptions are
  # unaffected (capacity_rebalance is a separate mechanism, still off).
  suspended_processes = ["AZRebalance"]

  # Single-type path: plain launch template. `launch_template` and
  # `mixed_instances_policy` are mutually exclusive on the ASG resource, so
  # exactly one of the two blocks below renders.
  dynamic "launch_template" {
    for_each = length(var.asg_instance_types) == 1 ? [1] : []
    content {
      id      = aws_launch_template.eval.id
      version = "$Latest"
    }
  }

  # Mixed instances / spot for scale (ADR-0021); dev kept a single type until
  # BUILDER4-EVAL-PACKING-2026-09-03: a spot-only fleet on ONE instance type
  # stalls at `no_hosts` when that spot pool thins, so the eval ASG now lists
  # NVMe siblings of the same shape (8 vCPU / ≥16 GiB) and lets the allocator
  # pick. Before this the block rendered only types[0] as its override — a
  # policy that never actually diversified — and coexisted with the top-level
  # launch_template it conflicts with; never exercised because dev was
  # single-type. `price-capacity-optimized` prefers the pools least likely to
  # be interrupted (an interruption mid-grade is one redelivered re-grade,
  # cheap but not free); asg_spot=false makes the fleet on-demand across the
  # same types.
  dynamic "mixed_instances_policy" {
    for_each = length(var.asg_instance_types) > 1 ? [1] : []
    content {
      instances_distribution {
        on_demand_allocation_strategy            = "lowest-price"
        on_demand_base_capacity                  = 0
        on_demand_percentage_above_base_capacity = var.asg_spot ? 0 : 100
        spot_allocation_strategy                 = "price-capacity-optimized"
      }
      launch_template {
        launch_template_specification {
          launch_template_id = aws_launch_template.eval.id
          version            = "$Latest"
        }
        dynamic "override" {
          for_each = var.asg_instance_types
          content {
            instance_type = override.value
          }
        }
      }
    }
  }

  tag {
    key                 = "Name"
    value               = "${var.name_prefix}-eval-asg"
    propagate_at_launch = true
  }
  tag {
    key                 = "purpose"
    value               = var.purpose
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "ec2" {
  name = "${var.name_prefix}-ec2-capacity-provider"

  auto_scaling_group_provider {
    auto_scaling_group_arn = aws_autoscaling_group.eval.arn
    # managed_scaling is DISABLED for envs/dev (post-apply A-2): with it ENABLED,
    # ECS owns the ASG desired count and holds a host at target_capacity even
    # with zero tasks — measured live at ~$81/month, and it silently overrides
    # asg_desired=0. The design (PA-10 / ADR-0014) is an eval host OFF by default,
    # scaled up by envs/dev/eval/ when it applies a service. envs/scale can turn
    # managed scaling back on (variable or a dedicated scale provider).
    managed_scaling {
      maximum_scaling_step_size = 2
      minimum_scaling_step_size = 1
      status                    = "DISABLED"
      target_capacity           = 80
    }
  }

  tags = {
    Name    = "${var.name_prefix}-ec2-capacity-provider"
    purpose = var.purpose
  }
}

# --- Register FARGATE + FARGATE_SPOT (both are built-in Fargate providers) ----
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = [
    "FARGATE",
    "FARGATE_SPOT",
    aws_ecs_capacity_provider.ec2.name,
  ]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}
output "cluster_id" {
  value = aws_ecs_cluster.main.id
}
output "ec2_capacity_provider" {
  value = aws_ecs_capacity_provider.ec2.name
}
output "eval_host_sg_id" {
  value = aws_security_group.eval_host.id
}

output "eval_asg_arn" {
  value = aws_autoscaling_group.eval.arn
}
output "eval_asg_name" {
  value = aws_autoscaling_group.eval.name
}