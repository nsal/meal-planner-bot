"""Expose the src-layout package from a repository-root SAM artifact."""

from pathlib import Path

_SOURCE_PACKAGE = Path(__file__).parent.parent / "src" / "meal_planner"
__path__ = [str(_SOURCE_PACKAGE)]
