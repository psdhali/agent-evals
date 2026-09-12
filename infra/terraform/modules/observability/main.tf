/**
 * modules/observability — cost guardrails + log groups
 *
 * Built FIRST (review F-5): the whole point of this module is to exist before
 * any priced resource. Two independent cost controls:
 *
 *  1. purpose=dev AWS Budget, TimeUnit=QUARTERLY, threshold sized against the
 *     $100/quarter account budget (R-2). AWS Budgets is a global service, so it
 *     needs no region (it is created in the provider's default region but scoped
 *     to the whole account via the cost filter).
 *
 *  2. Us-east-1-only AWS/Billing alarm (F-7): the CloudWatch half. These metrics
 *     publish ONLY in us-east-1 (22 there, 0 in us-west-2). A us-west-2 alarm on
 *     them sits in INSUFFICIENT_DATA forever, looking configured. So this module
 *     takes a second aws provider aliased to us-east-1 and creates the alarm
 *     through it. The provider alias is passed in from the root, not defined
 *     here.
 *
 *  3. Nightly scale-to-zero for envs/dev (R-1b): an EventBridge cron rule → a
 *     Lambda that scales the named ECS services to zero. This is the same-day
 *     control that does NOT depend on billing data (Budgets lags 8-12h,
 *     CloudWatch ~6h). It is demonstrated working in DoD 10, not assumed.
 *
 * CloudWatch log groups get explicit retention so they don't accumulate and
 * bill silently across many apply/destroy cycles (F-9).
 */
terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 5.0"
      configuration_aliases = [aws.us_east_1]
    }
  }
}

variable "account_budget_limit_quarterly_usd" {
  type        = number
  default     = 100
  description = "Whole-account AWS Budget limit, QUARTERLY (verified: $100/quarter, evals-costs)."
}

variable "purpose_dev_threshold_usd" {
  type        = number
  default     = 25
  description = "purpose=dev budget threshold in USD (R-2). 25 QUARTERLY = 25% of the $100/quarter account budget."
}

variable "purpose_tag_value" {
  type        = string
  description = "The `purpose` tag value this budget alerts on (e.g. 'dev')."
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix."
}

variable "region" {
  type        = string
  description = "Default provider region (us-west-2)."
}

variable "us_east_1_region" {
  type        = string
  default     = "us-east-1"
  description = "Region where AWS/Billing metrics publish (F-7)."
}

variable "ecs_services_to_scale_to_zero" {
  type        = list(string)
  default     = []
  description = "Full ECS service ARNs the nightly Lambda scales to zero (dev ephemeral services). Empty in scale/ and until services exist (item 8)."
}

variable "alb_arns" {
  type        = list(string)
  default     = []
  description = "ALB ARNs, used where a per-run budget alarm or retention guard reads them."
}

# --- Guarded teardown inputs (review item 2) ---------------------------------
# The nightly Lambda refuses to scale unless ALL of these report idle: no
# pending/running Postgres runs, no queued OR in-flight SQS work, no RUNNING
# tasks. These variables feed both the Lambda env and its scoped IAM policy.

variable "guard_db_cluster_arn" {
  type        = string
  default     = ""
  description = "Aurora cluster ARN — the RDS Data API resourceArn for the Postgres idle check."
}
variable "guard_db_master_secret_arn" {
  type        = string
  default     = ""
  description = "Secrets Manager ARN for the Aurora master secret — used by the Data API call."
}
variable "guard_db_name" {
  type        = string
  default     = "app_control_plane"
  description = "Database name the Data API queries (the control-plane DB holding `runs`)."
}
variable "guard_queue_urls" {
  type        = list(string)
  default     = []
  description = "SQS queue URLs whose depth (visible + NotVisible) is checked for idle (harness-jobs, eval-jobs)."
}
variable "guard_queue_arns" {
  type        = list(string)
  default     = []
  description = "ARNs of the guarded queues — scoped for sqs:GetQueueAttributes."
}
variable "guard_ecs_cluster_name" {
  type        = string
  default     = ""
  description = "ECS cluster name — used for ecs:ListTasks in the idle check and as the scaling cluster."
}
variable "guard_asg_arns" {
  type        = list(string)
  default     = []
  description = "ASG ARNs (the eval host layer) the nightly Lambda sets to 0 — behind the same in-flight checks that gate service scaling. Closes A-6: without this the one resource the build host runs on is the one the guard can't turn off. Empty in scale/."
}
variable "notification_email" {
  type        = string
  description = "Email subscribed to the scale-to-zero SNS topic (notified on every run, louder on refusal)."
}

