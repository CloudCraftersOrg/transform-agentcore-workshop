# Every spec-driven value lives here. Adapt the defaults to the migration spec;
# main.tf only references these variables.

# --- Service and placement (spec.service, spec.target, spec.finops) ---

variable "name" {
  description = "Service name (spec.service.name). Prefixes every resource."
  type        = string
  default     = "app"
}

variable "environment" {
  description = "Environment (spec.finops.environment). prod keeps a final DB snapshot."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "AWS region (spec.target.region)."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Tags applied to every resource through the provider default_tags."
  type        = map(string)
  default = {
    app         = "app"
    environment = "dev"
    managed-by  = "terraform"
    migrated-by = "transform-agentcore"
  }
}

# --- Network ---

variable "vpc_cidr" {
  description = "VPC CIDR. Subnets are /24s carved from it."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = "Availability Zones to spread subnets, nodes and the database over (2 or more)."
  type        = number
  default     = 2
}

variable "single_nat_gateway" {
  description = "true = one shared NAT gateway (cheaper); false = one NAT gateway per AZ (survives an AZ loss)."
  type        = bool
  default     = true
}

# --- Kubernetes (spec.target, spec.workload, spec.finops) ---

variable "kubernetes_version" {
  description = "EKS Kubernetes version (spec.target.kubernetes_version)."
  type        = string
  default     = "1.33"
}

variable "node_architecture" {
  description = "Node CPU architecture: arm64 (Graviton) or x86_64. Must match node_instance_types."
  type        = string
  default     = "x86_64"
}

variable "node_instance_types" {
  description = "Managed node group instance types (Graviton families end in g: t4g, m7g, c7g)."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "node_capacity_type" {
  description = "ON_DEMAND or SPOT."
  type        = string
  default     = "ON_DEMAND"
}

variable "node_min" {
  description = "Minimum (and initial) number of nodes."
  type        = number
  default     = 1
}

variable "node_max" {
  description = "Maximum number of nodes."
  type        = number
  default     = 3
}

# --- Container registry (modernization.image.repository) ---

variable "ecr_repository_name" {
  description = "ECR repository for the service image."
  type        = string
  default     = "app"
}

# --- Database (spec.data, spec.nfr, spec.security) ---

variable "db_engine_version" {
  description = "RDS for PostgreSQL major version (spec.data.version)."
  type        = string
  default     = "17"
}

variable "db_instance_class" {
  description = "RDS instance class (Graviton: db.t4g.*, db.m7g.*)."
  type        = string
  default     = "db.t3.micro"
}

variable "db_allocated_storage_gib" {
  description = "Initial storage; storage autoscaling grows it up to 5x."
  type        = number
  default     = 20
}

variable "db_multi_az" {
  description = "Standby replica in a second AZ (spec.data.multi_az)."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Automated backup and point-in-time recovery window in days (RPO about 5 minutes)."
  type        = number
  default     = 1
}

variable "db_deletion_protection" {
  description = "Block accidental deletion of the database (true for prod)."
  type        = bool
  default     = false
}
