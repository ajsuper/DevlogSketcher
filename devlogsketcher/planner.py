"""The planner: review the repo digest + existing post database, then propose or
update outlined post ideas.

Design contract (kept stable while the model backend is tuned):
  inputs  -> repo digest, all existing entries (for semantic dedup / merge), the
             project's audience templates
  outputs -> a list of Proposal actions: create a new entry, or update/merge an
             existing one as a feature matures.

The Claude/Opus backend is not wired yet. v1 ships a transparent HEURISTIC stub so
the full pipeline (digest -> proposals -> DB -> list/show) runs end-to-end. Swap in
``ClaudeBackend`` later without touching the CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .db import Entry, Store
from .digest import Digest, render_digest
from .llm import MODEL, first_text, get_client
from .paths import DevlogError, Project
from .templates import Template

# A progress sink: called with short status lines as planning proceeds. No-op default.
Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass
class Proposal:
    """One planner action. ``entry_id=None`` means create; otherwise update/merge."""
    action: str            # "create" | "update"
    audience: str
    title: str
    summary: str
    source_refs: list[str]
    entry_id: int | None = None
    note: str = ""


# The editable planner instruction template. The ``{{...}}`` tokens are filled with
# live data at run time (see ``build_planner_prompt``); everything else is prose the
# user can tune per project (`devlog prompt --edit`, or the web app).
PLANNER_PLACEHOLDERS = ("audiences", "branch_context", "templates", "existing", "digest")

DEFAULT_PLANNER_PROMPT = """\
You plan devlog/social posts for a software project. You DO NOT write
posts — you decide what is worth writing about and produce a tight pitch + the seed
of an outline. A separate research step deepens the outline later.

Audiences available: {{audiences}}

# Branch context
{{branch_context}}

For each idea, choose the best-fit audience template below and match its tone/scope.

# Audience templates
{{templates}}

