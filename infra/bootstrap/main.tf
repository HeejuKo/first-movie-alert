locals {
  bucket_name = "movie-alert-tfstate-${var.expected_account_id}-${var.aws_region}"
  bucket_arn  = "arn:aws:s3:::${local.bucket_name}"
  state_key   = "state/movie-alert.tfstate"
  oidc_arn    = var.existing_oidc_provider_arn != "" ? var.existing_oidc_provider_arn : "arn:aws:iam::${var.expected_account_id}:oidc-provider/token.actions.githubusercontent.com"
  repository  = split("/", var.github_repository)
  oidc_sub    = "repo:${local.repository[0]}@${var.github_owner_id}/${local.repository[1]}@${var.github_repository_id}:ref:refs/heads/main"
  tags        = { Project = "movie-alert", ManagedBy = "terraform" }
  state_access = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = local.bucket_arn },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = ["${local.bucket_arn}/${local.state_key}", "${local.bucket_arn}/${local.state_key}.tflock"] },
      { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${local.bucket_arn}/${local.state_key}.tflock" }
    ]
  }
}

resource "aws_s3_bucket" "state" {
  bucket        = local.bucket_name
  force_destroy = false
  tags          = local.tags
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [local.bucket_arn, "${local.bucket_arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
    }]
  })
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.existing_oidc_provider_arn == "" ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  tags           = local.tags
}

resource "aws_iam_role" "deploy" {
  name                 = "movie-alert-github-deploy"
  permissions_boundary = "arn:aws:iam::${var.expected_account_id}:policy/MovieAlertGithubDeploymentBoundary"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.oidc_arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = local.oidc_sub
        }
      }
    }]
  })
  tags       = local.tags
  depends_on = [aws_iam_openid_connect_provider.github]
}

resource "aws_iam_role_policy_attachment" "deployment" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::${var.expected_account_id}:policy/MovieAlertDeployment"
}

resource "aws_iam_role_policy" "table" {
  name   = "movie-alert-state-deployment"
  role   = aws_iam_role.deploy.id
  policy = replace(file("${path.module}/../iam/developer-policy.json.example"), "000000000000", var.expected_account_id)
}

resource "aws_iam_role_policy" "state" {
  name   = "movie-alert-terraform-state"
  role   = aws_iam_role.deploy.id
  policy = jsonencode(local.state_access)
}
