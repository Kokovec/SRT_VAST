# One-time bootstrap, applied with an admin identity: creates the scoped
# `srt-vast-deploy` role that the main configuration (../terraform) runs as.
#
#   cd deploy/bootstrap && AWS_PROFILE=srt-vast-base terraform init && terraform apply
#
# Escalation guard: the deploy role has to create IAM roles (EC2 instance
# profile, Lambda execution role) with policies and pass them to EC2/Lambda.
# Anything it creates must carry the `srt-vast-boundary` permissions boundary,
# which caps those identities at what this stack needs, so the deploy role
# cannot mint itself (or anyone) broader access. DNS changes are limited to
# names at or under `hostname`, so the rest of the zone (your website) is out
# of reach.

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

variable "domain" {
  description = "Route 53 zone the stack adds records to."
  type        = string
}

variable "hostname" {
  description = "Viewer host; the deploy role may only change records at or under this name."
  type        = string
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

data "aws_route53_zone" "main" {
  name         = var.domain
  private_zone = false
}

locals {
  account = data.aws_caller_identity.me.account_id
  region  = var.aws_region
  ec2     = "arn:aws:ec2:${local.region}:${local.account}"
  iam     = "arn:aws:iam::${local.account}"
  ssm     = "arn:aws:ssm:${local.region}:${local.account}"
  lambda  = "arn:aws:lambda:${local.region}:${local.account}"
  logs    = "arn:aws:logs:${local.region}:${local.account}:log-group:/aws/lambda/${var.name}-*"
  origin  = "origin.${var.hostname}"
  # Known at plan time (the policy name is fixed), so the plan shows the full deploy policy.
  boundary_arn = "${local.iam}:policy/${var.name}-boundary"
}

# ---------------------------------------------------------------- boundary
# Ceiling for every identity the deploy role creates (host role, starter Lambda role).

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
  statement {
    sid       = "OriginRecordAndCertChallenge"
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = [data.aws_route53_zone.main.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values   = [local.origin, "_acme-challenge.${local.origin}"]
    }
    condition {
      test     = "Null"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values   = ["false"]
    }
  }
  statement {
    sid       = "Route53Lookups"
    actions   = ["route53:ListHostedZones", "route53:GetChange"]
    resources = ["*"]
  }
  statement {
    sid       = "OwnLambdaLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.logs}:*"]
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
    actions   = ["ec2:Describe*", "ec2:GetConsoleOutput", "ec2:GetManagedPrefixListEntries"]
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
    sid = "SecurityGroupRulesAndPrefixLists" # sub-resources carry no tags; the group itself is checked above
    actions = [
      "ec2:AuthorizeSecurityGroupEgress", "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:ModifySecurityGroupRules",
    ]
    resources = ["${local.ec2}:security-group-rule/*", "arn:aws:ec2:${local.region}:aws:prefix-list/*"]
  }

  # ---- IAM: only srt-vast service roles, always under the boundary ---------
  statement {
    sid = "CreateBoundedRoles"
    actions = [
      "iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
    ]
    resources = ["${local.iam}:role/${var.name}-svc-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }
  statement {
    sid       = "OnlyTheSsmManagedPolicy"
    effect    = "Deny"
    actions   = ["iam:AttachRolePolicy"]
    resources = ["*"]
    condition {
      test     = "ArnNotEquals"
      variable = "iam:PolicyARN"
      values   = ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    }
  }
  statement {
    sid = "ManageOwnRoles"
    actions = [
      "iam:GetRole", "iam:DeleteRole", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags", "iam:UpdateRole",
      "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile", "iam:TagInstanceProfile",
      "iam:UntagInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
    ]
    resources = ["${local.iam}:role/${var.name}-svc-*", "${local.iam}:instance-profile/${var.name}-*"]
  }
  statement {
    sid       = "PassServiceRoles"
    actions   = ["iam:PassRole"]
    resources = ["${local.iam}:role/${var.name}-svc-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com", "lambda.amazonaws.com"]
    }
  }
  statement {
    sid    = "NoBoundaryEscapes"
    effect = "Deny"
    actions = [
      "iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary",
      "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion",
      "iam:CreateUser", "iam:CreateAccessKey",
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

  # ---- Route 53: only names at or under the viewer hostname -----------------
  statement {
    sid       = "OwnRecordsOnly"
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = [data.aws_route53_zone.main.arn]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values   = [var.hostname, "*.${var.hostname}"]
    }
    condition {
      test     = "Null"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values   = ["false"]
    }
  }
  statement {
    sid       = "ReadZone"
    actions   = ["route53:GetHostedZone", "route53:ListResourceRecordSets", "route53:ListTagsForResource"]
    resources = [data.aws_route53_zone.main.arn]
  }
  statement {
    sid       = "Route53Lookups"
    actions   = ["route53:ListHostedZones", "route53:ListHostedZonesByName", "route53:GetChange"]
    resources = ["*"]
  }

  # ---- ACM (us-east-1, for CloudFront) --------------------------------------
  statement {
    sid       = "RequestTaggedCertificate"
    actions   = ["acm:RequestCertificate", "acm:AddTagsToCertificate"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.name]
    }
  }
  statement {
    sid       = "ReadCertificates" # CloudFront checks the viewer certificate without tag context
    actions   = ["acm:DescribeCertificate", "acm:GetCertificate", "acm:ListTagsForCertificate"]
    resources = ["arn:aws:acm:us-east-1:${local.account}:certificate/*"]
  }
  statement {
    sid       = "ListCertificates"
    actions   = ["acm:ListCertificates"]
    resources = ["*"]
  }
  statement {
    sid       = "DeleteOwnCertificates"
    actions   = ["acm:DeleteCertificate"]
    resources = ["arn:aws:acm:us-east-1:${local.account}:certificate/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.name]
    }
  }

  # ---- CloudFront ------------------------------------------------------------
  statement {
    sid       = "CreateTaggedDistribution" # CreateDistribution has no resource-level permissions
    actions   = ["cloudfront:CreateDistribution", "cloudfront:TagResource"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = [var.name]
    }
  }
  statement {
    sid = "ManageOwnDistribution"
    actions = [
      "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution",
      "cloudfront:DeleteDistribution", "cloudfront:ListTagsForResource", "cloudfront:UntagResource",
      "cloudfront:CreateInvalidation",
    ]
    resources = ["arn:aws:cloudfront::${local.account}:distribution/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.name]
    }
  }
  statement {
    sid = "OriginAccessControlsAndManagedPolicies" # OACs are not taggable; managed policies are read-only
    actions = [
      "cloudfront:CreateOriginAccessControl", "cloudfront:GetOriginAccessControl",
      "cloudfront:UpdateOriginAccessControl", "cloudfront:DeleteOriginAccessControl",
      "cloudfront:ListOriginAccessControls", "cloudfront:ListCachePolicies", "cloudfront:GetCachePolicy",
      "cloudfront:ListOriginRequestPolicies", "cloudfront:GetOriginRequestPolicy",
    ]
    resources = ["*"]
  }

  # ---- Lambda + its logs -------------------------------------------------------
  statement {
    sid = "OwnFunctions"
    actions = [
      "lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
      "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:ListVersionsByFunction",
      "lambda:GetFunctionCodeSigningConfig", "lambda:GetRuntimeManagementConfig",
      "lambda:CreateFunctionUrlConfig", "lambda:GetFunctionUrlConfig", "lambda:UpdateFunctionUrlConfig",
      "lambda:DeleteFunctionUrlConfig", "lambda:AddPermission", "lambda:RemovePermission", "lambda:GetPolicy",
      "lambda:TagResource", "lambda:UntagResource", "lambda:ListTags",
    ]
    resources = ["${local.lambda}:function:${var.name}-*"]
  }
  statement {
    sid = "OwnLogGroups"
    actions = [
      "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
      "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource", "logs:TagLogGroup",
      "logs:ListTagsLogGroup", "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams",
    ]
    resources = [local.logs] # also matches its log streams
  }
  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  # ---- Cognito -----------------------------------------------------------------
  statement {
    sid       = "ManageThisStacksUserPool" # Cognito has no tag conditions: scope to the pool's ARN
    actions   = ["cognito-idp:*"]
    resources = [aws_cognito_user_pool.users.arn]
  }
  statement {
    sid       = "CognitoLookups"
    actions   = ["cognito-idp:DescribeUserPoolDomain", "cognito-idp:ListUserPools"]
    resources = ["*"]
  }

  # ---- region pin + self-protection -------------------------------------------
  statement {
    sid         = "OnlyThisRegion"
    effect      = "Deny"
    not_actions = ["iam:*", "sts:*", "s3:*", "cloudfront:*", "route53:*", "acm:*"]
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = [local.region]
    }
  }
  statement {
    sid       = "CertificatesOnlyForCloudFront"
    effect    = "Deny"
    actions   = ["acm:*"]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = ["us-east-1"]
    }
  }
  statement {
    sid       = "HandsOffTheDeployRoleAndBoundary"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = ["${local.iam}:role/${var.name}-deploy", local.boundary_arn]
  }
}