# Existing post database — DO NOT DUPLICATE these by MEANING, not just wording
Each entry is shown as: [#id] (audience/status) title — summary.
{{existing}}

How the status of an existing entry constrains you:
- suggested / researched / in_progress — already on the list. Don't re-propose it.
  If newer commits clearly advance the same story, return action "update" with its #id
  instead of creating a near-duplicate.
- published — already written and shipped. Never propose it again.
- rejected — the user deliberately declined this idea. Do NOT bring it back, even
  reworded, unless genuinely new work changes the story materially.
- stale — superseded by later changes; a good candidate to UPDATE/refresh.

Rules:
- Before proposing anything new, check it against every entry above. When in doubt
  about whether something is a duplicate, prefer UPDATE over CREATE.
- A story may span many commits over weeks — group related commits into one idea.
- Ignore changes with no story (pure chores) unless they add up to something.

# Recent repository activity
{{digest}}

Return a list of proposals: for each, action (create|update), target entry id (if
update), audience, a one-line title, a 1-2 sentence summary pitch, and the commit
SHAs / file paths that motivate it.
"""


def planner_prompt_path(project: Project) -> Path:
    return project.dir / "planner.md"


def planner_prompt_is_custom(project: Project) -> bool:
    return planner_prompt_path(project).exists()


def load_planner_prompt_template(project: Project) -> str:
    """The project's planner prompt template — its saved override, or the default."""
    path = planner_prompt_path(project)
    if path.exists():
        return path.read_text()
    return DEFAULT_PLANNER_PROMPT


def save_planner_prompt_template(project: Project, text: str) -> None:
    if "{{digest}}" not in text:
        raise DevlogError(
            "planner prompt must keep the {{digest}} placeholder — it's where the "
            "recent repository activity gets inserted."
        )
    project.dir.mkdir(parents=True, exist_ok=True)
    planner_prompt_path(project).write_text(text)


def reset_planner_prompt_template(project: Project) -> bool:
    """Drop the override so the default applies again. False if none was set."""
    path = planner_prompt_path(project)
    if path.exists():
        path.unlink()
        return True
    return False


def _branch_context(digest: Digest, branch_note: str) -> str:
    branch = digest.branch or "(current)"
    lines = [f"You are reviewing the `{branch}` branch."]
    if branch_note and branch_note.strip():
        lines.append("Branch-specific framing for this branch:")
        lines.append(branch_note.strip())
    return "\n".join(lines)


def build_planner_prompt(
    digest: Digest,
    existing: list[Entry],
    templates: list[Template],
    template_text: str | None = None,
    branch_note: str = "",
) -> str:
    """Assemble the planner's instruction text by filling the (possibly customized)
    template with live data. Used by the real backend; also handy to eyeball with
    `devlog run --show-prompt`."""
    template_text = template_text if template_text is not None else DEFAULT_PLANNER_PROMPT
    audiences = ", ".join(t.audience for t in templates) or "(none defined)"
    existing_lines = [
        f"  [#{e.id}] ({e.audience}/{e.status}) {e.title} — {e.summary}"
        for e in existing
    ] or ["  (none yet)"]
    template_blocks = (
        "\n\n".join(f"### {t.audience}\n{t.body}" for t in templates) or "(none defined)"
    )
    return (
        template_text
        .replace("{{audiences}}", audiences)
        .replace("{{branch_context}}", _branch_context(digest, branch_note))
        .replace("{{templates}}", template_blocks)
        .replace("{{existing}}", "\n".join(existing_lines))
        .replace("{{digest}}", render_digest(digest))
    )


# --- backends -------------------------------------------------------------

def heuristic_proposals(
    digest: Digest, existing: list[Entry], templates: list[Template]
) -> list[Proposal]:
    """Transparent placeholder so the pipeline runs without a model.

    Emits a single 'review window' entry for the first audience, listing the window's
    commits as source refs. Clearly NOT real planning — it exists to exercise the DB
    and CLI until the Claude backend lands.
    """
    if not digest.commits:
        return []
    audience = templates[0].audience if templates else "general"
    refs = [c.short_sha for c in digest.commits]
    title = f"[STUB] Review last {digest.window_days}d: {digest.num_commits} commits"
    summary = (
        "Placeholder proposal from the heuristic backend. Wire up the Claude planner "
        "to turn these commits into real, deduped post ideas."
    )
    return [Proposal("create", audience, title, summary, refs, note="heuristic-stub")]


# JSON Schema the model must fill (structured outputs). Kept simple: only the
# constructs supported by output_config.format (basic types, enum, null).
_PROPOSALS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update"]},
                    "entry_id": {"type": ["integer", "null"]},
                    "audience": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action", "entry_id", "audience", "title", "summary", "source_refs"],
            },
        }
    },
    "required": ["proposals"],
}

_PLANNER_SYSTEM = (
    "You plan devlog and social posts for a software project from its git history. "
    "You decide what is worth writing about — you never write the post itself. "
    "Deduplicate against existing entries by MEANING, not wording; prefer updating a "
    "maturing entry over creating a near-duplicate. Group related commits that tell "
    "one story. Skip pure chores with no narrative."
)

# Appended to the system prompt only when the user sets a target (> 0). Quality is
# explicitly prioritized over hitting the number so the count never invents filler.
_TARGET_CLAUSE = (
    " Target: aim to generate {n} new entr{y} this run. If you do not have enough "
    "material to reach {n}, or the additional ideas would duplicate existing entries "
    "or each other, it is okay to fall short. Always prefer fewer high-quality, "
    "non-duplicate entries over meeting the quota."
)


def planner_system(target: int = 0) -> str:
    """The planner system prompt, with the target instruction appended when set."""
    if target and target > 0:
        return _PLANNER_SYSTEM + _TARGET_CLAUSE.format(
            n=target, y="y" if target == 1 else "ies")
    return _PLANNER_SYSTEM


