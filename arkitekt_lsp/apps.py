"""The apps in a workspace, found by reading its files: choosing one must not import any of them."""

import os
import re
from pathlib import Path
from typing import Any

DECLARES_APP = re.compile(r"^[^#\n]*\b(?:Qt)?App\s*(?:\[[^\]\n]*\])?\s*\(", re.MULTILINE)
_ASSIGNS_APP = re.compile(r"^(\w+)\s*(?::[^=\n]*)?=\s*(?:\w+\.)*(?:Qt)?App\s*(?:\[[^\]\n]*\])?\s*\(", re.MULTILINE)
_SKIPPED = {"node_modules", "__pycache__", "venv", "site-packages"}
_LIMIT = 200
"""A client picks from these by hand, and a workspace can be a whole home directory."""


def project_root(path: Path, folders: list[Path]) -> Path:
    """The nearest directory of ``path`` with a ``pyproject.toml``, within a workspace folder."""
    folder = next((folder for folder in folders if path.is_relative_to(folder)), None)
    for directory in path.parents:
        if (directory / "pyproject.toml").exists():
            return directory
        if folder is not None and directory == folder:
            return folder
    return folder or path.parent


def names_file(target: str, relative: str) -> bool:
    """Whether a ``module[:attr]`` target, as ``arkitekt check`` takes it, means this project file."""
    location = target.rpartition(":")[0] or target
    return location.removesuffix(".py").replace(".", "/") == relative.removesuffix(".py")


def targets_of(text: str, relative: str) -> list[tuple[str, int]]:
    """The target for each app in a file, with its line: the attribute only where the CLI needs it."""
    assigned = list(_ASSIGNS_APP.finditer(text))
    if len(assigned) > 1:
        return [(f"{relative}:{match[1]}", text.count("\n", 0, match.start())) for match in assigned]
    first = assigned[0] if assigned else DECLARES_APP.search(text)
    return [(relative, text.count("\n", 0, first.start()))] if first else []


def find_apps(folders: list[Path]) -> list[dict[str, Any]]:
    """Every app in ``folders``, in path order, up to a limit. A path no module can be named for is left out."""
    found: list[dict[str, Any]] = []
    for folder in folders:
        for directory, subdirectories, files in os.walk(folder):
            subdirectories[:] = sorted(
                name for name in subdirectories if not name.startswith(".") and name not in _SKIPPED
            )
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = Path(directory) / name
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                root = project_root(path, folders)
                relative = path.relative_to(root)
                if not all(part.isidentifier() for part in relative.with_suffix("").parts):
                    continue
                for target, line in targets_of(text, relative.as_posix()):
                    found.append({"target": target, "file": str(path), "line": line, "project": str(root)})
                    if len(found) >= _LIMIT:
                        return found
    return found
