/**
 * envs/scale — main composition (PLAN ONLY; apply gated by a read-only IAM role).
 *
 * Same modules as dev/envs/dev/{persistent,ephemeral}, capacity turned up.
 * The provider profile defaults to the read-only eval-framework-ro role
 * (OPS-6; see providers.tf), so plan renders and apply fails AccessDenied.
 * Composition here mirrors dev/ to prove the modules parameterize cleanly.
 */

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(sort(data.aws_availability_zones.available.names), 0, 3)
}

# --- Network: capacity up, endpoints in all 3 AZs, NAT Gateway (per §6.6) -----
# NOTE: this module's `network` currently supports a NAT instance; scale would
# swap to a NAT Gateway. The module exposes `nat_enabled`; a full scale NAT
# Gateway would be a separate variable. For the plan-only render, module calls
# below prove parameterization. (See the network module comments.)
module "network" {
  source = "../../modules/network"

  region                = var.region
  azs                   = local.azs
  purpose               = "scale"
  name_prefix           = "eval-scale"
  nat_enabled           = true
  endpoint_azs          = local.azs # scale → all three AZs (C-2)
  vpc_endpoint_services = ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "sqs", "monitoring"]
}

output "vpc_cidr" {
  value = module.network.vpc_cidr
}
output "private_subnet_cidrs" {
  value = module.network.private_subnet_cidrs
}
