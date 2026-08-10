
## Engineering standards
# Project Rules: Python Standards

- **Configuration**: Always check for and strictly adhere to project-specific settings defined in `pyproject.toml`.
- **Ruff**: Use `ruff` for all linting, formatting, and auto-fixes. Ensure text formatted at 80 columns.
 Do not use other formatters unless specified.
- **PEP 8**: Ensure all generated code strictly follows PEP 8 standards (naming conventions, line lengths, etc.).
- **Mypy**: Every piece of Python code generated must include type hints.
Run `mypy` to verify static types before completing the task.
- **Verification Rule**: Before finalizing any code changes or marking a task as complete,
run `uv run pytest` and confirm all tests pass. If tests fail, fix the code and re-run until successful.

## Python Environment & Package Management
- **Primary Tool**: Use `uv` for all Python package management, environment creation, and tool execution.
- **Dependency Commands**:
  - Use `uv add <package>` to install new dependencies.
  - Use `uv add --dev <package>` for development tools (like ruff, mypy, pytest).
- **Execution**: Always run scripts and tools using `uv run <command>` (e.g., `uv run pytest`, `uv run ruff check .`).
- **One-off Tools**: Use `uvx` for running tools that aren't project dependencies.
- **Lockfile**: Ensure the `uv.lock` file is updated and in sync after any dependency change.

## GitHub & Version Control
- **Commit Pattern**: Follow the Conventional Commits specification.
Always include the relevant issue number in the commit message (e.g., `fix(db): fix connection timeout (#42)`).
- **Issue Pattern**: After completing work, add a comment to the associated GitHub Issue explaining
what was done and linking to the commit or PR.

## Git Branching & Release Strategy
- **Master Protection**: NEVER push or merge directly to the `master` branch.
All code changes must go to a dedicated feature\bug\etc branch via a Pull Request.
