mock_provider "aws" {}

variables {
  expected_account_id = "123456789012"
}

run "secure_state_and_main_only_oidc" {
  command = plan
  assert {
    condition     = aws_s3_bucket_public_access_block.state.block_public_acls && aws_s3_bucket_public_access_block.state.block_public_policy && aws_s3_bucket_public_access_block.state.ignore_public_acls && aws_s3_bucket_public_access_block.state.restrict_public_buckets
    error_message = "State must never be public."
  }
  assert {
    condition     = one(aws_s3_bucket_versioning.state.versioning_configuration).status == "Enabled" && one(one(aws_s3_bucket_server_side_encryption_configuration.state.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "Keep recovery versions and encryption at rest."
  }
  assert {
    condition     = jsondecode(aws_iam_role.deploy.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:HeejuKo@142784710/first-movie-alert@1428272327:ref:refs/heads/main"
    error_message = "Trust only this immutable GitHub repository subject on main."
  }
  assert {
    condition     = aws_iam_role.deploy.permissions_boundary == "arn:aws:iam::123456789012:policy/MovieAlertGithubDeploymentBoundary"
    error_message = "Delegated bootstrap must not create an unbounded deployment role."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.state.policy).Statement[2].Resource == "arn:aws:s3:::movie-alert-tfstate-123456789012-ap-northeast-2/state/movie-alert.tfstate.tflock"
    error_message = "Deleting state must not be granted; DeleteObject is lock-only."
  }
}

run "reuse_verified_oidc_provider" {
  command = plan
  variables {
    existing_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
  }
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 0
    error_message = "Do not recreate an existing account-wide OIDC provider."
  }
}
