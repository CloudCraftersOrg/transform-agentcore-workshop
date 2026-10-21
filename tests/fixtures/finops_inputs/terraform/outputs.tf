output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS API endpoint."
  value       = module.eks.cluster_endpoint
}

output "configure_kubectl" {
  description = "Command that adds the cluster to your kubeconfig."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "ecr_repository_url" {
  description = "Push the service image here."
  value       = aws_ecr_repository.app.repository_url
}

output "db_endpoint" {
  description = "PostgreSQL host name (private)."
  value       = aws_db_instance.db.address
}

output "db_secret_arn" {
  description = "Secrets Manager secret with the database master credentials."
  value       = aws_db_instance.db.master_user_secret[0].secret_arn
}

output "app_role_arn" {
  description = "IAM role bound to the service account through EKS Pod Identity."
  value       = aws_iam_role.app.arn
}

output "reports_bucket" {
  description = "S3 bucket for the nightly report."
  value       = aws_s3_bucket.reports.id
}

output "vpc_id" {
  description = "VPC ID."
  value       = module.vpc.vpc_id
}
