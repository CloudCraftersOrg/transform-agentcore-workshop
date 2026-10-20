# Legacy Inventory & Orders: one EC2 instance in the default VPC, rebuilt the way it was set up by hand in 2017.

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "default" {
  default = true
}

# Not every AZ offers every instance type, so only consider the ones that do.
data "aws_ec2_instance_type_offerings" "available" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }

  filter {
    name   = "availability-zone"
    values = data.aws_ec2_instance_type_offerings.available.locations
  }

  lifecycle {
    postcondition {
      condition     = length(self.ids) > 0
      error_message = "No default subnet found in an AZ that offers ${var.instance_type}."
    }
  }
}

locals {
  app_dir = "${path.module}/../app"

  # The app ships inside user_data: every file gzip+base64 encoded (no S3 bucket needed).
  app_files = {
    for f in fileset(local.app_dir, "**") :
    f => base64gzip(file("${local.app_dir}/${f}"))
    if !can(regex("(__pycache__|\\.pyc$|\\.DS_Store$|\\.db$)", f))
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    app_files        = local.app_files
    cloudwatch_agent = var.enable_cloudwatch_agent
  })
}

# --- IAM: Session Manager instead of SSH, plus the CloudWatch agent ---
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "legacy" {
  name_prefix        = "legacy-inventory-"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "legacy" {
  for_each = toset(["AmazonSSMManagedInstanceCore", "CloudWatchAgentServerPolicy"])

  role       = aws_iam_role.legacy.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

resource "aws_iam_instance_profile" "legacy" {
  name_prefix = "legacy-inventory-"
  role        = aws_iam_role.legacy.name
}

# --- Network: HTTP in, everything out, no port 22 ---
resource "aws_security_group" "legacy" {
  name_prefix = "legacy-inventory-"
  description = "Legacy Inventory: HTTP in, all out. No SSH (use SSM Session Manager)."
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "legacy-inventory"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.legacy.id
  description       = "HTTP to nginx"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = var.allowed_cidr
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.legacy.id
  description       = "All outbound: dnf, PyPI, SSM, CloudWatch"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# --- The server: one instance, one AZ, no load balancer, no Auto Scaling group ---
resource "aws_instance" "legacy" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.legacy.id]
  iam_instance_profile        = aws_iam_instance_profile.legacy.name
  associate_public_ip_address = true
  user_data                   = local.user_data
  user_data_replace_on_change = true

  root_block_device {
    volume_type           = var.root_volume_type
    volume_size           = var.root_volume_size_gib
    delete_on_termination = true
  }

  metadata_options {
    http_tokens = "required"
  }

  tags = {
    Name = "legacy-inventory"
    app  = "legacy-inventory"
  }

  lifecycle {
    # A newer AL2023 AMI must not replace the server on the next plan.
    ignore_changes = [ami]

    precondition {
      condition     = length(local.user_data) <= 16384
      error_message = "user_data is over the 16 KB EC2 limit; ship the app through S3 instead."
    }
  }
}
