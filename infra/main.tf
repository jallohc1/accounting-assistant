terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { Project = "accounting-assistant" }
  }
}

resource "aws_ecr_repository" "app" {
  name                 = "accounting-assistant"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

output "ecr_url" {
  value = aws_ecr_repository.app.repository_url
}
resource "aws_ecr_registry_scanning_configuration" "this" {
  scan_type = "ENHANCED"

  rule {
    scan_frequency = "CONTINUOUS_SCAN"
    repository_filter {
      filter      = "accounting-assistant"
      filter_type = "WILDCARD"
    }
  }
}

# ---- Inspector finding alerts (HIGH/CRITICAL on accounting-assistant) ----

variable "alert_email" {
  type    = string
}

resource "aws_sns_topic" "findings" {
  name = "accounting-assistant-findings"
}

resource "aws_sns_topic_policy" "findings" {
  arn = aws_sns_topic.findings.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEventBridgePublish"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.findings.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.findings.arn } }
    }]
  })
}

resource "aws_sns_topic_subscription" "findings_email" {
  topic_arn = aws_sns_topic.findings.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_event_rule" "findings" {
  name        = "accounting-assistant-findings"
  description = "HIGH/CRITICAL Inspector findings on the accounting-assistant image"
  event_pattern = jsonencode({
    source        = ["aws.inspector2"]
    "detail-type" = ["Inspector2 Finding"]
    detail = {
      severity = ["HIGH", "CRITICAL"]
      resources = {
        details = {
          awsEcrContainerImage = { repositoryName = ["accounting-assistant"] }
        }
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "findings_sns" {
  rule = aws_cloudwatch_event_rule.findings.name
  arn  = aws_sns_topic.findings.arn

  input_transformer {
    input_paths = {
      sev    = "$.detail.severity"
      status = "$.detail.status"
      title  = "$.detail.title"
      fix    = "$.detail.fixAvailable"
      tag    = "$.detail.resources[0].details.awsEcrContainerImage.imageTags[0]"
      pkg    = "$.detail.packageVulnerabilityDetails.vulnerablePackages[0].name"
      fixed  = "$.detail.packageVulnerabilityDetails.vulnerablePackages[0].fixedInVersion"
    }
    input_template = "\"<sev> (<status>) on accounting-assistant:<tag> - <title>. Package: <pkg>. Fix available: <fix>, fixed in <fixed>.\""
  }
}