variable "alarm_email" {
  type        = string
  default     = ""
  description = "Email subscribed to the us-east-1 billing-alarm SNS topic (G-1). Defaults to notification_email when empty; override via tfvars if they must differ. Email-only by decision — SMS is settled closed (sandbox has no origination identity)."
}

# --- purpose=dev AWS Budget (global service) ---------------------------------
resource "aws_budgets_budget" "purpose_dev" {
  name         = "purpose-dev-${var.purpose_tag_value}"
  budget_type  = "COST"
  limit_amount = tostring(var.purpose_dev_threshold_usd)
  limit_unit   = "USD"
  time_unit    = "QUARTERLY" # R-2: matches the account budget's clock

  cost_filter {
    name = "TagKeyValue"
    # AWS Budgets tag cost-filters use "user:<key>$<value>" — a `$` separator,
    # NOT a colon. First apply rejected "user:purpose:dev" (InvalidParameter:
    # 'key$value'). `format()` makes the `$` literal and interpolates the value;
    # neither `$${...}` (renders the literal string `${...}`) nor "${...}" works.
    values = [format("user:purpose$%s", var.purpose_tag_value)]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.notification_email]
  }

  tags = {
    Name    = "${var.name_prefix}-purpose-dev-budget"
    purpose = var.purpose_tag_value
  }
}

# --- us-east-1 AWS/Billing alarm + its SNS delivery (F-7 / G-1) ---------------
# G-1 (sign-off review): the alarm used to fire and notify NOBODY (alarmActions
# empty). A CloudWatch alarm can only target an SNS topic in ITS OWN region, so
# this topic lives in us-east-1 through the aws.us_east_1 alias — referencing
# the us-west-2 scale-to-zero topic here would be rejected at apply. It is also
# where 5a-ii's automated action will attach.
resource "aws_sns_topic" "billing_alarm" {
  provider = aws.us_east_1
  name     = "${var.name_prefix}-billing-alarm"

  tags = {
    Name    = "${var.name_prefix}-billing-alarm"
    purpose = var.purpose_tag_value
  }
}

resource "aws_sns_topic_subscription" "billing_alarm_email" {
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.billing_alarm.arn
  protocol  = "email"
  endpoint  = coalesce(var.alarm_email, var.notification_email)
  # DoD-9 discipline applies to this topic too: confirm the subscription shows a
  # real ARN with status Confirmed (link clicked), not PendingConfirmation.
}

resource "aws_cloudwatch_metric_alarm" "billing" {
  provider = aws.us_east_1

  alarm_name          = "${var.name_prefix}-aws-billing-alarm"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = "21600" # 6 hours (lag matches the metric's reporting cadence)
  statistic           = "Maximum"
  # F-2 (DoD 8 review): EstimatedCharges is ONLY published against the
  # Currency dimension — an alarm with no dimensions subscribes to a metric
  # stream that does not exist and sits INSUFFICIENT_DATA forever (verified:
  # state reason was still the creation record). The account-control alarm
  # in the same region works precisely because it carries Currency=USD.
  dimensions = { Currency = "USD" }
  # G-1: alarm_actions + ok_actions deliver the fire AND the restore (the
  # not-firing half of DoD 8 turns from a manual describe into a delivered
  # artifact). Same-region us-east-1 topic required.
  alarm_actions = [aws_sns_topic.billing_alarm.arn]
  ok_actions    = [aws_sns_topic.billing_alarm.arn]

  threshold         = var.purpose_dev_threshold_usd
  alarm_description = "Account-level AWS/Billing EstimatedCharges alarm — created in the us-east-1 provider region because AWS/Billing metrics publish only there (F-7); publishes to the us-east-1 billing-alarm SNS topic (G-1)."
  tags = {
    Name    = "${var.name_prefix}-aws-billing-alarm"
    purpose = var.purpose_tag_value
  }
}

# --- CloudWatch log groups with explicit retention (F-9) ---------------------
variable "log_group_names" {
  type        = list(string)
  default     = []
  description = "Log group names to create with explicit retention so cycles don't accumulate silently."
}

variable "log_retention_days" {
  type        = number
  default     = 90
  description = "Log retention in days. PA-6: persistent dev log groups hold 90 days (CloudWatch = recent operational debugging; S3 artifacts is the permanent record)."
}

resource "aws_cloudwatch_log_group" "app" {
  count = length(var.log_group_names)

  name              = var.log_group_names[count.index]
  retention_in_days = var.log_retention_days

  tags = {
    Name    = var.log_group_names[count.index]
    purpose = var.purpose_tag_value
  }
}

