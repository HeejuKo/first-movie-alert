variable "expected_account_id" {
  description = "AWS account ID checked before any infrastructure change."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must contain the intended 12-digit AWS account ID."
  }
}

variable "aws_region" {
  description = "AWS region for the notification state table."
  type        = string
  default     = "ap-northeast-2"
}

variable "table_name" {
  description = "DynamoDB table used by MOVIE_ALERT_DYNAMODB_TABLE."
  type        = string
  default     = "movie-alert-state"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{3,255}$", var.table_name))
    error_message = "table_name must contain 3 to 255 letters, digits, underscores, dots or hyphens."
  }
}

variable "read_capacity" {
  description = "Provisioned read capacity; review costs before increasing."
  type        = number
  default     = 1

  validation {
    condition     = var.read_capacity >= 1 && var.read_capacity == floor(var.read_capacity)
    error_message = "read_capacity must be a positive integer."
  }
}

variable "write_capacity" {
  description = "Provisioned write capacity; review throttling and costs before increasing."
  type        = number
  default     = 1

  validation {
    condition     = var.write_capacity >= 1 && var.write_capacity == floor(var.write_capacity)
    error_message = "write_capacity must be a positive integer."
  }
}

variable "deletion_protection_enabled" {
  description = "Protect sent notification records against accidental table deletion."
  type        = bool
  default     = true
}

variable "deploy_lambda" {
  description = "Deploy the checker only after packaging, permission and cost review."
  type        = bool
  default     = false
}

variable "schedule_enabled" {
  description = "Enable after a successful Lambda smoke test."
  type        = bool
  default     = false
}

variable "discord_webhook_url" {
  description = "Private secret: stored in protected local Terraform state and plan."
  type        = string
  sensitive   = true
  default     = ""
}

variable "reserved_concurrency" {
  description = "Use the DynamoDB polling lease by default; optional reservation if account quota allows."
  type        = number
  default     = -1
  validation {
    condition     = contains([-1, 1], var.reserved_concurrency)
    error_message = "Use -1, or 1 after reviewing account concurrency limits."
  }
}
