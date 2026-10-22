variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "srt-vast"
}

variable "aws_region" {
  type    = string
  default = "us-west-2"
}

variable "permissions_boundary_arn" {
  description = "Boundary from deploy/bootstrap; the deploy role can only create identities that carry it."
  type        = string
}

variable "vpc_id" {
  description = "VPC from deploy/bootstrap (`terraform output vpc_id`)."
  type        = string
}

variable "subnet_id" {
  description = "Public subnet from deploy/bootstrap (`terraform output subnet_id`)."
  type        = string
}

variable "user_pool_id" {
  description = "Cognito user pool from deploy/bootstrap (`terraform output user_pool_id`)."
  type        = string
}

variable "domain" {
  description = "Existing Route 53 public hosted zone, e.g. example.com. Only records under `hostname` are touched."
  type        = string
}

variable "hostname" {
  description = "Viewer URL host inside that zone, e.g. srt.example.com."
  type        = string
}

variable "allowed_emails" {
  description = "People allowed in. Each gets an email with a temporary password. Add/remove and re-apply."
  type        = list(string)
}

variable "manual_invite_emails" {
  description = "People allowed in whose credentials you hand over yourself: no Cognito email is sent; the temporary password is in `terraform output -json manual_invites`."
  type        = list(string)
  default     = []
}

variable "session_minutes" {
  description = "Session length: the ID token lifetime. Streams are cut and the user signed out when it ends."
  type        = number
  default     = 30
}

variable "instance_type" {
  description = "Graviton (arm64). 2 vCPU is the minimum for live encode + mezzanine conforms inside the cue preroll."
  type        = string
  default     = "c7g.large"
}

variable "root_volume_gb" {
  type    = number
  default = 20
}

variable "idle_minutes" {
  description = "Stop the instance after this long with nobody watching."
  type        = number
  default     = 5
}

variable "max_uptime_hours" {
  description = "Cost backstop: stop the instance after this long regardless."
  type        = number
  default     = 12
}
