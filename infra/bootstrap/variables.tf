variable "expected_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "Set the intended 12-digit account ID."
  }
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "github_repository" {
  type    = string
  default = "HeejuKo/first-movie-alert"
}

# Public GitHub API metadata verified on 2026-10-23. Immutable subjects apply
# to this repository created after July 15, 2026. Verify the live OIDC login too.
variable "github_owner_id" {
  type    = string
  default = "142784710"
}

variable "github_repository_id" {
  type    = string
  default = "1428272327"
}

variable "existing_oidc_provider_arn" {
  description = "Set only after verifying an existing GitHub OIDC provider in this account."
  type        = string
  default     = ""
}
