"""On-demand deep research for a single entry.

The planner decides *what* to write about; this step does the codebase research —
reading the relevant files to confirm technical details and expand a thin pitch into
a detailed, accurate outline. It runs lazily (user picks an entry), not for every
idea every run, to keep cost and latency down.

The Claude Agent SDK subagent (with file tools scoped to the repo) is not wired yet.
v1 stubs the outline so the `research` command and the `suggested -> researched`
status transition are exercisable end-to-end.
"""

from __future__ import annotations

from .db import Entry, Store
from .paths import Project


def build_research_prompt(entry: Entry, repo_path: str) -> str:
    return f"""Research one post idea against the codebase and produce a detailed
outline. You have read-only file tools scoped to: {repo_path}

Post idea:
  Title:    {entry.title}
  Audience: {entry.audience}
  Summary:  {entry.summary}
  Source refs: {", ".join(entry.source_refs) or "(none)"}

Read the relevant files/commits, confirm the technical specifics, and return a
detailed outline: a working headline, the key sections/beats, concrete details worth
mentioning (file/feature names, before/after, numbers), and any caveats. Outline
only — do not write the post prose.
"""


def research_entry(
    store: Store,
    project: Project,
    entry_id: int,
    backend: str = "stub",
) -> Entry:
    entry = store.get_entry(entry_id)
    if entry is None:
        raise ValueError(f"no entry #{entry_id}")

    if backend == "stub":
        outline = (
            "[STUB OUTLINE] The research agent is not wired yet.\n\n"
            f"Prompt that would be sent to the codebase-research subagent:\n\n"
            f"{build_research_prompt(entry, project.repo_path)}"
        )
    else:
        raise NotImplementedError(
            f"research backend '{backend}' is not wired yet (only 'stub' in v1)"
        )

    store.update_entry(entry_id, outline=outline, status="researched")
    return store.get_entry(entry_id)
