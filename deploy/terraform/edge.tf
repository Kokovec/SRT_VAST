# CloudFront in front of everything:
#   default  -> origin group: the instance (HTTPS, origin.<hostname>), failing
#               over to the starter Lambda when the instance is stopped/booting
#   /_auth/* -> the starter Lambda (sign-in flow)

resource "aws_acm_certificate" "viewer" {
  provider          = aws.us_east_1
  domain_name       = var.hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.viewer.domain_validation_options : o.domain_name => o
  }
  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "viewer" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.viewer.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# The host rewrites this on every boot with its new public IP.
resource "aws_route53_record" "origin" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.origin_host
  type    = "A"
  ttl     = 30
  records = [aws_instance.host.public_ip]

  lifecycle {
    ignore_changes = [records]
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_but_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_origin_access_control" "starter" {
  name                              = "${var.name}-starter"
  description                       = "CloudFront -> starter Lambda function URL"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

locals {
  starter_origin = trimsuffix(trimprefix(aws_lambda_function_url.starter.function_url, "https://"), "/")
}

resource "aws_cloudfront_distribution" "viewer" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${var.name} viewer"
  aliases         = [var.hostname]
  price_class     = "PriceClass_100"

  origin {
    origin_id           = "instance"
    domain_name         = local.origin_host
    connection_attempts = 1
    connection_timeout  = 2 # fail over fast when the instance is stopped

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60 # max gap between bytes; streams send far more often
      origin_keepalive_timeout = 5
    }
  }

  origin {
    origin_id                = "starter"
    domain_name              = local.starter_origin
    origin_access_control_id = aws_cloudfront_origin_access_control.starter.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  origin_group {
    origin_id = "instance-or-starter"
    failover_criteria {
      status_codes = [500, 502, 503, 504]
    }
    member {
      origin_id = "instance"
    }
    member {
      origin_id = "starter"
    }
  }

  default_cache_behavior {
    target_origin_id         = "instance-or-starter"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_but_host.id
    compress                 = false
  }

  ordered_cache_behavior {
    path_pattern             = "/_auth/*"
    target_origin_id         = "starter"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_but_host.id
    compress                 = true
  }

  # Never cache errors: "starting" answers must not stick after the instance is up.
  dynamic "custom_error_response" {
    for_each = [500, 502, 503, 504]
    content {
      error_code            = custom_error_response.value
      error_caching_min_ttl = 0
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.viewer.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "viewer" {
  for_each = toset(["A", "AAAA"])
  zone_id  = data.aws_route53_zone.main.zone_id
  name     = var.hostname
  type     = each.value

  alias {
    name                   = aws_cloudfront_distribution.viewer.domain_name
    zone_id                = aws_cloudfront_distribution.viewer.hosted_zone_id
    evaluate_target_health = false
  }
}
