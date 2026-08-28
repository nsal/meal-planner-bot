# Plan Chat Migration Pre-work

Run this pre-work before executing the conversational-draft simplification
plan. It prepares local SAM artifacts, verifies the repository, and captures a
read-only AWS baseline. It does not deploy, clean up stack resources, or read
or modify DynamoDB records.

## Local preparation

From the repository root, run:

```bash
uv run python scripts/prepare_simplification.py
```

The command stops at the first failure and runs these stages in order:

1. `uvx --from aws-sam-cli sam build --beta-features`
2. `uv run pytest`
3. `uv run ruff check .`
4. `uv run ruff format --check .`
5. `uv run mypy`

The SAM build is local and does not require AWS credentials. Generated files
under `.aws-sam/` are ignored by Git.

## Read-only AWS baseline

The baseline command requires AWS credentials with these read permissions:

- `cloudformation:DescribeStacks`
- `cloudformation:ListStackResources`
- `lambda:GetFunctionConfiguration`
- `dynamodb:DescribeTable`

Choose an output path that does not already exist, then run:

```bash
uv run python scripts/capture_migration_baseline.py \
  --stack-name "$STACK_NAME" \
  --profile meal-planner \
  --region eu-west-1 \
  --output /tmp/meal-planner-plan-chat-baseline.json
```

The output contains stack and resource identifiers, Lambda role ARNs, Lambda
environment-variable names, and DynamoDB table identity. It excludes stack
output values, Lambda environment values, table records, and table item
counts. Treat the file as internal deployment metadata.

The command never invokes DynamoDB item operations or AWS mutation APIs. It
does not delete the legacy Planner Lambda because CloudFormation owns that
resource. It also leaves historical plan, grocery, batch, repair, and revision
records untouched.