# ---------------------------------------------------------------- network
# A small dedicated VPC (free: no NAT gateway). One public subnet; the
# instance's security group (main config) only lets CloudFront in. Created
# here so the deploy role needs no VPC-building rights.

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/24"
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "stack" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "stack" {
  vpc_id = aws_vpc.stack.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.stack.id
  cidr_block              = var.vpc_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.stack.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.stack.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------- user pool
# Created here (admin) rather than by the deploy role: Cognito does not support
# tag-based conditions, so the only way to confine the deploy role to this
# stack's pool is to know the pool's ARN up front. The deploy role manages the
# app client, hosted-login domain and users inside it.

resource "aws_cognito_user_pool" "users" {
  name                     = var.name
  user_pool_tier           = "LITE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"
  deletion_protection      = "ACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = true # nobody can sign themselves up

    invite_message_template {
      email_subject = "Your SRT VAST viewer sign-in"
      email_message = "You have access to the SRT VAST stream viewer at https://${var.hostname}/ . Sign in with {username} and this temporary password: {####} (you will choose your own on first sign-in)."
      sms_message   = "SRT VAST sign-in: {username} / {####}"
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
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

output "vpc_id" {
  value = aws_vpc.stack.id
}

output "subnet_id" {
  value = aws_subnet.public.id
}

output "user_pool_id" {
  value = aws_cognito_user_pool.users.id
}

output "permissions_boundary_arn" {
  value = aws_iam_policy.boundary.arn
}
