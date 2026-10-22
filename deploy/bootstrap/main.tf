# One-time bootstrap, applied with an admin identity: creates the scoped
# `srt-vast-deploy` role that the main configuration (../terraform) runs as.
#
#   cd deploy/bootstrap && AWS_PROFILE=srt-vast-base terraform init && terraform apply
#
# Escalation guard: the deploy role has to create an IAM role (instance
# profile) and an IAM user (Worker key) with policies, and pass the role to
# EC2. Anything it creates must carry the `srt-vast-boundary` permissions
# boundary, which caps those identities at what this stack needs, so the
# deploy role cannot mint itself (or anyone) broader access.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

variable "name" {
  type    = string
  default = "srt-vast"
}

variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "deployer_arns" {
  description = "IAM principals allowed to assume the deploy role."
  type        = list(string)
}

variable "allowed_instance_types" {
  type    = list(string)
  default = ["c7g.large", "c7g.xlarge", "c8g.large", "c8g.xlarge", "m7g.large", "t4g.large", "t4g.xlarge"]
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { Project = var.name }
  }
}

data "aws_caller_identity" "me" {}

locals {
  account = data.aws_caller_identity.me.account_id
  region  = var.aws_region
  tag     = { "aws:ResourceTag/Project" = var.name }
  ec2     = "arn:aws:ec2:${local.region}:${local.account}"
  iam     = "arn:aws:iam::${local.account}"
  ssm     = "arn:aws:ssm:${local.region}:${local.account}"
  # Known at plan time (the policy name is fixed), so the plan shows the full deploy policy.
  boundary_arn = "${local.iam}:policy/${var.name}-boundary"
}

# ---------------------------------------------------------------- boundary
# Ceiling for every identity the deploy role creates (host role, Worker user).

