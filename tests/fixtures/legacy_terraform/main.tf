# Test fixture: the legacy single-EC2 estate, as the discovery agent expects to find it.
# The real one lives in legacy-infra/terraform; this copy keeps the discovery tests independent.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "instance_type" {
  type    = string
  default = "t3.large"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      app        = "legacy-inventory"
      managed_by = "terraform"
    }
  }
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_security_group" "web" {
  name        = "legacy-inventory-web"
  description = "HTTP from anywhere"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.web.id]
  user_data              = file("${path.module}/user_data.sh")

  root_block_device {
    volume_type = "gp2"
    volume_size = 30
  }

  tags = {
    Name = "legacy-inventory"
    app  = "legacy-inventory"
  }
}

output "public_ip" {
  value = aws_instance.app.public_ip
}
