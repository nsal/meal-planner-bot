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
    loader: yaml.SafeLoader, _tag_suffix: str, node: yaml.nodes.Node
) -> Any:
    """Load an intrinsic value without interpreting its CloudFormation tag."""
    if isinstance(node, yaml.nodes.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.nodes.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


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
    source_globals = source_template["Globals"]["Function"]
    built_globals = built_template["Globals"]["Function"]
    assert built_globals["Runtime"] == source_globals["Runtime"]
    assert built_globals["Architectures"] == source_globals["Architectures"]
    assert (
        built_template["Parameters"].keys()
        == source_template["Parameters"].keys()
    )

    for logical_id in ("BotFunction", "PlannerFunction"):
        source_resource = source_template["Resources"][logical_id]
        built_resource = built_template["Resources"][logical_id]
        assert (
            built_resource["Properties"]["Handler"]
            == source_resource["Properties"]["Handler"]
        )
        assert (
            built_resource["Metadata"]["BuildMethod"]
            == source_resource["Metadata"]["BuildMethod"]
        )

    built_global_variables = built_globals["Environment"]["Variables"]
    for variable_name, parameter_name in (
        ("TELEGRAM_BOT_TOKEN", "TelegramBotTokenSecretName"),
        ("LLM_API_KEY", "LlmApiKeySecretName"),
    ):
        dynamic_reference = built_global_variables[variable_name]
        assert _contains_text(dynamic_reference, "secretsmanager")
        assert _contains_text(dynamic_reference, parameter_name)

    built_bot_variables = built_template["Resources"]["BotFunction"][
        "Properties"
    ]["Environment"]["Variables"]
    webhook_reference = built_bot_variables["TELEGRAM_WEBHOOK_SECRET"]
    assert _contains_text(webhook_reference, "secretsmanager")
    assert _contains_text(webhook_reference, "TelegramWebhookSecretName")


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

    expected_handlers = {
        "BotFunction": "meal_planner.bot_handler.lambda_handler",
        "PlannerFunction": "meal_planner.planner_handler.lambda_handler",
    }
    for logical_id, handler in expected_handlers.items():
        resource = resources[logical_id]
        assert resource["Metadata"]["BuildMethod"] == "python-uv"
        assert resource["Properties"]["CodeUri"] == "./"
        assert resource["Properties"]["Handler"] == handler

    function_globals = template["Globals"]["Function"]
    assert function_globals["Runtime"] == "python3.14"
    assert function_globals["Architectures"] == ["arm64"]
    variables = function_globals["Environment"]["Variables"]
    assert variables["TELEGRAM_REQUEST_TIMEOUT_SECONDS"] == "10"
    assert variables["LLM_REQUEST_TIMEOUT_SECONDS"] == "20"
    assert variables["LLM_MAX_RETRIES"] == "3"
    assert variables["LLM_INITIAL_BACKOFF_SECONDS"] == "1"


def test_secret_inputs_are_secret_names_and_dynamic_references() -> None:
    """Secrets enter Lambda through Secrets Manager references."""
    template = _load_template()
    parameters = template["Parameters"]
    secret_parameters = {
        "TelegramBotTokenSecretName",
        "TelegramWebhookSecretName",
        "LlmApiKeySecretName",
    }

    assert secret_parameters <= parameters.keys()
    assert (
        not {
            "TelegramBotTokenParameter",
            "TelegramWebhookSecretParameter",
            "LlmApiKeyParameter",
        }
        & parameters.keys()
    )
    for parameter_name in secret_parameters:
        assert parameters[parameter_name]["Type"] == "String"
        assert "NoEcho" not in parameters[parameter_name]
        assert "Default" not in parameters[parameter_name]

    variables = template["Globals"]["Function"]["Environment"]["Variables"]
    for variable_name in (
        "TELEGRAM_BOT_TOKEN",
        "LLM_API_KEY",
    ):
        dynamic_reference = variables[variable_name]
        assert isinstance(dynamic_reference, list)
        assert (
            dynamic_reference[0]
            == "{{resolve:secretsmanager:${SecretName}:SecretString}}"
        )

    assert "TELEGRAM_WEBHOOK_SECRET" not in variables
    bot_variables = template["Resources"]["BotFunction"]["Properties"]
    bot_variables = bot_variables["Environment"]["Variables"]
    webhook_reference = bot_variables["TELEGRAM_WEBHOOK_SECRET"]
    assert isinstance(webhook_reference, list)
    assert (
        webhook_reference[0]
        == "{{resolve:secretsmanager:${SecretName}:SecretString}}"
    )


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


@pytest.mark.parametrize(
    ("logical_id", "module_name"),
    [
        ("BotFunction", "meal_planner.bot_handler"),
        ("PlannerFunction", "meal_planner.planner_handler"),
    ],
)
def test_built_artifact_imports_lambda_handler(
    logical_id: str, module_name: str
) -> None:
    """Import each handler from the source copied into the SAM artifact."""
    contract = _deployment_contract()
    compatibility = _host_compatibility(contract)
    if not compatibility.compatible:
        pytest.skip(compatibility.reason)

    artifact = _built_artifact(logical_id)
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

    _import_lambda_handler(artifact, module_name)
