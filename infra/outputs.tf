output "table_name" {
  description = "Set MOVIE_ALERT_DYNAMODB_TABLE to this value after a successful apply."
  value       = aws_dynamodb_table.notifications.name
}

output "aws_region" {
  description = "Set AWS_REGION to this value after a successful apply."
  value       = var.aws_region
}

output "table_arn" {
  description = "Notification state table ARN for IAM policies."
  value       = aws_dynamodb_table.notifications.arn
}

output "runtime_dynamodb_policy_json" {
  description = "Minimum app permissions; this output does not create or attach an IAM policy."
  value = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "UpdateNotificationState"
      Effect   = "Allow"
      Action   = ["dynamodb:UpdateItem"]
      Resource = aws_dynamodb_table.notifications.arn
    }]
  })
}

output "lambda_function_name" {
  value = try(aws_lambda_function.checker[0].function_name, null)
}

output "schedule_name" {
  value = try(aws_scheduler_schedule.checker[0].name, null)
}
