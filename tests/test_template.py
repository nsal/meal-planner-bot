"""Tests for the SAM template and its Python Lambda build artifacts."""

import os
import platform
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "template.yaml"
BUILD_ROOT = PROJECT_ROOT / ".aws-sam" / "build"


class CloudFormationLoader(yaml.SafeLoader):
    """YAML loader that accepts CloudFormation intrinsic tags."""


def _construct_intrinsic(
    loader: yaml.SafeLoader, tag_suffix: str, node: yaml.nodes.Node
) -> Any:
    """Load an intrinsic value without interpreting its CloudFormation tag."""
    if isinstance(node, yaml.nodes.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.nodes.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    intrinsic_name = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    return {intrinsic_name: value}


CloudFormationLoader.add_multi_constructor("!", _construct_intrinsic)


@dataclass(frozen=True)
class DeploymentContract:
    """Runtime values that must match a generated SAM artifact's host."""

    operating_system: str
    architecture: str
    runtime: str
    python_version: tuple[int, int]


@dataclass(frozen=True)
class CompatibilityResult:
    """Result of comparing a host with the deployment contract."""

    compatible: bool
    reason: str = ""


def _load_template() -> dict[str, Any]:
    """Load the SAM template for assertions."""
    with TEMPLATE_PATH.open(encoding="utf-8") as template_file:
        template = yaml.load(template_file, Loader=CloudFormationLoader)
    assert isinstance(template, dict)
    return template


def _load_project_metadata() -> dict[str, Any]:
    """Load project metadata for Python tooling contract assertions."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)
    assert isinstance(project, dict)
    return project


def _deployment_contract() -> DeploymentContract:
    """Derive the artifact contract from the SAM template."""
    template = _load_template()
    function_globals = template["Globals"]["Function"]
    runtime = function_globals["Runtime"]
    architectures = function_globals["Architectures"]
    assert isinstance(runtime, str)
    assert isinstance(architectures, list)
    assert len(architectures) == 1
    architecture = architectures[0]
    assert isinstance(architecture, str)

    version_parts = runtime.removeprefix("python").split(".")
    assert len(version_parts) == 2
    python_version = (int(version_parts[0]), int(version_parts[1]))
    return DeploymentContract(
        operating_system="Linux",
        architecture=architecture,
        runtime=runtime,
        python_version=python_version,
    )


def _artifact_compatibility(
    contract: DeploymentContract,
    *,
    operating_system: str,
    architecture: str,
    python_version: tuple[int, int],
) -> CompatibilityResult:
    """Compare a host's platform values with the deployment contract."""
    if operating_system != contract.operating_system:
        return CompatibilityResult(
            False,
            f"SAM artifact targets {contract.operating_system}, but this host "
            f"is {operating_system}",
        )

    expected_architectures = {
        "arm64": {"aarch64", "arm64"},
        "aarch64": {"aarch64", "arm64"},
    }.get(contract.architecture.lower(), {contract.architecture.lower()})
    normalized_architecture = architecture.lower()
    if normalized_architecture not in expected_architectures:
        return CompatibilityResult(
            False,
            f"SAM artifact targets {contract.architecture}, but this host "
            f"reports {architecture}",
        )

    if python_version != contract.python_version:
        expected_version = ".".join(map(str, contract.python_version))
        actual_version = ".".join(map(str, python_version))
        return CompatibilityResult(
            False,
            f"SAM artifact targets Python {expected_version}, but this host "
            f"runs Python {actual_version}",
        )

    return CompatibilityResult(True)


def _host_compatibility(contract: DeploymentContract) -> CompatibilityResult:
    """Compare the current host with the deployment contract."""
    return _artifact_compatibility(
        contract,
        operating_system=platform.system(),
        architecture=platform.machine(),
        python_version=sys.version_info[:2],
    )


def _built_artifact(logical_id: str) -> Path:
    """Return a function artifact, including SAM's shared-artifact layout."""
    function_artifact = BUILD_ROOT / logical_id
    if function_artifact.is_dir():
        return function_artifact

    shared_artifacts = sorted(BUILD_ROOT.glob("*-Shared"))
    if shared_artifacts:
        return shared_artifacts[0]

    return function_artifact


def _require_artifact(artifact: Path) -> None:
    """Skip or fail when the artifact is absent, based on release mode."""
    if artifact.is_dir():
        return

    message = f"SAM artifact missing at {artifact}; run sam build first"
    if os.environ.get("REQUIRE_SAM_ARTIFACTS") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _contains_text(value: object, needle: str) -> bool:
    """Return whether a nested CloudFormation value contains text."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(
            _contains_text(key, needle) or _contains_text(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_text(item, needle) for item in value)
    return False


def _normalize_build_template(value: object) -> object:
    """Normalize SAM-generated CodeUri and equivalent GetAtt forms."""
    if isinstance(value, dict):
        return {
            key: "<generated CodeUri>"
            if key == "CodeUri"
            else (
                item.split(".", 1)
                if key == "Fn::GetAtt" and isinstance(item, str)
                else _normalize_build_template(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_build_template(item) for item in value]
    return value


def _first_template_difference(
    expected: object, actual: object, path: str = "$"
) -> str | None:
    """Return a path for the first deep template mismatch."""
    if type(expected) is not type(actual):
        return path
    if isinstance(expected, dict) and isinstance(actual, dict):
        keys = list(dict.fromkeys([*expected.keys(), *actual.keys()]))
        for key in keys:
            child_path = f"{path}.{key}"
            if key not in expected or key not in actual:
                return child_path
            difference = _first_template_difference(
                expected[key], actual[key], child_path
            )
            if difference:
                return difference
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return path
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_template_difference(
                left, right, f"{path}[{index}]"
            )
            if difference:
                return difference
        return None
    return None if expected == actual else path


def _assert_built_template_is_current() -> None:
    """Verify the generated template reflects the checked-in template."""
    built_template_path = BUILD_ROOT / "template.yaml"
    assert built_template_path.is_file(), (
        f"SAM build template missing at {built_template_path}"
    )
    with built_template_path.open(encoding="utf-8") as template_file:
        built_template = yaml.load(template_file, Loader=CloudFormationLoader)
    assert isinstance(built_template, dict)

    source_template = _load_template()
    normalized_source = _normalize_build_template(source_template)
    normalized_built = _normalize_build_template(built_template)
    difference = _first_template_difference(normalized_source, normalized_built)
    assert difference is None, f"SAM template differs at {difference}"

    built_globals = built_template["Globals"]["Function"]

    built_global_variables = built_globals["Environment"]["Variables"]
    dynamic_reference = built_global_variables["TELEGRAM_BOT_TOKEN"]
    assert _contains_text(dynamic_reference, "secretsmanager")
    assert _contains_text(dynamic_reference, "AppSecretsSecretName")

    built_plan_chat_variables = built_template["Resources"]["PlanChatFunction"][
        "Properties"
    ]["Environment"]["Variables"]
    dynamic_reference = built_plan_chat_variables["LLM_API_KEY"]
    assert _contains_text(dynamic_reference, "secretsmanager")
    assert _contains_text(dynamic_reference, "AppSecretsSecretName")

    built_bot_variables = built_template["Resources"]["BotFunction"][
        "Properties"
    ]["Environment"]["Variables"]
    webhook_reference = built_bot_variables["TELEGRAM_WEBHOOK_SECRET"]
    assert _contains_text(webhook_reference, "secretsmanager")
    assert _contains_text(webhook_reference, "AppSecretsSecretName")

    assert built_global_variables["SECRET_REFRESH_TOKEN"] == {
        "Ref": "SecretRefreshToken"
    }


def _assert_source_files_match_artifact(artifact: Path) -> None:
    """Verify the artifact contains the current application source files."""
    source_files = [
        (
            PROJECT_ROOT / "meal_planner" / "__init__.py",
            Path("meal_planner") / "__init__.py",
        )
    ]
    source_files.extend(
        (
            source_path,
            Path("src") / source_path.relative_to(PROJECT_ROOT / "src"),
        )
        for source_path in sorted(
            (PROJECT_ROOT / "src" / "meal_planner").rglob("*.py")
        )
    )
    for source_path, artifact_relative_path in source_files:
        artifact_path = artifact / artifact_relative_path
        assert artifact_path.is_file(), artifact_relative_path
        assert artifact_path.read_bytes() == source_path.read_bytes(), (
            f"SAM artifact is stale for {artifact_relative_path}"
        )


def _import_lambda_handler(artifact: Path, module_name: str) -> None:
    """Import a Lambda handler in an isolated subprocess."""
    import_script = (
        "from importlib import import_module; "
        "import sys; "
        "artifact_root, module_name = sys.argv[1:]; "
        "sys.path.insert(0, artifact_root); "
        "module = import_module(module_name); "
        "assert callable(module.lambda_handler)"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            import_script,
            str(artifact.resolve()),
            module_name,
        ],
        cwd=artifact,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"Lambda handler import failed for {module_name}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_lambda_build_configuration() -> None:
    """Both functions build from the root with the Python uv workflow."""
    template = _load_template()
    resources = template["Resources"]

    resource = resources["BotFunction"]
    assert resource["Metadata"]["BuildMethod"] == "python-uv"
    assert resource["Properties"]["CodeUri"] == "./"
    assert resource["Properties"]["Handler"] == (
        "meal_planner.bot_handler.lambda_handler"
    )

    function_globals = template["Globals"]["Function"]
    assert function_globals["Runtime"] == "python3.14"
    assert function_globals["Architectures"] == ["arm64"]
    parameters = template["Parameters"]
    assert parameters["PlanChatLlmModel"]["Default"] == "gpt-5.6-luna"
    assert parameters["PlanChatLlmReasoningEffort"]["Default"] == "high"
    variables = function_globals["Environment"]["Variables"]
    assert "TELEGRAM_REQUEST_TIMEOUT_SECONDS" not in variables
    assert "LLM_REQUEST_TIMEOUT_SECONDS" not in variables
    assert "LLM_MAX_RETRIES" not in variables
    assert "LLM_INITIAL_BACKOFF_SECONDS" not in variables
    bot_variables = resources["BotFunction"]["Properties"]["Environment"][
        "Variables"
    ]
    assert all(not name.startswith("BOT_LLM_") for name in bot_variables)
    plan_chat_variables = resources["PlanChatFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    plan_chat_properties = resources["PlanChatFunction"]["Properties"]
    assert plan_chat_properties["Handler"] == (
        "meal_planner.plan_chat_handler.lambda_handler"
    )
    assert plan_chat_properties["Timeout"] == 310
    assert plan_chat_properties["MemorySize"] == 512
    assert (
        plan_chat_variables["PLAN_CHAT_TELEGRAM_REQUEST_TIMEOUT_SECONDS"]
        == "10"
    )
    assert plan_chat_variables["PLAN_CHAT_LLM_REQUEST_TIMEOUT_SECONDS"] == (
        "240"
    )
    assert "PLAN_CHAT_LLM_MAX_RETRIES" not in plan_chat_variables
    assert "LLM_API_KEY" not in bot_variables
    assert "TELEGRAM_WEBHOOK_SECRET" not in plan_chat_variables
    assert "TELEGRAM_ALLOWED_USER_IDS" not in plan_chat_variables
    assert "PLAN_CHAT_FUNCTION_NAME" in bot_variables
    assert all(
        not name.startswith(("PLANNER_", "CONVERSATIONAL_"))
        for name in (*bot_variables, *plan_chat_variables)
    )


def test_plan_chat_has_no_repair_or_transaction_permissions() -> None:
    """Plan Chat cannot self-invoke or write transactions."""
    template = _load_template()
    plan_chat = template["Resources"]["PlanChatFunction"]
    policies = plan_chat["Properties"]["Policies"]
    statements = [
        statement
        for policy in policies
        if isinstance(policy, dict) and "Statement" in policy
        for statement in policy["Statement"]
    ]

    restricted_actions = [
        action
        for statement in statements
        for action in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
        if action
        in {
            "lambda:InvokeFunction",
            "dynamodb:TransactWriteItems",
        }
    ]
    assert restricted_actions == []


def test_plan_chat_has_exact_table_scoped_dynamodb_permissions() -> None:
    """Plan Chat receives only the DynamoDB operations it performs."""
    template = _load_template()
    plan_chat = template["Resources"]["PlanChatFunction"]
    policies = plan_chat["Properties"]["Policies"]

    assert all(
        not (isinstance(policy, dict) and "DynamoDBCrudPolicy" in policy)
        for policy in policies
    )

    statements = [
        statement
        for policy in policies
        if isinstance(policy, dict)
        for statement in policy.get("Statement", [])
        if isinstance(statement, dict)
    ]
    assert statements == [
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:PutItem",
                "dynamodb:DeleteItem",
            ],
            "Resource": {"Fn::GetAtt": "MealPlannerTable.Arn"},
        }
    ]
    assert all(
        statement["Resource"] == {"Fn::GetAtt": "MealPlannerTable.Arn"}
        for statement in statements
    )
    assert all(
        action
        not in {
            "dynamodb:Scan",
            "dynamodb:UpdateItem",
            "dynamodb:BatchGetItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:TransactGetItems",
            "dynamodb:TransactWriteItems",
        }
        for statement in statements
        for action in statement["Action"]
    )


def test_transaction_permission_is_explicit_and_table_scoped() -> None:
    """Only Bot can explicitly transact against the application table."""
    template = _load_template()
    resources = template["Resources"]

    expected_statement = {
        "Effect": "Allow",
        "Action": "dynamodb:TransactWriteItems",
        "Resource": {"Fn::GetAtt": "MealPlannerTable.Arn"},
    }
    bot_policies = resources["BotFunction"]["Properties"]["Policies"]
    transaction_statements = [
        statement
        for policy in bot_policies
        if isinstance(policy, dict)
        for statement in policy.get("Statement", [])
        if isinstance(statement, dict)
        and statement.get("Action") == "dynamodb:TransactWriteItems"
    ]
    assert transaction_statements == [expected_statement]
    plan_chat_policies = resources["PlanChatFunction"]["Properties"]["Policies"]
    assert all(
        statement.get("Action") != "dynamodb:TransactWriteItems"
        for policy in plan_chat_policies
        if isinstance(policy, dict)
        for statement in policy.get("Statement", [])
        if isinstance(statement, dict)
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("Properties.Timeout", 31),
        ("Properties.MemorySize", 257),
        ("Properties.Environment.Variables.EXTRA", "stale"),
        ("Properties.Events", {}),
        ("Properties.Policies", []),
    ],
)
def test_template_difference_helper_reports_deploy_fields(
    path: str, value: object
) -> None:
    source: dict[str, Any] = {
        "Resources": {
            "Function": {
                "Properties": {
                    "CodeUri": "./",
                    "Timeout": 30,
                    "MemorySize": 256,
                    "Environment": {"Variables": {}},
                    "Events": {"Webhook": {"Type": "HttpApi"}},
                    "Policies": [{"Allow": "read"}],
                }
            }
        }
    }
    built = yaml.safe_load(yaml.safe_dump(source))
    target: dict[str, Any] = built["Resources"]["Function"]
    if path == "Properties.Environment.Variables.EXTRA":
        target["Properties"]["Environment"]["Variables"]["EXTRA"] = value
    else:
        target["Properties"][path.removeprefix("Properties.")] = value
    assert (
        _first_template_difference(
            _normalize_build_template(source), _normalize_build_template(built)
        )
        is not None
    )


def test_template_normalization_allows_generated_code_uri_only() -> None:
    source = {"Resources": {"Function": {"Properties": {"CodeUri": "./"}}}}
    built = {"Resources": {"Function": {"Properties": {"CodeUri": "Fn"}}}}
    assert (
        _first_template_difference(
            _normalize_build_template(source), _normalize_build_template(built)
        )
        is None
    )


def test_secret_inputs_are_secret_names_and_dynamic_references() -> None:
    """Secrets enter Lambda through Secrets Manager references."""
    template = _load_template()
    parameters = template["Parameters"]
    assert parameters["SecretRefreshToken"]["Type"] == "String"
    assert "Default" not in parameters["SecretRefreshToken"]

    assert "AppSecretsSecretName" in parameters
    secret_name_parameters = {
        name for name in parameters if name.endswith("SecretName")
    }
    assert secret_name_parameters == {"AppSecretsSecretName"}
    assert (
        not {
            "TelegramBotTokenSecretName",
            "TelegramWebhookSecretName",
            "LlmApiKeySecretName",
        }
        & parameters.keys()
    )
    assert (
        not {
            "TelegramBotTokenParameter",
            "TelegramWebhookSecretParameter",
            "LlmApiKeyParameter",
        }
        & parameters.keys()
    )
    assert parameters["AppSecretsSecretName"]["Type"] == "String"
    assert "NoEcho" not in parameters["AppSecretsSecretName"]
    assert "Default" not in parameters["AppSecretsSecretName"]

    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert variables["SECRET_REFRESH_TOKEN"] == {"Ref": "SecretRefreshToken"}
    dynamic_reference = variables["TELEGRAM_BOT_TOKEN"]
    assert isinstance(dynamic_reference, dict)
    dynamic_reference = dynamic_reference["Fn::Sub"]
    assert isinstance(dynamic_reference, list)
    assert dynamic_reference[0].endswith(":telegram_bot_token}}")
    assert dynamic_reference[1] == {
        "SecretName": {"Ref": "AppSecretsSecretName"}
    }

    plan_chat_variables = template["Resources"]["PlanChatFunction"][
        "Properties"
    ]["Environment"]["Variables"]
    for variable_name, json_key in (("LLM_API_KEY", "llm_api_key"),):
        dynamic_reference = plan_chat_variables[variable_name]
        assert isinstance(dynamic_reference, dict)
        dynamic_reference = dynamic_reference["Fn::Sub"]
        assert isinstance(dynamic_reference, list)
        assert (
            dynamic_reference[0]
            == "{{resolve:secretsmanager:${SecretName}:SecretString:"
            + json_key
            + "}}"
        )
        assert dynamic_reference[1] == {
            "SecretName": {"Ref": "AppSecretsSecretName"}
        }

    assert "TELEGRAM_WEBHOOK_SECRET" not in variables
    bot_variables = template["Resources"]["BotFunction"]["Properties"]
    bot_variables = bot_variables["Environment"]["Variables"]
    webhook_reference = bot_variables["TELEGRAM_WEBHOOK_SECRET"]
    assert isinstance(webhook_reference, dict)
    webhook_reference = webhook_reference["Fn::Sub"]
    assert isinstance(webhook_reference, list)
    assert (
        webhook_reference[0]
        == "{{resolve:secretsmanager:${SecretName}:SecretString:"
        "telegram_webhook_secret}}"
    )
    assert webhook_reference[1] == {
        "SecretName": {"Ref": "AppSecretsSecretName"}
    }


def test_secret_refresh_updates_both_lambda_configurations() -> None:
    """Both functions use the deployment refresh marker."""
    template = _load_template()
    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    assert variables["SECRET_REFRESH_TOKEN"] == {"Ref": "SecretRefreshToken"}


def test_secret_values_are_not_runtime_retrieved_or_whole_secret_injected() -> (
    None
):
    """The template resolves fields during deployment without runtime access."""
    template = _load_template()
    serialized_template = repr(template)
    assert "GetSecretValue" not in serialized_template
    assert "secretsmanager:GetSecretValue" not in serialized_template

    global_variables = template["Globals"]["Function"]["Environment"][
        "Variables"
    ]
    bot_variables = template["Resources"]["BotFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    for variable_name, json_key, function_variables in (
        ("TELEGRAM_BOT_TOKEN", "telegram_bot_token", global_variables),
        ("TELEGRAM_WEBHOOK_SECRET", "telegram_webhook_secret", bot_variables),
        (
            "LLM_API_KEY",
            "llm_api_key",
            template["Resources"]["PlanChatFunction"]["Properties"][
                "Environment"
            ]["Variables"],
        ),
    ):
        reference = function_variables[variable_name]
        assert isinstance(reference, dict)
        substitution = reference["Fn::Sub"]
        assert isinstance(substitution, list)
        assert substitution[0].endswith(f":{json_key}}}}}")
        assert ":SecretString}}" not in substitution[0]


def test_telegram_allowlist_is_required_and_bot_scoped() -> None:
    """Only BotFunction receives a validated explicit Telegram allowlist."""
    template = _load_template()
    parameter = template["Parameters"]["TelegramAllowedUserIds"]
    assert parameter["Type"] == "String"
    assert "Default" not in parameter
    assert parameter["AllowedPattern"] == ("^[1-9][0-9]*(,[1-9][0-9]*)*$")

    globals_variables = template["Globals"]["Function"]["Environment"][
        "Variables"
    ]
    assert "TELEGRAM_ALLOWED_USER_IDS" not in globals_variables
    bot_variables = template["Resources"]["BotFunction"]["Properties"]
    bot_variables = bot_variables["Environment"]["Variables"]
    assert bot_variables["TELEGRAM_ALLOWED_USER_IDS"] == {
        "Ref": "TelegramAllowedUserIds"
    }
    plan_chat_variables = template["Resources"]["PlanChatFunction"][
        "Properties"
    ]["Environment"]["Variables"]
    assert "TELEGRAM_ALLOWED_USER_IDS" not in plan_chat_variables


def test_python_314_project_contract() -> None:
    """Project metadata and static analysis target the deployment Python."""
    project = _load_project_metadata()
    assert project["project"]["requires-python"] == ">=3.14,<3.15"
    assert project["tool"]["ruff"]["target-version"] == "py314"
    assert project["tool"]["mypy"]["python_version"] == "3.14"


def test_root_build_context_has_one_locked_dependency_source() -> None:
    """The root CodeUri contains the source and the uv dependency manifests."""
    assert (PROJECT_ROOT / "pyproject.toml").is_file()
    assert (PROJECT_ROOT / "uv.lock").is_file()
    assert (PROJECT_ROOT / "src" / "meal_planner").is_dir()
    assert not (PROJECT_ROOT / "requirements.txt").exists()
    assert not (PROJECT_ROOT / "requirements-dev.txt").exists()


@pytest.mark.parametrize(
    ("operating_system", "architecture", "python_version"),
    [
        ("Linux", "aarch64", (3, 14)),
        ("Linux", "arm64", (3, 14)),
    ],
)
def test_matching_host_is_compatible(
    operating_system: str,
    architecture: str,
    python_version: tuple[int, int],
) -> None:
    """Linux ARM64 Python 3.14 hosts can import the artifact."""
    result = _artifact_compatibility(
        _deployment_contract(),
        operating_system=operating_system,
        architecture=architecture,
        python_version=python_version,
    )
    assert result.compatible
    assert not result.reason


@pytest.mark.parametrize(
    ("operating_system", "architecture", "python_version", "reason"),
    [
        ("Darwin", "arm64", (3, 14), "targets Linux"),
        ("Linux", "x86_64", (3, 14), "targets arm64"),
        ("Linux", "aarch64", (3, 13), "targets Python 3.14"),
    ],
)
def test_mismatching_host_is_incompatible(
    operating_system: str,
    architecture: str,
    python_version: tuple[int, int],
    reason: str,
) -> None:
    """Operating system, architecture, and Python mismatches are explicit."""
    result = _artifact_compatibility(
        _deployment_contract(),
        operating_system=operating_system,
        architecture=architecture,
        python_version=python_version,
    )
    assert not result.compatible
    assert reason in result.reason


def test_missing_artifact_is_skipped_in_optional_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ordinary test runs skip when a generated artifact is absent."""
    monkeypatch.delenv("REQUIRE_SAM_ARTIFACTS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="SAM artifact missing"):
        _require_artifact(tmp_path / "missing-artifact")


def test_missing_artifact_fails_in_required_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Release verification fails when a generated artifact is absent."""
    monkeypatch.setenv("REQUIRE_SAM_ARTIFACTS", "1")
    with pytest.raises(pytest.fail.Exception, match="SAM artifact missing"):
        _require_artifact(tmp_path / "missing-artifact")


def test_artifact_import_failure_preserves_subprocess_diagnostics(
    tmp_path: Path,
) -> None:
    """Import failures include both subprocess output streams."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "broken_handler.py").write_text(
        "print('stdout diagnostic')\nraise RuntimeError('stderr diagnostic')\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError) as error:
        _import_lambda_handler(artifact, "broken_handler")

    message = str(error.value)
    assert "stdout diagnostic" in message
    assert "stderr diagnostic" in message


def test_built_artifact_imports_lambda_handler() -> None:
    """Import each handler from the source copied into the SAM artifact."""
    contract = _deployment_contract()
    compatibility = _host_compatibility(contract)
    if not compatibility.compatible:
        pytest.skip(compatibility.reason)

    artifact = _built_artifact("BotFunction")
    _require_artifact(artifact)
    _assert_built_template_is_current()
    _assert_source_files_match_artifact(artifact)

    for dependency in (
        "aiogram",
        "boto3",
        "litellm",
        "pydantic",
        "pydantic_settings",
    ):
        assert (artifact / dependency).exists(), dependency
    assert (artifact / "meal_planner" / "__init__.py").is_file()

    _import_lambda_handler(artifact, "meal_planner.bot_handler")