data "aws_iam_policy_document" "boundary" {
  statement {
    sid = "SsmAgent" # the actions of AmazonSSMManagedInstanceCore, minus parameter reads
    actions = [
      "ssm:DescribeAssociation", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:GetDocument",
      "ssm:DescribeDocument", "ssm:GetManifest", "ssm:ListAssociations", "ssm:ListInstanceAssociations",
      "ssm:PutInventory", "ssm:PutComplianceItems", "ssm:PutConfigurePackageResult",
      "ssm:UpdateAssociationStatus", "ssm:UpdateInstanceAssociationStatus", "ssm:UpdateInstanceInformation",
      "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
      "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage",
      "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "OwnParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["${local.ssm}:parameter/${var.name}", "${local.ssm}:parameter/${var.name}/*"]
  }
  statement {
    sid       = "DecryptOwnParametersViaSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "OwnBundle"
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.name}-bundle-*/*"]
  }
  statement {
    sid       = "DescribeInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }
  statement {
    sid       = "StartOwnInstance"
    actions   = ["ec2:StartInstances"]
    resources = ["${local.ec2}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.name]
    }
  }
}

resource "aws_iam_policy" "boundary" {
  name        = "${var.name}-boundary"
  description = "Permissions boundary for identities created by ${var.name}-deploy"
  policy      = data.aws_iam_policy_document.boundary.json
}

# ---------------------------------------------------------------- deploy role

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = var.deployer_arns
    }
  }
}

data "aws_iam_policy_document" "deploy" {
  # ---- EC2 -----------------------------------------------------------------
  statement {
    sid       = "Ec2Read"
    actions   = ["ec2:Describe*", "ec2:GetConsoleOutput"]
    resources = ["*"]
  }
  statement {
    sid       = "RunTaggedGravitonInstance"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.name]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = var.allowed_instance_types
    }
  }
  statement {
    sid     = "RunInstancesSupportingResources"
    actions = ["ec2:RunInstances"]
    resources = [
      "arn:aws:ec2:${local.region}::image/*",
      "${local.ec2}:subnet/*", "${local.ec2}:network-interface/*",
      "${local.ec2}:security-group/*", "${local.ec2}:volume/*",
    ]
  }
  statement {
    sid       = "CreateTaggedSecurityGroup"
    actions   = ["ec2:CreateSecurityGroup"]
    resources = ["${local.ec2}:security-group/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.name]
    }
  }
  statement {
    sid       = "CreateSecurityGroupInVpc"
    actions   = ["ec2:CreateSecurityGroup"]
    resources = ["${local.ec2}:vpc/*"]
  }
  statement {
    sid       = "TagOnCreate"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2}:*/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateSecurityGroup"]
    }
  }
  statement {
    sid = "ManageOwnTaggedResources"
    actions = [
      "ec2:TerminateInstances", "ec2:StopInstances", "ec2:StartInstances", "ec2:RebootInstances",
      "ec2:ModifyInstanceAttribute", "ec2:ModifyInstanceMetadataOptions",
      "ec2:AssociateIamInstanceProfile", "ec2:ReplaceIamInstanceProfileAssociation",
      "ec2:DisassociateIamInstanceProfile",
      "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupEgress", "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:ModifySecurityGroupRules",
      "ec2:CreateTags", "ec2:DeleteTags",
    ]
    resources = ["${local.ec2}:instance/*", "${local.ec2}:security-group/*", "${local.ec2}:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.name]
    }
  }
  statement {
    sid = "SecurityGroupRules" # rule sub-resources carry no tags; the group itself is checked above
    actions = [
      "ec2:AuthorizeSecurityGroupEgress", "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:ModifySecurityGroupRules",
    ]
    resources = ["${local.ec2}:security-group-rule/*"]
  }

  # ---- IAM: only srt-vast identities, always under the boundary ------------
  statement {
    sid = "CreateBoundedIdentities"
    actions = [
      "iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:CreateUser", "iam:PutUserPolicy", "iam:DeleteUserPolicy",
    ]
    resources = ["${local.iam}:role/${var.name}-host-*", "${local.iam}:user/${var.name}-waker"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }
  statement {
    sid       = "OnlyTheSsmManagedPolicy"
    effect    = "Deny"
    actions   = ["iam:AttachRolePolicy", "iam:AttachUserPolicy"]
    resources = ["*"]
    condition {
      test     = "ArnNotEquals"
      variable = "iam:PolicyARN"
      values   = ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    }
  }
  statement {
    sid = "ManageOwnIdentities"
    actions = [
      "iam:GetRole", "iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags", "iam:UpdateRole",
      "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:GetUser", "iam:DeleteUser", "iam:TagUser", "iam:UntagUser", "iam:ListUserTags",
      "iam:GetUserPolicy", "iam:ListUserPolicies", "iam:ListAttachedUserPolicies", "iam:ListGroupsForUser",
      "iam:CreateAccessKey", "iam:DeleteAccessKey", "iam:ListAccessKeys", "iam:UpdateAccessKey",
      "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile", "iam:TagInstanceProfile",
      "iam:UntagInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
    ]
    resources = [
      "${local.iam}:role/${var.name}-host-*", "${local.iam}:user/${var.name}-waker",
      "${local.iam}:instance-profile/${var.name}-*",
    ]
  }
  statement {
    sid       = "PassHostRoleToEc2"
    actions   = ["iam:PassRole"]
    resources = ["${local.iam}:role/${var.name}-host-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }
  statement {
    sid    = "NoBoundaryEscapes"
    effect = "Deny"
    actions = [
      "iam:DeleteRolePermissionsBoundary", "iam:DeleteUserPermissionsBoundary",
      "iam:PutRolePermissionsBoundary", "iam:PutUserPermissionsBoundary",
      "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion",
    ]
    resources = ["*"]
  }

  # ---- S3, SSM ---------------------------------------------------------------
  statement {
    sid       = "OwnBundleBuckets"
    actions   = ["s3:*"]
    resources = ["arn:aws:s3:::${var.name}-bundle-*"] # matches the buckets and their objects
  }
  statement {
    sid = "OwnParameters"
    actions = [
      "ssm:PutParameter", "ssm:DeleteParameter", "ssm:GetParameter", "ssm:GetParameters",
      "ssm:GetParametersByPath", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource",
    ]
    resources = ["${local.ssm}:parameter/${var.name}", "${local.ssm}:parameter/${var.name}/*"]
  }
  statement {
    sid       = "PublicAmiParameters"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${local.region}::parameter/aws/service/canonical/*"]
  }
  statement {
    sid       = "DescribeParameters"
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }
  statement {
    sid       = "AdminOwnInstance" # shell / run-command on the stack's instance only
    actions   = ["ssm:StartSession", "ssm:SendCommand"]
    resources = ["${local.ec2}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.name]
    }
  }
  statement {
    sid     = "AdminDocuments"
    actions = ["ssm:StartSession", "ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:${local.region}::document/AWS-RunShellScript",
      "${local.ssm}:document/SSM-SessionManagerRunShell",
    ]
  }
  statement {
    sid       = "AdminSessionsAndResults"
    actions   = ["ssm:TerminateSession", "ssm:ResumeSession", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }

  # ---- region pin + self-protection -------------------------------------------
  statement {
    sid         = "OnlyThisRegion"
    effect      = "Deny"
    not_actions = ["iam:*", "sts:*", "s3:*"]
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
  statement {
    sid       = "HandsOffTheDeployRoleAndBoundary"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = ["${local.iam}:role/${var.name}-deploy", local.boundary_arn]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name}-deploy"
  description          = "Deploys the ${var.name} stack (deploy/terraform); scoped, boundary-enforcing"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "deploy" {
  name       = "${var.name}-deploy"
  role       = aws_iam_role.deploy.id
  policy     = data.aws_iam_policy_document.deploy.json
  depends_on = [aws_iam_policy.boundary]
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}

output "permissions_boundary_arn" {
  value = aws_iam_policy.boundary.arn
}
