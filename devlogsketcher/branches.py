"""Per-branch planner instructions.

A small per-project map of ``branch name -> extra instruction`` that steers how the
planner frames ideas reviewed from that branch — e.g. a production branch should be
written up as shipped and available, while a dev branch should read as work in
progress. The note is injected into the planner prompt via the ``{{branch_context}}``
placeholder. Stored as ``branches.json`` in the project dir, alongside templates and
the custom planner prompt.
"""

from __future__ import annotations

import json

from .paths import Project


def branch_notes_path(project: Project):
    return project.dir / "branches.json"


def load_branch_notes(project: Project) -> dict[str, str]:
    path = branch_notes_path(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return {k: str(v) for k, v in data.items() if str(v).strip()}


def get_branch_note(project: Project, branch: str) -> str:
    return load_branch_notes(project).get(branch, "")


def set_branch_note(project: Project, branch: str, note: str) -> None:
    notes = load_branch_notes(project)
    if note and note.strip():
        notes[branch] = note.strip()
    else:
        notes.pop(branch, None)
    project.dir.mkdir(parents=True, exist_ok=True)
    path = branch_notes_path(project)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(notes, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def delete_branch_note(project: Project, branch: str) -> bool:
    notes = load_branch_notes(project)
    if branch not in notes:
        return False
    set_branch_note(project, branch, "")
    return True
