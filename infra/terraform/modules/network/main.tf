/**
 * modules/network — VPC shape, subnets, VPC endpoints, NAT
 *
 * Builds to ADR-0018's 20k-ready shape: a /16 VPC carved into /18 per AZ.
 * "IP space is free; a VPC resize is not" — the least-reversible line in the
 * whole Terraform, so /16 / /18 is set here unconditionally, not as a variable.
 *
 * Interface VPC endpoints are placed per-env: envs/dev uses ONE AZ (review
 * C-2: endpoints bill ~$0.01/hr per endpoint per AZ — an ENI in every subnet),
 * envs/scale uses all three. Placement is `endpoint_azs`, a "size" variable
 * consistent with ADR-0018's shape/size split — ADR-0004 keeps endpoints
 * unconditional in both; only their placement differs.
 *
 * NO EFS (review U1): the ADR-0010 git-mirror filesystem was removed — it held
 * 12 KB, nothing could ever populate it (the 12 repos are baked into the
 * git-mirror image), and the mirror is served by the git-daemon service. The
 * git mirror's DNS still lives here via Cloud Map (Stage 2.2, below).
 */
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  type        = string
  description = "AWS region (locked to us-west-2 per prereq-setup)."
}

variable "azs" {
  type        = list(string)
  description = "Availability zones to deploy across (dev uses 1-3; scale uses 3)."
}

variable "purpose" {
  type        = string
  description = "Cost-allocation tag value, e.g. 'dev' or 'scale' (N7 / ADR-0014)."
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix, e.g. 'eval-dev'."
}

variable "nat_enabled" {
  type        = bool
  default     = true
  description = "Whether to create a NAT instance for internet egress. envs/scale swaps to a NAT Gateway in its own root module (not here)."
}

variable "endpoint_azs" {
  type        = list(string)
  default     = []
  description = "Which AZs to place interface VPC endpoints in. dev → [az0]; scale → all three (C-2)."
}

variable "vpc_endpoint_services" {
  type        = list(string)
  default     = ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "sqs", "monitoring"]
  description = "Interface VPC endpoint services to create (ADR-0004)."
}

variable "endpoint_gateway_policy_bucket_arns" {
  type        = list(string)
  default     = []
  description = "Bucket ARNs the S3 GATEWAY endpoint policy may serve (A1-6 / ADR-0033 decision 3). Empty → open policy (legacy); persistent/ passes the framework buckets + the ECR layer bucket. This is the load-bearing piece of endpoint policy: after isolation, S3 is the harness's only remaining access, and IAM governs only SIGNED requests — an unsigned GET against any public in-region bucket would otherwise still traverse the gateway endpoint."
}

variable "ssm_tunnel_enabled" {
  type        = bool
  default     = true
  description = "Attach an SSM instance profile to the NAT instance so the internal ALBs are reachable via port-forwarding ($0, review §7/A1-5). Disabling removes the only operator path to the now-internal gateway/UI ALBs."
}

variable "nat_gateway_enabled" {
  type        = bool
  default     = true # DEFAULT ON for the e2e phase — owner decision 2026-08-29
  description = <<-EOT
    When true, a managed NAT Gateway carries private-subnet egress and the
    private route table's 0.0.0.0/0 points at it. When false, egress falls back
    to the t3 NAT instance. The instance ALWAYS exists while nat_enabled — it is
    the SSM tunnel host, not just a NAT.
  EOT
}

# --- VPC: /16, carved /18 per AZ (ADR-0018) --------------------------------

locals {
  # /16 carved into /18 per AZ: 3 AZs → 3 × /18, each ~16,382 usable ≈ 49k total.
  vpc_cidr = "10.0.0.0/16"
  # one /18 per AZ (a /16 is 2^6 /18s; carve one contiguous /18 per AZ)
  az_cidrs = [
    for i in range(3) : cidrsubnet("10.0.0.0/16", 2, i) # 0,1,2 → /18 each
  ]
  # within each /18, carve a private /19 (for compute) + public /27 (for the NAT).
  # The public /27 must come from the /18's SECOND half: the original
  # cidrsubnet(c, 8, 8) = 10.0.2.0/26 sat INSIDE the private 10.0.0.0/19, which
  # AWS rejected (InvalidSubnet.Conflict on the first apply).
  private_cidrs = [for c in local.az_cidrs : cidrsubnet(c, 1, 0)]   # /19, first half
  public_cidrs  = [for c in local.az_cidrs : cidrsubnet(c, 9, 256)] # /27, second half
  # A1 / ADR-0028 §Decision + ADR-0033: dedicated HARNESS-ISOLATED subnets, one
  # /22 per AZ, in each /18's UNALLOCATED second half. Chosen by the ADR and
  # verified non-overlapping against the carve above (reproduced the module's
  # arithmetic 2026-08-19: they sit clear of the private /19 and public /27).
  # 3 × /22 = 3,057 usable ENIs; awsvpc is one ENI per task, so ~3,000 concurrent
  # harness tasks — above full SWE-bench at total parallelism (§9 of the review).
  harness_isolated_cidrs = ["10.0.48.0/22", "10.0.112.0/22", "10.0.176.0/22"]
}

resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name      = "${var.name_prefix}-vpc"
    purpose   = var.purpose
    ManagedBy = "terraform"
  }
}

resource "aws_subnet" "private" {
  count = length(var.azs)

  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = var.azs[count.index]

  tags = {
    Name    = "${var.name_prefix}-private-${var.azs[count.index]}"
    purpose = var.purpose
    Type    = "private"
  }
}

resource "aws_subnet" "public" {
  count = length(var.azs)

  vpc_id            = aws_vpc.main.id
  cidr_block        = local.public_cidrs[count.index]
  availability_zone = var.azs[count.index]

  map_public_ip_on_launch = false

  tags = {
    Name    = "${var.name_prefix}-public-${var.azs[count.index]}"
    purpose = var.purpose
    Type    = "public"
  }
}

# A1 (ADR-0028/0033): HARNESS-ISOLATED subnets. The harness (untrusted agent
# code) is the ONLY tenant here. Their route table has NO default route — that
# absence is the load-bearing control (routing cannot silently fail open the way
# iptables on the NAT box can). Every non-harness component keeps the NAT.
resource "aws_subnet" "harness_isolated" {
  count = length(var.azs)

  vpc_id            = aws_vpc.main.id
  cidr_block        = local.harness_isolated_cidrs[count.index]
  availability_zone = var.azs[count.index]

  map_public_ip_on_launch = false

  tags = {
    Name    = "${var.name_prefix}-harness-isolated-${var.azs[count.index]}"
    purpose = var.purpose
    Type    = "harness-isolated"
  }
}

resource "aws_route_table" "harness_isolated" {
  vpc_id = aws_vpc.main.id
  # Deliberately NO 0.0.0.0/0 route: the scanned posture is "harness workers had
  # no route to the internet; the only reachable endpoints were the model gateway
  # and the git mirror service" (ADR-0033). The implicit `local` route covers the
  # gateway ALB, the git mirror, Valkey and every interface-endpoint ENI.
  tags = {
    Name    = "${var.name_prefix}-harness-isolated-rt"
    purpose = var.purpose
  }
}

resource "aws_route_table_association" "harness_isolated" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.harness_isolated[count.index].id
  route_table_id = aws_route_table.harness_isolated.id
}

resource "aws_security_group" "default" {
  name        = "${var.name_prefix}-default-sg"
  description = "Default VPC security group."
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.name_prefix}-default-sg"
    purpose = var.purpose
  }
}

# --- Internet egress: NAT instance (ADR-0004) --------------------------------
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name    = "${var.name_prefix}-igw"
    purpose = var.purpose
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name    = "${var.name_prefix}-public-rt"
    purpose = var.purpose
  }
}

resource "aws_route_table_association" "public" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  count  = var.nat_enabled ? 1 : 0
  domain = "vpc"

  tags = {
    Name    = "${var.name_prefix}-nat-eip"
    purpose = var.purpose
  }
}

resource "aws_security_group" "nat" {
  count  = var.nat_enabled ? 1 : 0
  name   = "${var.name_prefix}-nat-sg"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = local.private_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.name_prefix}-nat-sg"
    purpose = var.purpose
  }
}

