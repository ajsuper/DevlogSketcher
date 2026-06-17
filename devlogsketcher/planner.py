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

from .db import Entry, Store
from .digest import Digest, render_digest
from .llm import MODEL, first_text, get_client
from .templates import Template


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


def build_planner_prompt(
    digest: Digest, existing: list[Entry], templates: list[Template]
) -> str:
    """Assemble the planner's instruction text. Used by the real backend; also handy
    to eyeball with `devlog run --show-prompt`."""
    audiences = ", ".join(t.audience for t in templates) or "(none defined)"
    existing_lines = [
        f"  [#{e.id}] ({e.audience}/{e.status}) {e.title} — {e.summary}"
        for e in existing
    ] or ["  (none yet)"]
    template_blocks = "\n\n".join(f"### {t.audience}\n{t.body}" for t in templates)

    return f"""You plan devlog/social posts for a software project. You DO NOT write
posts — you decide what is worth writing about and produce a tight pitch + the seed
of an outline. A separate research step deepens the outline later.

Audiences available: {audiences}

For each idea, choose the best-fit audience template below and match its tone/scope.

# Audience templates
{template_blocks}

# Existing post database (do not duplicate by MEANING, not just wording)
{chr(10).join(existing_lines)}

Rules:
- Skip ideas already covered by an existing entry. If a feature has matured since an
  entry was created, prefer UPDATING that entry over creating a new one.
- A story may span many commits over weeks — group related commits into one idea.
- Ignore changes with no story (pure chores) unless they add up to something.

# Recent repository activity
{render_digest(digest)}

Return a list of proposals: for each, action (create|update), target entry id (if
update), audience, a one-line title, a 1-2 sentence summary pitch, and the commit
SHAs / file paths that motivate it.
"""


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


def claude_proposals(
    digest: Digest, existing: list[Entry], templates: list[Template]
) -> list[Proposal]:
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_PLANNER_SYSTEM,
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": _PROPOSALS_SCHEMA},
        },
        messages=[{"role": "user", "content": build_planner_prompt(digest, existing, templates)}],
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
) -> list[Proposal]:
    existing = store.list_entries()
    if backend == "claude":
        return claude_proposals(digest, existing, templates)
    if backend == "heuristic":
        return heuristic_proposals(digest, existing, templates)
    raise NotImplementedError(f"unknown planner backend '{backend}'")


def apply_proposals(store: Store, proposals: list[Proposal], run_id: int) -> list[int]:
    """Persist proposals; returns the affected entry ids."""
    touched: list[int] = []
    for p in proposals:
        # Only honor an update if the target entry actually exists; otherwise the
        # planner referenced a stale/hallucinated id — fall back to creating one.
        if p.action == "update" and p.entry_id is not None and store.get_entry(p.entry_id):
            store.update_entry(
                p.entry_id, title=p.title, summary=p.summary,
                source_refs=p.source_refs, run_id=run_id,
            )
            touched.append(p.entry_id)
        else:
            eid = store.add_entry(
                audience=p.audience, title=p.title, summary=p.summary,
                source_refs=p.source_refs, run_id=run_id,
            )
            touched.append(eid)
    return touched
