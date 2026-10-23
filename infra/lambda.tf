locals {
  lambda_name  = "movie-alert-checker"
  lambda_arn   = "arn:aws:lambda:${var.aws_region}:${var.expected_account_id}:function:${local.lambda_name}"
  group_name   = "movie-alert"
  group_arn    = "arn:aws:scheduler:${var.aws_region}:${var.expected_account_id}:schedule-group/${local.group_name}"
  boundary_arn = "arn:aws:iam::${var.expected_account_id}:policy/MovieAlertRuntimeBoundary"
  manifest     = var.deploy_lambda ? jsondecode(file("${path.module}/../build/lambda-manifest.json")) : null
}

resource "aws_cloudwatch_log_group" "checker" {
  count             = var.deploy_lambda ? 1 : 0
  name              = "/aws/lambda/${local.lambda_name}"
  retention_in_days = 7
  tags              = { Project = "movie-alert", ManagedBy = "terraform" }
}

resource "aws_iam_role" "lambda" {
  count                = var.deploy_lambda ? 1 : 0
  name                 = "movie-alert-lambda"
  permissions_boundary = local.boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
    }]
  })
  tags = { Project = "movie-alert", ManagedBy = "terraform" }
}

resource "aws_iam_role_policy" "lambda" {
  count = var.deploy_lambda ? 1 : 0
  name  = "movie-alert-runtime"
  role  = aws_iam_role.lambda[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:UpdateItem"], Resource = aws_dynamodb_table.notifications.arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.checker[0].arn}:*" }
    ]
  })
}

resource "aws_lambda_function" "checker" {
  count                          = var.deploy_lambda ? 1 : 0
  function_name                  = local.lambda_name
  role                           = aws_iam_role.lambda[0].arn
  handler                        = "movie_alert.lambda_handler.handler"
  runtime                        = "python3.13"
  architectures                  = ["arm64"]
  filename                       = "${path.module}/../build/movie-alert-lambda.zip"
  code_sha256                    = var.deploy_lambda ? filebase64sha256("${path.module}/../build/movie-alert-lambda.zip") : null
  memory_size                    = 256
  timeout                        = 180
  reserved_concurrent_executions = var.reserved_concurrency

  environment {
    variables = {
      DISCORD_WEBHOOK_URL        = var.discord_webhook_url
      MOVIE_ALERT_DYNAMODB_TABLE = aws_dynamodb_table.notifications.name
      MOVIE_ALERT_CONFIG         = "config/watch.toml"
    }
  }

  lifecycle {
    precondition {
      condition     = length(var.discord_webhook_url) > 0
      error_message = "A private Discord webhook is required when deploy_lambda is enabled."
    }
    precondition {
      condition     = var.deploy_lambda ? filesha256("${path.module}/../build/movie-alert-lambda.zip") == local.manifest.archive_sha256 : true
      error_message = "Rebuild the Lambda package and manifest together."
    }
  }
  depends_on = [aws_iam_role_policy.lambda, aws_cloudwatch_log_group.checker]
  tags       = { Project = "movie-alert", ManagedBy = "terraform" }
}

resource "aws_lambda_function_event_invoke_config" "checker" {
  count                        = var.deploy_lambda ? 1 : 0
  function_name                = aws_lambda_function.checker[0].function_name
  maximum_event_age_in_seconds = 60
  maximum_retry_attempts       = 0
}

resource "aws_scheduler_schedule_group" "movie_alert" {
  count = var.deploy_lambda ? 1 : 0
  name  = local.group_name
  tags  = { Project = "movie-alert", ManagedBy = "terraform" }
}

resource "aws_iam_role" "scheduler" {
  count                = var.deploy_lambda ? 1 : 0
  name                 = "movie-alert-scheduler"
  permissions_boundary = local.boundary_arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.expected_account_id }
        ArnEquals    = { "aws:SourceArn" = local.group_arn }
      }
    }]
  })
  tags = { Project = "movie-alert", ManagedBy = "terraform" }
}

resource "aws_iam_role_policy" "scheduler" {
  count = var.deploy_lambda ? 1 : 0
  name  = "movie-alert-invoke"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = local.lambda_arn }]
  })
}

resource "aws_scheduler_schedule" "checker" {
  count                        = var.deploy_lambda ? 1 : 0
  name                         = "movie-alert-checker"
  group_name                   = aws_scheduler_schedule_group.movie_alert[0].name
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"
  schedule_expression          = "rate(1 minute)"
  schedule_expression_timezone = "Asia/Seoul"
  # UpdateSchedule rejects a StartDate more than five minutes in the past.
  # A missing start date starts immediately; the app still checks active_from.
  start_date = timecmp(local.manifest.active_from, plantimestamp()) > 0 ? local.manifest.active_from : null
  end_date   = local.manifest.active_until
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.checker[0].arn
    role_arn = aws_iam_role.scheduler[0].arn
    # jsonencode escapes angle brackets, preventing Scheduler substitution.
    input = <<-JSON
      {"notify":true,"checks":1,"scheduled_at":"<aws.scheduler.scheduled-time>"}
    JSON
    retry_policy {
      maximum_event_age_in_seconds = 60
      maximum_retry_attempts       = 0
    }
  }
  depends_on = [aws_iam_role_policy.scheduler, aws_lambda_function_event_invoke_config.checker]
}