resource "aws_instance" "nat" {
  count = var.nat_enabled ? 1 : 0

  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.nat[0].id]
  source_dest_check      = false
  # A1-5 / review §7: the operator tunnel to the (now internal) ALBs goes
  # through this box. Attaching an instance profile is an IN-PLACE update (the
  # reviewer verified; do NOT touch user_data in the same change — this resource
  # has user_data_replace_on_change = true and would be rebuilt).
  iam_instance_profile = var.ssm_tunnel_enabled ? aws_iam_instance_profile.nat[0].id : null
  # J-1 (nat-user-data-never-ran): without this, changing user_data updates the
  # ATTRIBUTE and leaves the instance in place — cloud-init runs user scripts
  # once per instance, so the new script silently never executes (verified:
  # stop/start, 55ms modules:final, no dnf/iptables in the boot log). Must
  # replace the instance on script change.
  user_data_replace_on_change = true
  # AL2 is EOL (2026-06-30) — this is AL2023. The AMI name-filter was the
  # B-1-determinism shape; AL2023 comes from the maintained SSM parameter.
  # AL2023 does NOT ship `iptables` (nftables-based; probe confirmed
  # `command not found`), so user_data must `dnf install` it first. AL2023 EOL:
  # June 2029.
  # I-1: AL2023 renames eth0->ens5; derive the interface, never name it.
  # J-2: the config must SURVIVE a reboot — sysctl -w and iptables -A are
  # runtime-only, so write /etc/sysctl.d/99-nat.conf and iptables-save to
  # /etc/sysconfig/iptables with the iptables service enabled to restore the
  # table on boot. ORDER matters: save AFTER adding the rule.
  user_data = <<-EOT
    #!/bin/bash
    set -e
    dnf install -y iptables iptables-services
    echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-nat.conf
    sysctl -p /etc/sysctl.d/99-nat.conf
    IFACE=$(ip -o -4 route show to default | awk '{print $5}')
    iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE
    iptables-save > /etc/sysconfig/iptables
    systemctl enable --now iptables
  EOT

  tags = {
    Name    = "${var.name_prefix}-nat-instance"
    purpose = var.purpose
  }

  # 2026-08-31 (NAT-Gateway apply attempt): data.aws_ssm_parameter.al2023
  # deliberately tracks "latest AL2023" (not a pinned AMI id) so a genuinely
  # NEW instance always gets a maintained, patched image — that choice stays.
  # But `ami` isn't itself in user_data_replace_on_change's blast radius, and
  # AWS republishes that SSM parameter to a newer AMI on its own schedule —
  # so on ANY future plan/apply against this module, drift alone (nobody
  # changed a single line here) would force-replace an already-running,
  # stateful instance: new instance id, new ENI, and it kills the SSM tunnel
  # (the only operator path to the internal ALBs). Caught live: an unrelated
  # NAT-Gateway change's plan tried to replace this instance for exactly that
  # reason. `ignore_changes = [ami]` keeps the "resolve latest at genuine
  # create/recreate" behavior while stopping a moved pointer alone from ever
  # triggering an unwanted rebuild of a running instance.
  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_eip_association" "nat" {
  count         = var.nat_enabled ? 1 : 0
  instance_id   = aws_instance.nat[0].id
  allocation_id = aws_eip.nat[0].id
}

# ADR-0004 Consequences: "the NAT instance needs basic resilience ... since,
# unlike NAT Gateway, AWS does not manage its availability." Free EC2 auto-
# recovery on the underlying-host failure metric — keeps the same instance id
# and ENI, so neither the private route (when in instance mode) nor the SSM
# tunnel target (§0.5 — always in instance mode) breaks. Does NOT cover
# OS-level failure or CPU-credit exhaustion; do not describe this as HA.
resource "aws_cloudwatch_metric_alarm" "nat_instance_recovery" {
  count               = var.nat_enabled ? 1 : 0
  alarm_name          = "${var.name_prefix}-nat-instance-recovery"
  alarm_description   = "Auto-recovers the NAT/SSM-tunnel instance on StatusCheckFailed_System (host-level failure only)."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed_System"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  dimensions = {
    InstanceId = aws_instance.nat[0].id
  }
  alarm_actions = ["arn:aws:automate:${var.region}:ec2:recover"]

  tags = {
    Name    = "${var.name_prefix}-nat-instance-recovery"
    purpose = var.purpose
  }
}

# --- A1-5 / review §7: SSM on the NAT instance (the operator tunnel) ----------
# $0, no new hosts. AL2023 ships the SSM agent; the managed-instance core policy
# is all the profile needs. The SG already egresses to the SSM endpoints via the
# IGW. The custom SCOTED document for the reviewer role lives in the tiers that
# own each ALB's DNS (eval/ → gateway:4000), because IAM cannot condition on SSM
# document parameters and the document ARN is the only scopeable handle.
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "nat_ssm" {
  count              = var.ssm_tunnel_enabled && var.nat_enabled ? 1 : 0
  name               = "${var.name_prefix}-nat-ssm"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags = {
    Name    = "${var.name_prefix}-nat-ssm"
    purpose = var.purpose
  }
}

