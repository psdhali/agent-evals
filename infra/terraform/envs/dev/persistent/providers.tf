provider "aws" {
  region  = var.region
  profile = local.aws_profile
  default_tags {
    tags = {
      App       = "swe-bench-eval-framework"
      Env       = var.purpose
      purpose   = var.purpose
      ManagedBy = "terraform"
    }
  }
}

# us-east-1 alias (F-7): AWS/Billing metrics publish ONLY in us-east-1. The
# observability module's billing alarm is created through this alias; an alarm
# against those metrics in any other region sits in INSUFFICIENT_DATA forever,
# looking configured.
provider "aws" {
  alias   = "us_east_1"
  region  = "us-east-1"
  profile = local.aws_profile
  default_tags {
    tags = {
      App       = "swe-bench-eval-framework"
      Env       = var.purpose
      purpose   = var.purpose
      ManagedBy = "terraform"
    }
  }
}
