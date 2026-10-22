# Sign-in: the Cognito user pool from deploy/bootstrap (admin-created users
# only), Cognito's hosted login page, and an app client whose ID token lasts
# `session_minutes`. The starter Lambda runs the OAuth code flow and puts the
# ID token in a cookie; the viewer verifies it and ends streams at its expiry.

resource "random_id" "auth_domain" {
  byte_length = 4
}

locals {
  issuer = "https://cognito-idp.${var.aws_region}.amazonaws.com/${var.user_pool_id}"
}

resource "aws_cognito_user_pool_domain" "login" {
  domain       = "${var.name}-${random_id.auth_domain.hex}"
  user_pool_id = var.user_pool_id
}

resource "aws_cognito_user_pool_client" "viewer" {
  name                                 = "${var.name}-viewer"
  user_pool_id                         = var.user_pool_id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${var.hostname}/_auth/callback"]
  logout_urls                          = ["https://${var.hostname}/_auth/signed-out"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"

  id_token_validity      = var.session_minutes
  access_token_validity  = var.session_minutes
  refresh_token_validity = 60 # minimum; never used (no silent renewal: the session is a hard cap)
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "minutes"
  }
}

resource "aws_cognito_user" "allowed" {
  for_each                 = toset([for e in var.allowed_emails : lower(e)])
  user_pool_id             = var.user_pool_id
  username                 = each.value
  desired_delivery_mediums = ["EMAIL"]
  attributes = {
    email          = each.value
    email_verified = true
  }
}
