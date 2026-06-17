"""Per-user state location, the project registry, and repo -> project resolution.

All DevlogSketcher state lives under the XDG data dir (never inside a target repo
and never inside this tool's own repo). A single ``registry.json`` maps each
registered repo to a stable project folder; the repo is identified at runtime by
the git root of the current directory (or an explicit ``--repo`` path).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class DevlogError(Exception):
    """User-facing error; the CLI prints its message without a traceback."""


# --- base directories (XDG) ------------------------------------------------

def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "devlogsketcher"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "devlogsketcher"


def registry_path() -> Path:
    return data_dir() / "registry.json"


def projects_root() -> Path:
    return data_dir() / "projects"


# --- git ------------------------------------------------------------------

def git_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Absolute path to the git work-tree root containing ``start`` (cwd default)."""
    cwd = Path(start).expanduser().resolve() if start else Path.cwd()
    if not cwd.exists():
        raise DevlogError(f"path does not exist: {cwd}")
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except FileNotFoundError:
        raise DevlogError("git is not installed or not on PATH")
    except subprocess.CalledProcessError:
        raise DevlogError(f"not inside a git repository: {cwd}")
    return Path(out).resolve()


# --- registry -------------------------------------------------------------

@dataclass
class Project:
    id: str
    name: str
    repo_path: str
    created_at: str

    @property
    def dir(self) -> Path:
        return projects_root() / self.id

    @property
    def db_path(self) -> Path:
        return self.dir / "store.db"

    @property
    def templates_dir(self) -> Path:
        return self.dir / "templates"


def load_registry() -> dict[str, Project]:
    path = registry_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {
        pid: Project(id=pid, **entry)
        for pid, entry in raw.get("projects", {}).items()
    }


def save_registry(projects: dict[str, Project]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "projects": {
            p.id: {"name": p.name, "repo_path": p.repo_path, "created_at": p.created_at}
            for p in projects.values()
        }
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-")
    return slug or "repo"


def _short_hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:6]


def new_project_id(name: str, repo_path: str) -> str:
    """Stable, human-skimmable folder name; never renamed once created."""
    return f"{_slug(name)}-{_short_hash(repo_path + str(time.time()))}"


def find_by_repo(projects: dict[str, Project], repo_path: str) -> Project | None:
    target = str(Path(repo_path).resolve())
    for p in projects.values():
        if str(Path(p.repo_path).resolve()) == target:
            return p
    return None


def find_by_name_or_id(projects: dict[str, Project], needle: str) -> Project | None:
    if needle in projects:
        return projects[needle]
    matches = [p for p in projects.values() if p.name == needle]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DevlogError(
            f"'{needle}' matches multiple projects; use the project id instead "
            f"({', '.join(m.id for m in matches)})"
        )
    return None


def resolve_current_project(repo: str | None = None) -> Project:
    """Project for the current repo, or a clear error telling the user to init."""
    root = git_root(repo)
    projects = load_registry()
    found = find_by_repo(projects, str(root))
    if found is None:
        raise DevlogError(
            f"repo is not linked: {root}\n"
            f"Run `devlog init` inside it first."
        )
    return found
