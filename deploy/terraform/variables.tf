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
  default     = null
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

variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_zone_id" {
  description = "Zone (domain) on Cloudflare that hosts the viewer hostname."
  type        = string
}

variable "hostname" {
  description = "Public hostname for the viewer, inside that zone, e.g. srt.example.com."
  type        = string
}

variable "access_team_domain" {
  description = "Cloudflare Zero Trust team domain, e.g. myteam.cloudflareaccess.com."
  type        = string
}

variable "allowed_emails" {
  description = "People allowed in. Add or remove an address and re-apply."
  type        = list(string)
}

variable "session_duration" {
  description = "Cloudflare Access session length; the viewer also ends streams at the token's expiry."
  type        = string
  default     = "30m"
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

variable "create_otp_login" {
  description = "Create the Cloudflare one-time PIN (email code) login method. Set false if the team already has it."
  type        = bool
  default     = true
}
