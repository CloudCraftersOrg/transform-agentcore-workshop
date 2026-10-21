locals {
  app_url = "http://${coalesce(aws_instance.legacy.public_dns, aws_instance.legacy.public_ip)}"
}

output "app_url" {
  description = "Public URL of the legacy app (nginx on port 80)."
  value       = local.app_url
}

output "instance_id" {
  description = "EC2 instance ID (tag app=legacy-inventory)."
  value       = aws_instance.legacy.id
}

output "instance_type" {
  description = "EC2 instance type."
  value       = aws_instance.legacy.instance_type
}

output "public_ip" {
  description = "Public IPv4 address of the instance."
  value       = aws_instance.legacy.public_ip
}

output "availability_zone" {
  description = "The single AZ the estate runs in."
  value       = aws_instance.legacy.availability_zone
}

output "ssm_session_command" {
  description = "Open a shell on the instance (no SSH key, no port 22)."
  value       = "aws ssm start-session --target ${aws_instance.legacy.id} --region ${var.region}"
}

output "smoke_test" {
  description = "One-line smoke test."
  value       = "curl -sf ${local.app_url}/status && echo && curl -s ${local.app_url}/api/items"
}
