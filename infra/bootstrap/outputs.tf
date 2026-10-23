output "state_bucket" {
  value = local.bucket_name
}

output "deploy_role_arn" {
  value = "arn:aws:iam::${var.expected_account_id}:role/movie-alert-github-deploy"
}

output "expected_oidc_subject" {
  value = local.oidc_sub
}

output "developer_state_policy_json" {
  description = "Read/write only the app state; deletion is limited to the lock object."
  value       = jsonencode(local.state_access)
}
