# Plan Chat migration pre-work

Run this pre-work before deploying the conversational Plan Chat workflow. It
prepares local SAM artifacts, verifies the repository, and captures a
read-only AWS baseline. It does not deploy, scan DynamoDB, or delete records.

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

The baseline command requires these read permissions:

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
environment-variable names, and DynamoDB table identity. It excludes output
values, environment values, table records, and item counts. Treat the file as
internal deployment metadata.

The command never invokes DynamoDB item operations or AWS mutation APIs. It
does not remove resources owned by CloudFormation, and it leaves existing
DynamoDB records untouched. Historical completed plan documents remain under
`docs/plans/completed/` and are not active deployment instructions.
