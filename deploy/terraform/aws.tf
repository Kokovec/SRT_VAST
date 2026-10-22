# EC2 host for the docker compose stack. Inbound: HTTPS from CloudFront's
# origin-facing ranges only. Admin access: SSM Session Manager.

data "aws_ssm_parameter" "ubuntu_arm64" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_caller_identity" "me" {}

data "aws_route53_zone" "main" {
  name         = var.domain
  private_zone = false
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

locals {
  param_prefix = "/${var.name}"
  bundle_key   = "bundle/${var.name}.zip"
  repo_root    = abspath("${path.module}/../..")
  origin_host  = "origin.${var.hostname}" # A record the host points at itself on every boot
}

# ---------------------------------------------------------------- code bundle

data "archive_file" "bundle" {
  type        = "zip"
  source_dir  = local.repo_root
  output_path = "${path.module}/.build/${var.name}.zip"
  excludes = [
    ".git", ".git/**", "out", "out/**", ".claude", ".claude/**",
    "deploy/terraform/.terraform", "deploy/terraform/.terraform/**", "deploy/terraform/.build",
    "deploy/terraform/.build/**", "deploy/terraform/terraform.tfstate", "deploy/terraform/terraform.tfstate.backup",
    "deploy/terraform/terraform.tfvars", "deploy/bootstrap/.terraform", "deploy/bootstrap/.terraform/**",
    "deploy/bootstrap/terraform.tfstate", "deploy/bootstrap/terraform.tfstate.backup",
    "deploy/bootstrap/terraform.tfvars", "**/__pycache__/**",
  ]
}

resource "aws_s3_bucket" "bundle" {
  bucket_prefix = "${var.name}-bundle-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "bundle" {
  bucket                  = aws_s3_bucket.bundle.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "bundle" {
  bucket = aws_s3_bucket.bundle.id
  key    = local.bundle_key
  source = data.archive_file.bundle.output_path
  etag   = data.archive_file.bundle.output_md5
}

# ---------------------------------------------------------------- settings (-> /opt/srt-vast/.env)

resource "aws_ssm_parameter" "settings" {
  for_each = {
    AUTH_ISSUER     = local.issuer
    AUTH_AUDIENCE   = aws_cognito_user_pool_client.viewer.id
    ORIGIN_HOSTNAME = local.origin_host
  }
  name  = "${local.param_prefix}/${each.key}"
  type  = "String"
  value = each.value
}

# ---------------------------------------------------------------- host role

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "host" {
  name_prefix          = "${var.name}-svc-host-"
  assume_role_policy   = data.aws_iam_policy_document.ec2_assume.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "host" {
  statement {
    sid       = "CodeBundle"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.bundle.arn}/${local.bundle_key}"]
  }
  statement {
    sid       = "Settings"
    actions   = ["ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.me.account_id}:parameter${local.param_prefix}"]
  }
  statement {
    sid       = "OriginRecordAndCertChallenge" # its own A record + the Let's Encrypt TXT record, nothing else
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = [data.aws_route53_zone.main.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "route53:ChangeResourceRecordSetsNormalizedRecordNames"
      values   = [local.origin_host, "_acme-challenge.${local.origin_host}"]
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
}

resource "aws_iam_role_policy" "host" {
  role   = aws_iam_role.host.id
  policy = data.aws_iam_policy_document.host.json
}

resource "aws_iam_instance_profile" "host" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.host.name
}

# ---------------------------------------------------------------- instance

resource "aws_security_group" "host" {
  name_prefix = "${var.name}-"
  description = "HTTPS from CloudFront only; outbound for packages, S3, SSM, Route 53, Lets Encrypt"
  vpc_id      = var.vpc_id

  ingress {
    description     = "CloudFront origin-facing"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_instance" "host" {
  ami                         = data.aws_ssm_parameter.ubuntu_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.host.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  associate_public_ip_address = true # changes per start; the boot script updates origin.<hostname>

  # The idle watchdog powers the OS off; that must STOP the instance, not terminate it.
  instance_initiated_shutdown_behavior = "stop"

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    conf = join("\n", [
      "REGION=${var.aws_region}",
      "BUCKET=${aws_s3_bucket.bundle.id}",
      "KEY=${local.bundle_key}",
      "PARAM_PREFIX=${local.param_prefix}",
      "ZONE_ID=${data.aws_route53_zone.main.zone_id}",
      "ORIGIN_HOSTNAME=${local.origin_host}",
      "IDLE_MINUTES=${var.idle_minutes}",
      "MAX_UPTIME_HOURS=${var.max_uptime_hours}",
    ])
    boot_sh    = filebase64("${path.module}/../host/srt-vast-boot.sh")
    idle_sh    = filebase64("${path.module}/../host/srt-vast-idle.sh")
    boot_unit  = filebase64("${path.module}/../host/srt-vast.service")
    idle_unit  = filebase64("${path.module}/../host/srt-vast-idle.service")
    idle_timer = filebase64("${path.module}/../host/srt-vast-idle.timer")
  })

  tags = { Name = var.name }

  lifecycle {
    # New Ubuntu AMIs / script edits must not replace a running host; code
    # updates arrive through the S3 bundle at the next boot instead.
    ignore_changes = [ami, user_data]
  }

  depends_on = [aws_s3_object.bundle, aws_ssm_parameter.settings]
}
