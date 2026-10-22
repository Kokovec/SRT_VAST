# Starter Lambda: sign-in flow + waking the instance (see deploy/starter/handler.py).
# Function URL with IAM auth; only CloudFront (via origin access control) can call it.

data "archive_file" "starter" {
  type        = "zip"
  output_path = "${path.module}/.build/starter.zip"
  source {
    filename = "handler.py"
    content  = file("${path.module}/../starter/handler.py")
  }
  source {
    filename = "auth.py" # the same verifier the viewer uses
    content  = file("${path.module}/../../srtvast/auth.py")
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "starter" {
  name_prefix          = "${var.name}-svc-starter-"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_cloudwatch_log_group" "starter" {
  name              = "/aws/lambda/${var.name}-starter"
  retention_in_days = 14
}

data "aws_iam_policy_document" "starter" {
  statement {
    sid       = "StartTheInstance"
    actions   = ["ec2:StartInstances"]
    resources = [aws_instance.host.arn]
  }
  statement {
    sid       = "ReadInstanceState"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"] # no resource-level permissions for Describe
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.starter.arn}:*"]
  }
}

resource "aws_iam_role_policy" "starter" {
  role   = aws_iam_role.starter.id
  policy = data.aws_iam_policy_document.starter.json
}

resource "aws_lambda_function" "starter" {
  function_name    = "${var.name}-starter"
  role             = aws_iam_role.starter.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  filename         = data.archive_file.starter.output_path
  source_code_hash = data.archive_file.starter.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      PUBLIC_HOST     = var.hostname
      COGNITO_DOMAIN  = "${aws_cognito_user_pool_domain.login.domain}.auth.${var.aws_region}.amazoncognito.com"
      CLIENT_ID       = aws_cognito_user_pool_client.viewer.id
      CLIENT_SECRET   = aws_cognito_user_pool_client.viewer.client_secret
      ISSUER          = local.issuer
      INSTANCE_ID     = aws_instance.host.id
      SESSION_MINUTES = tostring(var.session_minutes)
    }
  }

  depends_on = [aws_cloudwatch_log_group.starter, aws_iam_role_policy.starter]
}

resource "aws_lambda_function_url" "starter" {
  function_name      = aws_lambda_function.starter.function_name
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_url" {
  statement_id           = "CloudFrontInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.starter.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.viewer.arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id  = "CloudFrontInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.starter.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.viewer.arn
}
