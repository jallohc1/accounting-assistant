variable "image_tag" {
  description = "Image to run at first boot. Later deploys go through Session Manager."
  default     = "98a6b4a"
}

locals {
  domain         = "assistant.mounam.site"
  zone           = "mounam.site"
  registry       = split("/", aws_ecr_repository.app.repository_url)[0]
  audit_bucket   = "accounting-assistant-audit-975050154381"
  app_secret_arn = "arn:aws:secretsmanager:us-east-1:975050154381:secret:accounting-assistant/app-2Oh5Q7"
  qbo_secret_arn = "arn:aws:secretsmanager:us-east-1:975050154381:secret:accounting-assistant/qbo/phoenix-jujwEN"
  base_env = {
    ANTHROPIC_MODEL      = "claude-sonnet-5-5"
    BASE_CURRENCY        = "USD"
    FISCAL_YEAR_START    = "01-01"
    READ_ONLY            = "false"
    QBO_BASE_URL         = "https://sandbox-quickbooks.api.intuit.com"
    QBO_REALM_ID         = "9341458378795435"
    QBO_SECRET_ID        = "accounting-assistant/qbo/phoenix"
    AUDIT_BUCKET         = "accounting-assistant-audit-975050154381"
    AUDIT_RETENTION_DAYS = "1"
    DB_PATH              = "/data/ledger.db"
    CHAT_DB_PATH         = "/data/chats.db"
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "app" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = "us-east-1a"
  default_for_az    = true
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_route53_zone" "main" {
  name         = local.zone
  private_zone = false
}

resource "aws_security_group" "web" {
  name        = "accounting-assistant-web"
  description = "HTTP and HTTPS only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
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

resource "aws_iam_role" "app" {
  name = "accounting-assistant-app"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "app" {
  name = "accounting-assistant-app"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.app.arn },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:PutObjectRetention"], Resource = "arn:aws:s3:::${local.audit_bucket}/audit/*" },
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = [local.app_secret_arn, local.qbo_secret_arn] },
      { Effect = "Allow", Action = "secretsmanager:PutSecretValue", Resource = local.qbo_secret_arn },
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "accounting-assistant-app"
  role = aws_iam_role.app.name
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.app.availability_zone
  size              = 10
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "accounting-assistant-data", Backup = "daily" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.insecure_value
  instance_type          = "t4g.small"
  subnet_id              = data.aws_subnet.app.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    domain    = local.domain
    registry  = local.registry
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    volume_id = replace(aws_ebs_volume.data.id, "-", "")
    base_env  = join("\n", [for k, v in local.base_env : "${k}=${v}"])
  })

  tags = { Name = "accounting-assistant" }

  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.app.id
}

resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id
  tags     = { Name = "accounting-assistant" }
}

resource "aws_route53_record" "app" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.domain
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}

resource "aws_iam_role" "dlm" {
  name = "accounting-assistant-dlm"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "dlm.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data" {
  description        = "Daily snapshots of the app data volume"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"
  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Backup = "daily" }
    schedule {
      name = "daily"
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["05:00"]
      }
      retain_rule {
        count = 7
      }
      copy_tags = true
    }
  }
}

output "url" {
  value = "https://${local.domain}"
}

output "instance_id" {
  value = aws_instance.app.id
}

output "public_ip" {
  value = aws_eip.app.public_ip
}