# --- Nightly scale-to-zero: EventBridge cron → Lambda (R-1b) ------------------
data "aws_iam_policy_document" "scale_to_zero_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "scale_to_zero_policy" {
  # Scale target services to zero (only after all idle checks pass).
  # S-5: wrapped in a dynamic block so an EMPTY service list renders NO
  # statement. IAM rejects an identity-policy statement with no `Resource` key
  # (MalformedPolicyDocument at apply); `plan` cannot see it because the policy
  # references the SNS topic ARN (unknown until apply). Until 5a-ii supplies
  # service ARNs the guard has nothing to scale, so it needs no ECS write
  # permission — the better baseline posture anyway.
  dynamic "statement" {
    for_each = length(var.ecs_services_to_scale_to_zero) > 0 ? [1] : []
    content {
      actions = [
        "ecs:UpdateService",
        "ecs:DescribeServices",
      ]
      resources = var.ecs_services_to_scale_to_zero
    }
  }
  # Guard check 3: find RUNNING tasks on the target services.
  statement {
    actions   = ["ecs:ListTasks"]
    resources = ["*"] # list is a LIST verb scoped by serviceName; "*" is acceptable for ListTasks
  }
  # A-6 (guard can zero the ASG): scale the eval host layer to 0, behind the
  # same in-flight checks as the services. Dynamic so an EMPTY ASG list renders
  # NO statement (S-5 pattern — an identity-policy statement needs a Resource
  # key, and plan cannot see the ASG ARN, so an empty list would fail at apply).
  dynamic "statement" {
    for_each = length(var.guard_asg_arns) > 0 ? [1] : []
    content {
      actions = [
        "autoscaling:SetDesiredCapacity",
        "autoscaling:DescribeAutoScalingGroups",
      ]
      resources = var.guard_asg_arns
    }
  }
  # Guard check 2: queue depth (visible + NotVisible) on the job queues.
  statement {
    actions   = ["sqs:GetQueueAttributes"]
    resources = var.guard_queue_arns
  }
  # Guard check 1a (S-2): read the ServerlessDatabaseCapacity metric to detect
  # a PAUSED cluster (capacity 0 => idle, no query needed).
  statement {
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"] # CloudWatch metric statistics are not ARN-scoped; "*" required
  }
  # Guard check 1b (S-2 hybrid): RDS Data API query against Aurora (reads
  # `runs`) — only reached when the cluster is awake.
  statement {
    actions   = ["rds-data:ExecuteStatement"]
    resources = [var.guard_db_cluster_arn]
  }
  # The Data API call supplies the master secret as its secretArn.
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.guard_db_master_secret_arn]
  }
  # Notify on every run (louder on refusal).
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.scale_to_zero.arn]
  }
  # CloudWatch logs for the Lambda runtime.
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

# SNS topic for scale-to-zero decisions (guard notify requirement).
resource "aws_sns_topic" "scale_to_zero" {
  name = "${var.name_prefix}-scale-to-zero"

  tags = {
    Name    = "${var.name_prefix}-scale-to-zero"
    purpose = var.purpose_tag_value
  }
}

resource "aws_sns_topic_subscription" "scale_to_zero" {
  topic_arn = aws_sns_topic.scale_to_zero.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_iam_role" "scale_to_zero" {
  name               = "${var.name_prefix}-scale-to-zero"
  assume_role_policy = data.aws_iam_policy_document.scale_to_zero_assume.json

  tags = {
    Name    = "${var.name_prefix}-scale-to-zero"
    purpose = var.purpose_tag_value
  }
}

resource "aws_iam_role_policy" "scale_to_zero" {
  name   = "${var.name_prefix}-scale-to-zero-policy"
  role   = aws_iam_role.scale_to_zero.id
  policy = data.aws_iam_policy_document.scale_to_zero_policy.json
}

