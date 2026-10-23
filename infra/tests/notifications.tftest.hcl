# Mocked provider: no AWS credentials, API requests, resources or charges.
mock_provider "aws" {
  mock_resource "aws_dynamodb_table" {
    defaults = {
      arn = "arn:aws:dynamodb:ap-northeast-2:123456789012:table/movie-alert-state"
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
