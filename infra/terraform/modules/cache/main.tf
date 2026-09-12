/**
 * modules/cache — ElastiCache Serverless Valkey (ADR-0018 §3/§4)
 *
 * One Serverless cluster, two key namespaces: instance_progress:* and the
 * gateway's shared rate-limit state. EPHEMERAL — lives in envs/dev/ephemeral/,
 * created by apply, destroyed by destroy, never left up (1 GB storage floor
 * ~$61.32/mo on Valkey if leaked — an orphan-sweep event, not a small overrun).
 *
 * engine = "valkey" — decided 2026-08-12 (ADR-0018 §3): same resource type,
 * ~33% cheaper storage, ~27% cheaper ECPU than Redis, and it keeps the exact
 * resource type across dev/scale (a node class would be a shape change ADR-0018
 * exists to avoid). redis-py is wire-compatible (Redis 7.2), so LiteLLM's
 * client is expected to work — DoD 8 confirms this on the live run, not assumes.
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

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security group IDs."
}

variable "data_storage_max_gb" {
  type        = number
  default     = 1
  description = "Max serverless cache storage in GB. B-2: A-7a's cap as a variable (ADR-0018 'size is a variable'). 1 GB is the billed floor anyway (free). ~250x headroom over the 20k-split working set (~4 MB) — safe at any scale."
}
variable "ecpu_per_second_max" {
  type        = number
  default     = 5000
  description = "Max ECPU per second. B-2: the cap that actually bounds a runaway (no-TTL keys, retry storm on rate-limit counters) — ElastiCache Serverless THROTTLES when this binds, it does not queue, so it must stay above the 20k steady-state ~2,200 writes/s (ADR-0018). Raise before scale/, not after."
}

resource "aws_elasticache_serverless_cache" "main" {
  engine = "valkey" # ADR-0018 §3 — not Redis

  name = "${var.name_prefix}-cache"

  # A-7a (post-apply review): Serverless Storage + ECPU scale with NO default
  # ceiling — the only uncapped resource in the config. A correct run (~4 MB
  # working set) can't approach these, so this binds the defect case (keys
  # without TTLs, a retry storm on rate-limit counters). Values are variables
  # (B-2) so scale/ inherits dev's caps explicitly rather than re-deriving them.
  cache_usage_limits {
    data_storage {
      maximum = var.data_storage_max_gb
      unit    = "GB"
    }
    ecpu_per_second {
      maximum = var.ecpu_per_second_max
    }
  }

  # Serverless cache must live in a VPC with a subnet; uses two security-group
  # replicas. The data-model (keys/TTLs/counters) is Redis-protocol.
  subnet_ids         = var.subnet_ids
  security_group_ids = var.security_group_ids

  # lifecycled: ephemeral like the rest of envs/dev (destroyed by destroy)
  tags = {
    Name    = "${var.name_prefix}-cache"
    purpose = var.purpose
  }
}

output "arn" {
  value = aws_elasticache_serverless_cache.main.arn
}
output "id" {
  value = aws_elasticache_serverless_cache.main.id
}
# Endpoint for the gateway/worker redis clients. `endpoint` is a list of
# endpoint objects (one per applicable address) each with `address`/`port`.
output "endpoint" {
  value = aws_elasticache_serverless_cache.main.endpoint[0].address
}
output "endpoint_port" {
  value = aws_elasticache_serverless_cache.main.endpoint[0].port
}
