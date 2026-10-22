# Monthly cost cap for the stack (AWS Budgets). When actual spend on resources
# tagged Project=<name> passes `budget_limit_usd`, two budget actions run
# automatically:
#   * stop the instance (AWS-managed "stop EC2 instances" action), and
#   * attach a deny on ec2:StartInstances to the starter Lambda's role, so a
#     sign-in can't wake it again (the Lambda shows a "paused" page instead).
# Nothing restarts until the owner reverses the actions: deploy/budget-resume.sh.
#
# AWS updates budget spend a few times a day (billing data lags up to ~24 h), so
# the stop can come after the limit has been passed. Only costs incurred after
# the Project tag is activated for cost allocation are counted.
#
# Needs the main stack's outputs (starter_role_name, instance_id), so leave
# those empty on the very first bootstrap apply; the budget is created once set.

variable "budget_limit_usd" {
  type    = number
  default = 50
}

variable "budget_email" {
  description = "Who gets budget alerts and action notices."
  type        = string
  default     = ""
}

variable "starter_role_name" {
  description = "From `terraform output starter_role_name` in deploy/terraform."
  type        = string
  default     = ""
}

variable "instance_id" {
  description = "From `terraform output instance_id` in deploy/terraform."
  type        = string
  default     = ""
}

provider "aws" {
  alias  = "us_east_1" # Cost Explorer (cost allocation tags) lives in us-east-1
  region = "us-east-1"
  default_tags {
    tags = { Project = var.name }
  }
}

locals {
  budget_on   = var.budget_email != "" && var.starter_role_name != "" && var.instance_id != ""
  budget_name = "${var.name}-monthly"
}

resource "aws_ce_cost_allocation_tag" "project" {
  provider = aws.us_east_1
  tag_key  = "Project"
  status   = "Active"
}

resource "aws_iam_policy" "budget_block_start" {
  name        = "${var.name}-budget-block-start"
  description = "Attached to the starter role by the budget action: no instance starts until reversed"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "BudgetLimitReached"
      Effect   = "Deny"
      Action   = "ec2:StartInstances"
      Resource = "*"
    }]
  })
}

data "aws_iam_policy_document" "budgets_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${local.account}:budget/${local.budget_name}*"]
    }
  }
}

resource "aws_iam_role" "budget_actions" {
  count              = local.budget_on ? 1 : 0
  name               = "${var.name}-budget-actions"
  description        = "Lets AWS Budgets stop the ${var.name} instance and block restarts"
  assume_role_policy = data.aws_iam_policy_document.budgets_trust.json
}

data "aws_iam_policy_document" "budget_actions" {
  statement {
    sid       = "BlockOrUnblockTheStarter"
    actions   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    resources = ["${local.iam}:role/${var.starter_role_name}"]
    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = [aws_iam_policy.budget_block_start.arn]
    }
  }
}

resource "aws_iam_role_policy" "budget_actions" {
  count  = local.budget_on ? 1 : 0
  role   = aws_iam_role.budget_actions[0].id
  policy = data.aws_iam_policy_document.budget_actions.json
}

resource "aws_iam_role_policy_attachment" "budget_actions_ssm" {
  count      = local.budget_on ? 1 : 0
  role       = aws_iam_role.budget_actions[0].name
  policy_arn = "arn:aws:iam::aws:policy/AWSBudgetsActions_RolePolicyForResourceAdministrationWithSSM"
}

resource "aws_budgets_budget" "stack" {
  count        = local.budget_on ? 1 : 0
  name         = local.budget_name
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:Project$%s", var.name)] # TagKeyValue form: user:<key>$<value>
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

resource "aws_budgets_budget_action" "block_start" {
  count              = local.budget_on ? 1 : 0
  budget_name        = aws_budgets_budget.stack[0].name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_actions[0].arn

  action_threshold {
    action_threshold_type  = "ABSOLUTE_VALUE"
    action_threshold_value = var.budget_limit_usd
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.budget_block_start.arn
      roles      = [var.starter_role_name]
    }
  }

  subscriber {
    address           = var.budget_email
    subscription_type = "EMAIL"
  }

  depends_on = [aws_iam_role_policy.budget_actions]
}

resource "aws_budgets_budget_action" "stop_instance" {
  count              = local.budget_on ? 1 : 0
  budget_name        = aws_budgets_budget.stack[0].name
  action_type        = "RUN_SSM_DOCUMENTS"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_actions[0].arn

  action_threshold {
    action_threshold_type  = "ABSOLUTE_VALUE"
    action_threshold_value = var.budget_limit_usd
  }

  definition {
    ssm_action_definition {
      action_sub_type = "STOP_EC2_INSTANCES"
      instance_ids    = [var.instance_id]
      region          = var.aws_region
    }
  }

  subscriber {
    address           = var.budget_email
    subscription_type = "EMAIL"
  }

  depends_on = [aws_iam_role_policy_attachment.budget_actions_ssm]
}

output "budget_name" {
  value = local.budget_on ? aws_budgets_budget.stack[0].name : "(not configured: set budget_email, starter_role_name, instance_id)"
}
