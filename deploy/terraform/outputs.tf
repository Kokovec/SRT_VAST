output "url" {
  description = "Share this with the allowed people."
  value       = "https://${var.hostname}"
}

output "instance_id" {
  value = aws_instance.host.id
}

output "ssm_shell" {
  description = "Admin shell on the host (no SSH, no open ports)."
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.host.id}"
}

output "access_aud" {
  value = cloudflare_zero_trust_access_application.viewer.aud
}
