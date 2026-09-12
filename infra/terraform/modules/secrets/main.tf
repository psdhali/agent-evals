/**
 * modules/secrets — Secrets Manager secrets (arch §6.4 / N1)
 *
 * Created in envs/dev/persistent/ (durable, alongside Aurora) so a weekly
 * `ephemeral/` destroy never wipes credentials and triggers a second-cycle
 * name collision. `recovery_window_in_days = 0` (F-9): deleted secrets must not
 * sit in a 7-30 day recovery window, or the next apply fails on the name.
 *
 * Values are passed as a map keyed by secret NAME (non-sensitive `name` list is
 * used for for_each; the values map itself is sensitive). They never print in a
 * plan render. Task definitions reference these ARNs via ECS `secrets`
 * (resolved at task start) — no application code change required (the app
 * already reads os.environ).
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

# Names are non-sensitive so they can be for_each keys; values are sensitive.
variable "secret_values" {
  type        = map(string)
  description = "Map of secret name → value. Sensitive; never printed."
  sensitive   = true
}

variable "secret_names" {
  type        = list(string)
  description = "Non-sensitive list of secret names (for_each keys)."
}

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(var.secret_names)

  name                    = "${var.name_prefix}-${each.key}"
  recovery_window_in_days = 0 # F-9: no 7-30 day recovery window to collide on next apply
  tags = {
    Name    = "${var.name_prefix}-${each.key}"
    purpose = var.purpose
  }
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each = toset(var.secret_names)

  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = var.secret_values[each.key]
}

output "secret_arns" {
  value = { for n in var.secret_names : n => aws_secretsmanager_secret.this[n].arn }
}
output "secret_names" {
  value = { for n in var.secret_names : n => aws_secretsmanager_secret.this[n].name }
}
