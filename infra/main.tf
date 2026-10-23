locals {
  health_read_statement = {
    Sid      = "ReadCheckerHealth"
    Effect   = "Allow"
    Action   = ["dynamodb:GetItem"]
    Resource = aws_dynamodb_table.notifications.arn
    Condition = {
      "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["health:v1:checker"] }
      "Null"                      = { "dynamodb:LeadingKeys" = "false" }
    }
  }
}

resource "aws_dynamodb_table" "notifications" {
  name                        = var.table_name
  billing_mode                = "PROVISIONED"
  table_class                 = "STANDARD"
  read_capacity               = var.read_capacity
  write_capacity              = var.write_capacity
  hash_key                    = "id"
  deletion_protection_enabled = var.deletion_protection_enabled
  stream_enabled              = false

  attribute {
    name = "id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = {
    Project   = "movie-alert"
    ManagedBy = "terraform"
  }
}
