terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Real environments keep state in S3 (not needed for `terraform validate`):
  # backend "s3" {
  #   bucket       = "<state-bucket>"
  #   key          = "inventory-service/terraform.tfstate"
  #   region       = "us-west-2"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = var.tags
  }
}
