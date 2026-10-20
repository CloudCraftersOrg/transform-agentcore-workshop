variable "region" {
  description = "AWS region of the legacy estate."
  type        = string
  default     = "us-west-2"
}

variable "project" {
  description = "Value of the project tag on every resource."
  type        = string
  default     = "transform-agentcore-workshop"
}

variable "instance_type" {
  description = "EC2 instance type. Deliberately oversized: the app idles at a few percent CPU."
  type        = string
  default     = "t3.large"
}

variable "root_volume_type" {
  description = "Root EBS volume type. gp2 is the old default (gp3 is cheaper and faster)."
  type        = string
  default     = "gp2"
}

variable "root_volume_size_gib" {
  description = "Root EBS volume size in GiB. Holds the OS, the SQLite database, reports and logs."
  type        = number
  default     = 30
}

variable "allowed_cidr" {
  description = "CIDR allowed to reach the app on port 80. The default 0.0.0.0/0 keeps the demo simple; use your-ip/32 to lock it down."
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrhost(var.allowed_cidr, 0))
    error_message = "allowed_cidr must be a valid IPv4 CIDR, for example 203.0.113.10/32."
  }
}

variable "enable_cloudwatch_agent" {
  description = "Install the CloudWatch agent for memory and disk metrics. Best effort: it never blocks the bootstrap."
  type        = bool
  default     = true
}