resource "aws_lambda_function" "scale_to_zero" {
  count = var.nightly_scale_to_zero_enabled ? 1 : 0

  function_name = "${var.name_prefix}-scale-to-zero"
  role          = aws_iam_role.scale_to_zero.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  timeout       = 60

  filename         = data.archive_file.scale_to_zero[0].output_path
  source_code_hash = data.archive_file.scale_to_zero[0].output_base64sha256

  environment {
    variables = {
      ECS_SERVICES   = jsonencode(var.ecs_services_to_scale_to_zero)
      DB_CLUSTER_ARN = var.guard_db_cluster_arn
      DB_SECRET_ARN  = var.guard_db_master_secret_arn
      DB_NAME        = var.guard_db_name
      QUEUE_URLS     = jsonencode(var.guard_queue_urls)
      ECS_CLUSTER    = var.guard_ecs_cluster_name
      ASG_ARNS       = jsonencode(var.guard_asg_arns)
      SNS_TOPIC_ARN  = aws_sns_topic.scale_to_zero.arn
    }
  }

  tags = {
    Name    = "${var.name_prefix}-scale-to-zero"
    purpose = var.purpose_tag_value
  }
}

data "archive_file" "scale_to_zero" {
  count = var.nightly_scale_to_zero_enabled ? 1 : 0

  type        = "zip"
  source_file = "${path.module}/scale_to_zero_lambda/index.py"
  output_path = "${path.module}/scale_to_zero_lambda.zip"
}

resource "aws_cloudwatch_event_rule" "nightly_scale_to_zero" {
  count = var.nightly_scale_to_zero_enabled ? 1 : 0

  name                = "${var.name_prefix}-nightly-scale-to-zero"
  description         = "Same-day cost control (R-1b): scales dev ephemeral ECS services to zero nightly, independent of billing data."
  schedule_expression = var.nightly_scale_to_zero_cron
  # Owner decision 2026-09-03: for a multi-hour scored run the guard is switched
  # OFF rather than trusted to refuse (its "running tasks" check is the safety
  # net, not the plan — a refusal that fails to fire costs a $600 run). The
  # Lambda and its IAM stay; only the schedule is disabled, so re-enabling is a
  # one-attribute in-place change with nothing recreated.
  state = var.nightly_scale_to_zero_paused ? "DISABLED" : "ENABLED"
  tags = {
    Name    = "${var.name_prefix}-nightly-scale-to-zero"
    purpose = var.purpose_tag_value
  }
}

resource "aws_cloudwatch_event_target" "nightly_scale_to_zero" {
  count = var.nightly_scale_to_zero_enabled ? 1 : 0

  rule      = aws_cloudwatch_event_rule.nightly_scale_to_zero[0].name
  target_id = "scale-to-zero"
  arn       = aws_lambda_function.scale_to_zero[0].arn
}

resource "aws_lambda_permission" "nightly_eventbridge" {
  count = var.nightly_scale_to_zero_enabled ? 1 : 0

  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scale_to_zero[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.nightly_scale_to_zero[0].arn
}

variable "nightly_scale_to_zero_cron" {
  type = string
  # 10:00 UTC = 03:00 PDT / 02:00 PST — the middle of the owner's night in
  # every part of the year.
  #
  # Was cron(0 2 * * ? *) = 02:00 UTC = 19:00 PDT, i.e. SEVEN PM local, in the
  # middle of the working evening. The guard fired while the fleet was
  # legitimately busy and correctly refused, six nights running (2026-08-16..22,
  # zero Lambda errors — every one a REFUSAL, not a crash). A cost control that
  # can only ever fire while you are still working is a cost control that never
  # fires.
  description = "UTC cron for the nightly scale-to-zero rule. EventBridge cron is ALWAYS UTC — there is no timezone field on aws_cloudwatch_event_rule."
  default     = "cron(0 10 * * ? *)"
}

variable "nightly_scale_to_zero_enabled" {
  type        = bool
  default     = false
  description = "Enable the nightly scale-to-zero rule + Lambda (dev only — scale/ must not auto-scale to zero)."
}

variable "nightly_scale_to_zero_paused" {
  type        = bool
  default     = false
  description = "When true the nightly rule exists but is DISABLED (no firing) — set for the duration of a scored run that spans the cron; flip back to false at teardown. No-op when nightly_scale_to_zero_enabled is false."
}

# --- Outputs -----------------------------------------------------------------
output "scale_to_zero_lambda_arn" {
  value = var.nightly_scale_to_zero_enabled ? aws_lambda_function.scale_to_zero[0].arn : ""
}
output "scale_to_zero_lambda_name" {
  value = var.nightly_scale_to_zero_enabled ? aws_lambda_function.scale_to_zero[0].function_name : ""
}
output "nightly_rule_arn" {
  value = var.nightly_scale_to_zero_enabled ? aws_cloudwatch_event_rule.nightly_scale_to_zero[0].arn : ""
}
output "billing_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.billing.arn
}
output "purpose_dev_budget_id" {
  value = aws_budgets_budget.purpose_dev.id
}