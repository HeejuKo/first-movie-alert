# Mocked provider: no AWS credentials, API requests, resources or charges.
variables {
  deploy_lambda       = false
  schedule_enabled    = false
  discord_webhook_url = ""
}

mock_provider "aws" {
  mock_resource "aws_dynamodb_table" {
    defaults = {
      arn = "arn:aws:dynamodb:ap-northeast-2:123456789012:table/movie-alert-state"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/movie-alert-lambda"
    }
  }
  mock_resource "aws_lambda_function" {
    defaults = {
      arn = "arn:aws:lambda:ap-northeast-2:123456789012:function:movie-alert-checker"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:ap-northeast-2:123456789012:log-group:/aws/lambda/movie-alert-checker"
    }
  }
}

run "notification_state_contract" {
  command = apply

  variables {
    expected_account_id = "123456789012"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.hash_key == "id" && one(aws_dynamodb_table.notifications.attribute).type == "S"
    error_message = "The app requires a string partition key named id."
  }

  assert {
    condition     = one(aws_dynamodb_table.notifications.ttl).enabled && one(aws_dynamodb_table.notifications.ttl).attribute_name == "expires_at"
    error_message = "Notification retention requires TTL on expires_at."
  }

  assert {
    condition     = aws_dynamodb_table.notifications.billing_mode == "PROVISIONED" && aws_dynamodb_table.notifications.read_capacity == 1 && aws_dynamodb_table.notifications.write_capacity == 1
    error_message = "Default capacity must stay at the reviewed 1 RCU / 1 WCU."
  }

  assert {
    condition     = aws_dynamodb_table.notifications.deletion_protection_enabled && !aws_dynamodb_table.notifications.stream_enabled && !one(aws_dynamodb_table.notifications.point_in_time_recovery).enabled
    error_message = "Protect deduplication records and keep optional paid features disabled by default."
  }

  assert {
    condition     = jsondecode(output.runtime_dynamodb_policy_json).Statement[0].Action == ["dynamodb:UpdateItem"] && jsondecode(output.runtime_dynamodb_policy_json).Statement[0].Resource == aws_dynamodb_table.notifications.arn
    error_message = "App permissions must allow only UpdateItem on the notification table."
  }
}

run "reject_invalid_account_id" {
  command = plan

  variables {
    expected_account_id = "not-an-account"
  }

  expect_failures = [var.expected_account_id]
}

# Build the local ZIP/manifest before this run; mock apply never calls AWS.
run "scheduled_checker_contract" {
  command = apply
  variables {
    expected_account_id = "123456789012"
    deploy_lambda       = true
    discord_webhook_url = "https://discord.com/api/webhooks/synthetic/token"
  }
  assert {
    condition     = aws_lambda_function.checker[0].runtime == "python3.13" && one(aws_lambda_function.checker[0].architectures) == "arm64" && aws_lambda_function.checker[0].memory_size == 256
    error_message = "Package runtime, architecture and reviewed memory must match."
  }
  assert {
    condition     = aws_lambda_function.checker[0].reserved_concurrent_executions == -1 && aws_cloudwatch_log_group.checker[0].retention_in_days == 7
    error_message = "Bound concurrency and log retention."
  }
  assert {
    condition     = aws_scheduler_schedule.checker[0].schedule_expression == "rate(1 minute)" && aws_scheduler_schedule.checker[0].state == "DISABLED" && aws_scheduler_schedule.checker[0].end_date == local.manifest.active_until
    error_message = "Scheduler must start disabled and stop at the configured boundary."
  }
  assert {
    condition     = jsondecode(aws_iam_role.scheduler[0].assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == local.group_arn && jsondecode(aws_iam_role_policy.scheduler[0].policy).Statement[0].Resource == local.lambda_arn
    error_message = "Scheduler trust must use the schedule group ARN and invoke only the checker."
  }
  assert {
    condition     = aws_iam_role.lambda[0].permissions_boundary == local.boundary_arn && aws_iam_role.scheduler[0].permissions_boundary == local.boundary_arn
    error_message = "Both execution roles must stay within the pre-created runtime boundary."
  }
  assert {
    condition     = aws_lambda_function_event_invoke_config.checker[0].maximum_retry_attempts == 0 && one(aws_scheduler_schedule.checker[0].target).retry_policy[0].maximum_retry_attempts == 0
    error_message = "Do not queue repeated polls after transient failures."
  }
  assert {
    condition     = strcontains(one(aws_scheduler_schedule.checker[0].target).input, "<aws.scheduler.scheduled-time>") && jsondecode(one(aws_scheduler_schedule.checker[0].target).input).notify
    error_message = "Scheduler context substitution requires a literal, unescaped placeholder in valid JSON."
  }
}
