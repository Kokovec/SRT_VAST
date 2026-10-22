# Cloudflare front door: tunnel to the viewer, Access login in front of it,
# and the Worker that wakes the instance.

resource "random_id" "tunnel_secret" {
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "viewer" {
  account_id = var.cloudflare_account_id
  name       = var.name
  secret     = random_id.tunnel_secret.b64_std
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "viewer" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.viewer.id

  config {
    ingress_rule {
      hostname = var.hostname
      service  = "http://viewer:8090" # compose service name, same docker network as cloudflared
    }
    ingress_rule {
      service = "http_status:404"
    }
  }
}

resource "cloudflare_record" "viewer" {
  zone_id = var.cloudflare_zone_id
  name    = var.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.viewer.id}.cfargotunnel.com"
  proxied = true
}

# ---------------------------------------------------------------- Access

resource "cloudflare_zero_trust_access_identity_provider" "otp" {
  count      = var.create_otp_login ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Email one-time PIN"
  type       = "onetimepin"
}

resource "cloudflare_zero_trust_access_application" "viewer" {
  zone_id                   = var.cloudflare_zone_id
  name                      = var.name
  domain                    = var.hostname
  type                      = "self_hosted"
  session_duration          = var.session_duration
  app_launcher_visible      = false
  auto_redirect_to_identity = var.create_otp_login
  allowed_idps              = var.create_otp_login ? [cloudflare_zero_trust_access_identity_provider.otp[0].id] : null
}

resource "cloudflare_zero_trust_access_policy" "allowed" {
  application_id = cloudflare_zero_trust_access_application.viewer.id
  zone_id        = var.cloudflare_zone_id
  name           = "Allowed people"
  precedence     = 1
  decision       = "allow"

  include {
    email = var.allowed_emails
  }
}

# ---------------------------------------------------------------- waker Worker

resource "cloudflare_workers_script" "waker" {
  account_id         = var.cloudflare_account_id
  name               = "${var.name}-waker"
  content            = file("${path.module}/../worker/waker.js")
  module             = true
  compatibility_date = "2025-09-01"

  plain_text_binding {
    name = "INSTANCE_ID"
    text = aws_instance.host.id
  }
  plain_text_binding {
    name = "AWS_REGION"
    text = var.aws_region
  }
  secret_text_binding {
    name = "AWS_ACCESS_KEY_ID"
    text = aws_iam_access_key.waker.id
  }
  secret_text_binding {
    name = "AWS_SECRET_ACCESS_KEY"
    text = aws_iam_access_key.waker.secret
  }
}

resource "cloudflare_workers_route" "waker" {
  zone_id     = var.cloudflare_zone_id
  pattern     = "${var.hostname}/*"
  script_name = cloudflare_workers_script.waker.name
}