resource "aws_iam_role_policy_attachment" "nat_ssm" {
  count      = var.ssm_tunnel_enabled && var.nat_enabled ? 1 : 0
  role       = aws_iam_role.nat_ssm[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "nat" {
  count = var.ssm_tunnel_enabled && var.nat_enabled ? 1 : 0
  name  = "${var.name_prefix}-nat-ssm-profile"
  role  = aws_iam_role.nat_ssm[0].name
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name    = "${var.name_prefix}-private-rt"
    purpose = var.purpose
  }
}

resource "aws_route" "private_nat" {
  count                  = var.nat_enabled && !var.nat_gateway_enabled ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  network_interface_id   = aws_instance.nat[0].primary_network_interface_id
}

# --- NAT Gateway alternative (owner decision 2026-08-29: default ON for the
# e2e phase — a $625 inference run must not depend on a single self-managed,
# burstable EC2 instance with no auto-recovery). The t3 instance in
# aws_instance.nat above ALWAYS stays: it is the SSM tunnel host (§0.5 of
# BUILDER4-NAT-GATEWAY-IMPLEMENTATION-2026-08-29.md), not swapped out. Only
# the private route table's default route moves between the two.
#
# EIP is counted on nat_enabled, NOT nat_gateway_enabled — deliberate. A
# gateway recreated with a fresh EIP gets a NEW public egress IP each time. An
# unattached EIP costs the same $0.005/hr as an attached one, so holding it is
# free in practice and keeps the egress IP stable across toggles — that
# matters the moment a provider allowlists us (BYOK is exactly that
# conversation).
resource "aws_eip" "nat_gw" {
  count  = var.nat_enabled ? 1 : 0
  domain = "vpc"

  tags = {
    Name    = "${var.name_prefix}-nat-gw-eip"
    purpose = var.purpose
  }
}

resource "aws_nat_gateway" "main" {
  count         = var.nat_enabled && var.nat_gateway_enabled ? 1 : 0
  allocation_id = aws_eip.nat_gw[0].id
  subnet_id     = aws_subnet.public[0].id # same AZ as the NAT instance

  tags = {
    Name    = "${var.name_prefix}-nat-gw"
    purpose = var.purpose
  }
}

# AWS permits exactly one route per destination CIDR per route table, so this
# resource's count and aws_route.private_nat's count above must be strictly
# opposing (the `&&`/`!` pair guarantees exactly one exists) — both counted 1
# at once fails apply with RouteAlreadyExists.
resource "aws_route" "private_nat_gw" {
  count                  = var.nat_enabled && var.nat_gateway_enabled ? 1 : 0
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}

resource "aws_route_table_association" "private" {
  count          = length(var.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Endpoint security group (A1-9 / ADR-0033): ONE shape for every interface
# endpoint. The module's endpoints previously attached `aws_security_group.default`,
# which has NO ingress — so no task could reach any endpoint, and envs/scale's six
# all-AZ endpoints were decorative. `10.0.0.0/16` inbound 443 admits everything
# inside the VPC (the only place the endpoints are meaningfully reachable); the
# endpoints expose one AWS service each, and their per-service API requires IAM
# (S3, the one service that does not, is served by the GATEWAY endpoint, whose
# policy is resource-scoped below — not by any interface endpoint).
resource "aws_security_group" "endpoint" {
  name        = "${var.name_prefix}-vpce-sg"
  description = "Interface VPC endpoints: inbound 443 from the VPC only (framework tasks incl. the isolated harness subnet)."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [local.vpc_cidr]
  }

  tags = {
    Name    = "${var.name_prefix}-vpce-sg"
    purpose = var.purpose
  }
}

# --- Interface VPC endpoints (ADR-0004; placement per C-2) -------------------
# Endpoints in the AZs chosen by `endpoint_azs` (dev → 0 [eval tier owns its
# own five, destroyed with the run], scale → all), on the private subnet of each
# chosen AZ, behind the endpoint SG (A1-9).
resource "aws_vpc_endpoint" "interface" {
  count = length(var.vpc_endpoint_services)

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${var.vpc_endpoint_services[count.index]}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for az in var.endpoint_azs : aws_subnet.private[index(var.azs, az)].id]
  security_group_ids  = [aws_security_group.endpoint.id]
  private_dns_enabled = true

  tags = {
    Name    = "${var.name_prefix}-vpce-${var.vpc_endpoint_services[count.index]}"
    purpose = var.purpose
  }
}

# The free S3 gateway endpoint. Carries the PRIVATE route table (all services)
# AND the isolated harness route table (A1: the path for ECR layer data, the
# dataset mirror and artifact upload with no default route). Its policy is the
# load-bearing endpoint policy (A1-6 / ADR-0033): with isolation, S3 is the only
# egress left, and IAM governs only *signed* requests — an unsigned GET against
# any public in-region bucket would still traverse this endpoint.
locals {
  endpoint_gateway_policy_resources = distinct(flatten([
    for arn in var.endpoint_gateway_policy_bucket_arns : [arn, "${arn}/*"]
  ]))
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids = [
    aws_route_table.private.id,
    aws_route_table.harness_isolated.id,
  ]

  # Explicit Allow on the framework buckets + the ECR layer bucket; everything
  # else is implicitly denied (an endpoint policy grants what it lists).
  policy = length(local.endpoint_gateway_policy_resources) > 0 ? jsonencode({
    Version = "2008-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:*"
      Resource  = local.endpoint_gateway_policy_resources
    }]
  }) : null

  tags = {
    Name    = "${var.name_prefix}-vpce-s3-gateway"
    purpose = var.purpose
  }
}