def _stream_proposals(client, *, progress: Progress, **kwargs):
    """Run the planner call in streaming mode, surfacing the model's reasoning as it
    arrives, and return the final message. The planner is a single structured-output
    call (no tools), so the only live signal is the thinking stream — we emit it a
    line at a time so a long run shows continuous progress instead of a dead wait."""
    buf: list[str] = []

    def flush() -> None:
        for line in "".join(buf).splitlines():
            line = line.strip()
            if line:
                progress(f"  💭 {line}")
        buf.clear()

    composing = False
    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            if event.type == "content_block_delta":
                if event.delta.type == "thinking_delta":
                    buf.append(event.delta.thinking)
                    if "\n" in event.delta.thinking:
                        flush()
                elif event.delta.type == "text_delta" and not composing:
                    # First output token: thinking is done, JSON is being written.
                    composing = True
                    flush()
                    progress("Composing proposals…")
            elif event.type == "content_block_stop":
                flush()
        return stream.get_final_message()


def claude_proposals(
    digest: Digest,
    existing: list[Entry],
    templates: list[Template],
    prompt_template: str | None = None,
    branch_note: str = "",
    target: int = 0,
    progress: Progress = _noop,
) -> list[Proposal]:
    client = get_client()
    message = _stream_proposals(
        client,
        progress=progress,
        model=MODEL,
        max_tokens=16000,
        # display "summarized" so the thinking stream carries readable text — on
        # Opus 4.8 the default is "omitted", which streams empty thinking blocks.
        thinking={"type": "adaptive", "display": "summarized"},
        system=planner_system(target),
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": _PROPOSALS_SCHEMA},
        },
        messages=[{"role": "user",
                   "content": build_planner_prompt(
                       digest, existing, templates, prompt_template, branch_note)}],
    )
    data = json.loads(first_text(message))
    valid_audiences = {t.audience for t in templates}
    proposals: list[Proposal] = []
    for p in data.get("proposals", []):
        audience = p["audience"] if p["audience"] in valid_audiences else (
            templates[0].audience if templates else p["audience"]
        )
        proposals.append(Proposal(
            action=p["action"], audience=audience, title=p["title"],
            summary=p["summary"], source_refs=list(p.get("source_refs", [])),
            entry_id=p.get("entry_id"),
        ))
    return proposals


def plan(
    digest: Digest,
    store: Store,
    templates: list[Template],
    backend: str = "claude",
    prompt_template: str | None = None,
    branch_note: str = "",
    target: int = 0,
    progress: Progress = _noop,
) -> list[Proposal]:
    existing = store.list_entries()
    if backend == "claude":
        return claude_proposals(digest, existing, templates, prompt_template,
                                branch_note, target, progress)
    if backend == "heuristic":
        return heuristic_proposals(digest, existing, templates)
    raise NotImplementedError(f"unknown planner backend '{backend}'")


def apply_proposals(
    store: Store, proposals: list[Proposal], run_id: int, branch: str = ""
) -> list[tuple[int, str]]:
    """Persist proposals; returns (entry_id, "created"|"updated") per proposal.

    The reviewed ``branch`` is stamped as provenance on created entries and refreshed
    on updated ones, so an idea that started on a dev branch reflects the branch it
    most recently matured on (e.g. main, once it ships)."""
    branch_kw = {"branch": branch} if branch else {}
    results: list[tuple[int, str]] = []
    for p in proposals:
        # Only honor an update if the target entry actually exists; otherwise the
        # planner referenced a stale/hallucinated id — fall back to creating one.
        if p.action == "update" and p.entry_id is not None and store.get_entry(p.entry_id):
            store.update_entry(
                p.entry_id, title=p.title, summary=p.summary,
                source_refs=p.source_refs, run_id=run_id, **branch_kw,
            )
            results.append((p.entry_id, "updated"))
        else:
            eid = store.add_entry(
                audience=p.audience, title=p.title, summary=p.summary,
                source_refs=p.source_refs, run_id=run_id, branch=branch,
            )
            results.append((eid, "created"))
    return results
