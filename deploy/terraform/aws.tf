# EC2 host for the docker compose stack. No inbound access at all: the
# Cloudflare tunnel dials out, admin access is SSM Session Manager.

data "aws_ssm_parameter" "ubuntu_arm64" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_caller_identity" "me" {}

locals {
  param_prefix = "/${var.name}"
  bundle_key   = "bundle/${var.name}.zip"
  repo_root    = abspath("${path.module}/../..")
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
    "deploy/terraform/terraform.tfvars", "**/__pycache__/**",
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

# ---------------------------------------------------------------- settings / secrets

resource "aws_ssm_parameter" "tunnel_token" {
  name  = "${local.param_prefix}/TUNNEL_TOKEN"
  type  = "SecureString"
  value = cloudflare_zero_trust_tunnel_cloudflared.viewer.tunnel_token
}

resource "aws_ssm_parameter" "access_team" {
  name  = "${local.param_prefix}/CF_ACCESS_TEAM_DOMAIN"
  type  = "String"
  value = var.access_team_domain
}

resource "aws_ssm_parameter" "access_aud" {
  name  = "${local.param_prefix}/CF_ACCESS_AUD"
  type  = "String"
  value = cloudflare_zero_trust_access_application.viewer.aud
}

# ---------------------------------------------------------------- instance role

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
  name_prefix          = "${var.name}-host-"
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
  description = "No inbound; outbound for the tunnel, package installs, S3/SSM"
  vpc_id      = data.aws_vpc.default.id

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
  subnet_id                   = data.aws_subnets.default.ids[0]
  vpc_security_group_ids      = [aws_security_group.host.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  associate_public_ip_address = true # outbound only (no inbound rules); avoids paying for a NAT gateway

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
      "IDLE_MINUTES=${var.idle_minutes}",
      "MAX_UPTIME_HOURS=${var.max_uptime_hours}",
    ])
    boot_sh      = filebase64("${path.module}/../host/srt-vast-boot.sh")
    idle_sh      = filebase64("${path.module}/../host/srt-vast-idle.sh")
    boot_unit    = filebase64("${path.module}/../host/srt-vast.service")
    idle_unit    = filebase64("${path.module}/../host/srt-vast-idle.service")
    idle_timer   = filebase64("${path.module}/../host/srt-vast-idle.timer")
  })

  tags = { Name = var.name }

  lifecycle {
    # New Ubuntu AMIs / script edits must not replace a running host; code
    # updates arrive through the S3 bundle at the next boot instead.
    ignore_changes = [ami, user_data]
  }

  depends_on = [aws_s3_object.bundle, aws_ssm_parameter.tunnel_token, aws_ssm_parameter.access_aud]
}

# ---------------------------------------------------------------- waker credentials

# The Cloudflare Worker uses these to start the instance on sign-in. They can
# describe instances and start this one instance, nothing else.
resource "aws_iam_user" "waker" {
  name                 = "${var.name}-waker"
  permissions_boundary = var.permissions_boundary_arn
}

data "aws_iam_policy_document" "waker" {
  statement {
    sid       = "StartThisInstance"
    actions   = ["ec2:StartInstances"]
    resources = [aws_instance.host.arn]
  }
  statement {
    sid       = "ReadState"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"] # DescribeInstances has no resource-level permissions
  }
}

resource "aws_iam_user_policy" "waker" {
  user   = aws_iam_user.waker.name
  policy = data.aws_iam_policy_document.waker.json
}

resource "aws_iam_access_key" "waker" {
  user = aws_iam_user.waker.name
}
