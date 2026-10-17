# Read-only auditor: verifies the locked audit copies. The app's role stays put-only.

resource "aws_iam_role" "auditor" {
  name                 = "accounting-assistant-auditor"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::975050154381:user/jalloh" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "auditor_read" {
  name = "audit-bucket-read-only"
  role = aws_iam_role.auditor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ListAuditPrefix"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = "arn:aws:s3:::${local.audit_bucket}"
        Condition = { StringLike = { "s3:prefix" = ["audit/*", "audit/"] } }
      },
      {
        Sid      = "ReadBucketLockSettings"
        Effect   = "Allow"
        Action   = ["s3:GetBucketObjectLockConfiguration", "s3:GetBucketVersioning"]
        Resource = "arn:aws:s3:::${local.audit_bucket}"
      },
      {
        Sid      = "ReadAuditObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention", "s3:GetObjectLegalHold"]
        Resource = "arn:aws:s3:::${local.audit_bucket}/audit/*"
      },
      {
        Sid    = "NeverWrite"
        Effect = "Deny"
        Action = [
          "s3:PutObject*", "s3:DeleteObject*", "s3:PutBucket*", "s3:DeleteBucket*",
          "s3:BypassGovernanceRetention", "s3:RestoreObject"
        ]
        Resource = ["arn:aws:s3:::${local.audit_bucket}", "arn:aws:s3:::${local.audit_bucket}/*"]
      }
    ]
  })
}

output "auditor_role_arn" {
  value = aws_iam_role.auditor.arn
}
