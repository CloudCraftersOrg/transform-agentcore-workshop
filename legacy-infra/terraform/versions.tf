terraform {
  required_version = ">= 1.6"

  # State stays on the presenter's laptop: this estate is short-lived and single-user.
  backend "local" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      app        = "legacy-inventory"
      project    = var.project
      managed-by = "terraform"
    }
  }
}