# --- Data sources ------------------------------------------------------------
# AL2 is EOL; the maintained pointer is the SSM parameter for the current AL2023
# kernel-default image. Replaces the old `most_recent` name filter (the B-1
# determinism shape — an apply on a different day could have silently picked a
# different AMI). Resolves to ami-0ed2b71371a7efa24 (verified 2026-08-15).
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# --- Outputs -----------------------------------------------------------------
output "vpc_id" {
  value = aws_vpc.main.id
}
output "vpc_cidr" {
  value = aws_vpc.main.cidr_block
}
output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}
output "private_subnet_cidrs" {
  value = aws_subnet.private[*].cidr_block
}
output "public_subnet_cidrs" {
  value = aws_subnet.public[*].cidr_block
}
output "default_security_group_id" {
  value = aws_security_group.default.id
}
output "private_route_table_id" {
  value = aws_route_table.private.id
}
output "endpoint_security_group_id" {
  value = aws_security_group.endpoint.id
}
output "harness_isolated_subnet_ids" {
  value = aws_subnet.harness_isolated[*].id
}
output "harness_isolated_subnet_cidrs" {
  value = aws_subnet.harness_isolated[*].cidr_block
}
output "harness_isolated_route_table_id" {
  value = aws_route_table.harness_isolated.id
}
output "nat_instance_id" {
  value = var.nat_enabled ? aws_instance.nat[0].id : ""
}
output "nat_gateway_id" {
  value       = var.nat_enabled && var.nat_gateway_enabled ? aws_nat_gateway.main[0].id : ""
  description = "NAT Gateway id, or empty string when egress is on the t3 instance."
}

# --- Stage 2.2: Cloud Map service discovery (the git mirror's DNS) -----------
# `git-mirror.eval.internal` resolves via a VPC-private namespace. The NAME is
# the stable indirection (never an address); the mirror service / task behind it
# can be replaced freely and dev/scale can resolve it differently. A private
# namespace is a Route53 private hosted zone for the VPC.
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name = "eval.internal"
  vpc  = aws_vpc.main.id
  tags = {
    Name    = "eval.internal"
    purpose = var.purpose
  }
}

resource "aws_service_discovery_service" "git_mirror" {
  name = "git-mirror"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 60
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  tags = {
    Name    = "git-mirror.eval.internal"
    purpose = var.purpose
  }
}

output "git_mirror_discovery_arn" {
  value = aws_service_discovery_service.git_mirror.arn
}
output "git_mirror_host" {
  value = "git-mirror.eval.internal"
}

# Stable-name indirection for internal ALBs (builder1-gateway-stable-dns):
# the Route53 private hosted zone id for eval.internal — an alias record to an
# internal ALB targets this zone, so the gateway is reachable at
# `gateway.eval.internal` no matter what numeric suffix AWS assigns the ALB.
output "internal_dns_zone_id" {
  value       = aws_service_discovery_private_dns_namespace.internal.hosted_zone
  description = "Route53 private hosted zone id for eval.internal — for alias records to internal ALBs."
}

# AND the Cloud Map namespace id (not the hosted-zone id): a Cloud Map DNS_PRIVATE
# namespace owns its zone, so a gateway aliased to the ALB must be registered as a
# Cloud Map SERVICE + INSTANCE (WEIGHTED routing) inside this namespace — you cannot
# write a standalone route53 record into a Cloud Map-owned zone (AccessDenied).
# Cloud Map services reference the namespace by .id, not .hosted_zone.
output "internal_dns_namespace_id" {
  value       = aws_service_discovery_private_dns_namespace.internal.id
  description = "Cloud Map namespace id for eval.internal — for services that alias internal ALBs (gateway)."
}
