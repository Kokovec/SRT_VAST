output "url" {
  description = "Share this with the allowed people (they also get an email with a temporary password)."
  value       = "https://${var.hostname}"
}

output "instance_id" {
  value = aws_instance.host.id
}

output "ssm_shell" {
  description = "Admin shell on the host (needs the Session Manager plugin)."
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.host.id}"
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.viewer.domain_name
}

output "starter_role_name" {
  description = "For deploy/bootstrap's budget action (blocks restarts when the budget is hit)."
  value       = aws_iam_role.starter.name
}
