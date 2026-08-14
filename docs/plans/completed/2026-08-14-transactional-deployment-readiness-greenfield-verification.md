# Transactional Deployment Readiness and Greenfield Verification

> Planned for GitHub issue
> [#24](https://github.com/nsal/meal-planner-bot/issues/24).

## Overview

Close three deployment-readiness gaps around DynamoDB transactions. The Bot
Lambda must receive the exact IAM permission required by
`TransactWriteItems`; the greenfield data assumption must be explicit and
protected by transaction-shape tests; and operators need a repeatable way to
verify the deployed execution role before running a small Telegram smoke test.

The change keeps the existing single-table schema, source-update marker
format, callback and conversational APIs, and deployment topology. It adds no
runtime dependency, table, index, migration, or application environment
variable.

## Context (from discovery)

- Files/components involved: `template.yaml`, `tests/test_template.py`,
  `src/meal_planner/db/dynamo.py`, `tests/test_dynamo.py`, `README.md`, and a
  small deployment-verification script with focused tests.
- `DynamoRepository.log_meal`, `confirm_plan`, and `update_meal_outcome` call
  `TransactWriteItems` from Bot Lambda request paths.
- `BotFunction` currently receives SAM's `DynamoDBCrudPolicy`, whose generated
  action set does not include `dynamodb:TransactWriteItems`.
- Moto exercises transaction behavior but does not enforce the deployed Lambda
  role, so the full local suite can pass while AWS rejects the operation.
- The project is entirely greenfield: no persistent environment has ever
  stored `MEAL#...#UPDATE#...` items without corresponding
  `MEAL_UPDATE#...` markers. A backfill or compatibility lookup is therefore
  unnecessary.
- Existing deployment documentation covers SAM validation, build, deploy, and
  Telegram webhook registration, but not execution-role authorization for
  DynamoDB transactions.

## Development Approach

- **Testing approach**: TDD; add a failing contract test before each code or
  template fix.
- Complete each task fully before moving to the next.
- Make small, focused changes and preserve unrelated worktree edits.
- Every code task must add or update tests for success and failure paths.
- All focused tests must pass before starting the next task.
- Update this plan immediately if implementation scope changes.
- Prefer least-privilege, table-scoped IAM over broader managed policies or
  wildcard resources.
- Do not add migration logic for a state that cannot exist in this greenfield
  project.
- Use `uv` for execution, Ruff at 80 columns, strict mypy, and the existing SAM
  artifact freshness checks.

## Testing Strategy

- **Template tests**: prove Bot Lambda has an explicit, table-scoped
  `dynamodb:TransactWriteItems` statement; reject wildcard resources and avoid
  granting the action to Planner Lambda when it has no transaction call path.
- **Repository tests**: inspect the source-update transaction and prove every
  greenfield source-backed meal atomically includes a stable conditional
  marker independent of date, meal type, description, and timestamp.
- **Verifier unit tests**: mock CloudFormation, Lambda, DynamoDB, and IAM
  clients to cover allowed, denied, malformed-output, and API-error outcomes
  without touching AWS.
- **Deployment tests**: validate and rebuild SAM, then require fresh artifacts
  and current template contents.
- **Manual AWS checks**: after deployment, run the read-only role verifier and
  a Telegram/DynamoDB smoke scenario in a non-production stack.
- **Project gates**: full pytest, Ruff lint and format checks, strict mypy for
  application and verifier code, and `git diff --check`.
- There is no UI suite; Telegram behavior is exercised at the API boundary and
  manually in a test chat after deployment.

## Progress Tracking

- Mark completed items with `[x]` immediately when done.
- Add newly discovered work with a ➕ prefix.
- Record blockers or failed assumptions with a ⚠️ prefix.
- Keep this plan synchronized with implementation and verification results.
- Move the plan to `docs/plans/completed/` only after every local gate passes.

## Solution Overview

Add one explicit inline IAM statement to `BotFunction` allowing
`dynamodb:TransactWriteItems` only against the meal-planner table ARN. Retain
`DynamoDBCrudPolicy` for the existing non-transaction operations and leave
Planner Lambda unchanged under least privilege.

Treat greenfield status as a deliberate architecture constraint. Do not scan,
backfill, or add fallback lookups for markerless source-update records. Instead,
strengthen repository tests so the first source-backed write must contain both
the date-indexed meal and source-only marker in one transaction, making the
invalid state unreachable from this release onward.

Add a read-only deployment verifier that resolves the stack's Bot Lambda role
and DynamoDB table ARN, then calls IAM policy simulation for
`dynamodb:TransactWriteItems`. Unit-test the verifier locally and document its
required caller permissions and limitations. Keep a real Telegram meal-log
smoke test in Post-Completion because it requires deployed AWS and Telegram
resources.

## Technical Details

- Add an inline Bot Lambda policy statement with action
  `dynamodb:TransactWriteItems`, effect `Allow`, and resource
  `!GetAtt MealPlannerTable.Arn`.
- Do not use `Resource: "*"`, broaden Planner Lambda, or replace
  `DynamoDBCrudPolicy`.
- Template tests must inspect the actual source-template policy structure, not
  infer transaction authorization from the name `DynamoDBCrudPolicy`.
- Source-backed meal transactions retain two ordered puts: the date-indexed
  meal and `MEAL_UPDATE#<source_update_id>` marker conditioned with
  `attribute_not_exists(PK)`.
- The marker key must remain independent of LLM-derived values. Calls without
  `source_update_id` retain the timestamp-based non-transaction path.
- No backfill, table scan, migration command, compatibility read, schema/index,
  or marker TTL is introduced because no markerless deployed records exist.
- The verifier is read-only. It resolves stack outputs, reads the Bot Lambda
  execution-role ARN and table ARN, and evaluates
  `dynamodb:TransactWriteItems` through `iam:SimulatePrincipalPolicy`.
- A denied, implicit-deny, missing output, malformed AWS response, or AWS API
  exception must produce a nonzero exit status without exposing credentials.
- Document that IAM simulation requires suitable caller permission and is not
  a substitute for the post-deployment Telegram smoke test.

## What Goes Where

- **Implementation Steps**: template IAM contract, greenfield transaction
  invariant, deployment verifier, operator documentation, SAM rebuild, and
  project verification.
- **Post-Completion**: deploy to a non-production AWS stack, execute the
  read-only role check, exercise Telegram meal logging, inspect DynamoDB, open
  a pull request, and update the GitHub issue.

## Implementation Steps

### Task 1: Grant Bot Lambda least-privilege transaction access

**Files:**

- Modify: `template.yaml`
- Modify: `tests/test_template.py`

- [x] add a failing template test requiring Bot Lambda to allow exactly
  `dynamodb:TransactWriteItems` against `MealPlannerTable.Arn`
- [x] add failure-oriented assertions rejecting wildcard transaction resources
  and accidental Planner Lambda transaction permission
- [x] add the explicit table-scoped inline statement to `BotFunction.Policies`
  while retaining the existing CRUD and planner-invoke policies
- [x] verify the source template grants all actions used by Bot transaction
  paths and no additional DynamoDB resources
- [x] run `uv run pytest tests/test_template.py`; all tests must pass before
  Task 2

### Task 2: Lock in the greenfield marker invariant

**Files:**

- Verify: `src/meal_planner/db/dynamo.py`
- Modify: `tests/test_dynamo.py`

- [x] add a transaction-shape test proving every source-backed meal write
  includes the meal and stable source-only marker in the same transaction
- [x] assert the marker condition and item ordering make marker-only
  cancellation distinguishable from unexpected transaction failures
- [x] retain integration coverage proving retries with changed dates, meal
  types, descriptions, and timestamps preserve only the first meal
- [x] retain coverage for distinct source IDs, timestamp fallback, history
  boundaries, pagination, malformed-item filtering, and legacy non-update meal
  keys
- [x] document in the test or plan why markerless update-key migration is
  intentionally excluded for this greenfield project
- [x] run `uv run pytest tests/test_dynamo.py`; all tests must pass before
  Task 3

### Task 3: Add read-only deployed-role authorization verification

**Files:**

- Create: `scripts/verify_transaction_permission.py`
- Create: `tests/test_verify_transaction_permission.py`

- [x] add a typed CLI accepting stack name and region without reading or
  repurposing broad shell environment variables
- [x] resolve Bot function and table identifiers from CloudFormation outputs,
  then resolve the Lambda role ARN and DynamoDB table ARN
- [x] call `iam:SimulatePrincipalPolicy` for
  `dynamodb:TransactWriteItems` and return success only for an explicit allowed
  decision on the exact table ARN
- [x] handle missing outputs, denied and implicit-deny decisions, malformed AWS
  responses, and `ClientError` with concise credential-safe diagnostics
- [x] write unit tests for successful resolution and allowed authorization
- [x] write unit tests for denied, missing, malformed, and AWS-error paths,
  including a nonzero CLI exit status
- [x] run `uv run pytest tests/test_verify_transaction_permission.py` and
  `uv run mypy scripts/verify_transaction_permission.py`; all checks must pass
  before Task 4

### Task 4: Document and validate the deployment workflow

**Files:**

- Modify: `README.md`
- Verify: `template.yaml`
- Verify: `tests/test_template.py`
- Rebuild: `.aws-sam/build/`

- [x] document the read-only authorization verifier, its required AWS caller
  permissions, expected allowed result, and failure interpretation
- [x] document the greenfield/no-backfill assumption and the stop condition if
  markerless `UPDATE#` records are ever discovered before deployment
- [x] add a concise non-production Telegram/DynamoDB smoke procedure that
  confirms one meal item plus one marker and verifies a replay does not create
  another meal
- [x] run `uvx --from aws-sam-cli sam validate --lint --region us-east-1`
- [x] rebuild with `uvx --from aws-sam-cli sam build --beta-features`
- [x] run
  `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`; all tests must
  pass before Task 5

### Task 5: Verify acceptance criteria and project standards

**Files:**

- Verify: `template.yaml`
- Verify: `src/meal_planner/db/dynamo.py`
- Verify: `scripts/verify_transaction_permission.py`
- Verify: `tests/`
- Verify: `README.md`

- [x] verify Bot Lambda alone has explicit table-scoped transaction permission
  and the generated SAM artifact is current
- [x] verify no migration, scan, schema/index, dependency, or application
  environment-variable change was introduced
- [x] verify source-backed meal writes cannot create markerless records through
  the supported greenfield path
- [x] verify the deployment checker performs read-only authorization inspection
  and fails closed on uncertain responses
- [x] run `uv run pytest` and fix failures until the full suite passes
- [x] run `uv run ruff check .` and fix every finding
- [x] run `uv run ruff format --check .` and fix every formatting difference
- [x] run `uv run mypy` and
  `uv run mypy scripts/verify_transaction_permission.py`; fix every type error
- [x] run `git diff --check` and fix every whitespace error

### Task 6: Finalize plan tracking and documentation

**Files:**

- Modify: `README.md` only for the operator guidance described above
- Modify: `AGENTS.md` only if implementation discovers a reusable project rule
- Modify: this plan with final deviations and verification results
- Move: this plan to `docs/plans/completed/`

- [x] record implementation deviations and final verification results in this
  plan
- [x] update `AGENTS.md` only if a reusable rule not already covered by project
  standards is discovered
- [x] confirm every implementation and verification checkbox is complete
- [x] rerun `uv run pytest` after final documentation changes
- [x] move this plan to `docs/plans/completed/` only after all local checks pass

## Implementation and verification results

- Added an explicit Bot-only, table-scoped `dynamodb:TransactWriteItems`
  statement while retaining the existing CRUD and planner-invoke policies.
- Added source-update transaction-shape coverage for ordered meal and marker
  puts, the marker condition, and marker-only cancellation; no migration or
  compatibility lookup was introduced.
- Added `scripts/verify_transaction_permission.py`, a typed read-only checker
  that resolves CloudFormation outputs and fails closed unless IAM simulation
  explicitly allows the exact table ARN.
- Documented caller permissions, failure interpretation, greenfield stop
  conditions, and the non-production Telegram/DynamoDB smoke procedure.
- `uvx --from aws-sam-cli sam validate --lint --region us-east-1`: passed.
- `uvx --from aws-sam-cli sam build --beta-features`: passed; `.aws-sam/build/`
  is current.
- `REQUIRE_SAM_ARTIFACTS=1 uv run pytest tests/test_template.py`: 21 passed.
- `uv run pytest`: 187 passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run mypy`: passed; `uv run mypy scripts/verify_transaction_permission.py`:
  passed.
- `git diff --check`: passed.
- No reusable project rule was discovered, so `AGENTS.md` was unchanged.
- Manual AWS deployment, role verification, Telegram smoke testing, PR
  creation, and GitHub issue commenting remain post-completion operations
  requiring configured external resources.

## Post-Completion

**Manual verification:**

- Deploy to a dedicated non-production stack from a feature branch through the
  normal SAM workflow; never push or merge directly to `master`.
- Run `uv run python scripts/verify_transaction_permission.py` with the stack
  name and region, and confirm the deployed Bot role is explicitly allowed to
  call `dynamodb:TransactWriteItems` on the stack table.
- In a Telegram test chat, submit one conversational meal log, locate its meal
  and `MEAL_UPDATE#<update_id>` marker in DynamoDB, replay the same update with
  changed extracted fields, and verify no second meal appears.
- Exercise plan confirmation and meal-check-in callbacks to cover the other Bot
  transaction paths under the deployed execution role.

**External system updates:**

- Open a pull request after local verification.
- Run the read-only authorization verifier and Telegram smoke procedure after
  deployment; these require configured AWS and Telegram resources and are not
  local test-suite gates.
- Comment on the associated GitHub issue with the Conventional Commit or pull
  request link and concise local and deployed verification results.
