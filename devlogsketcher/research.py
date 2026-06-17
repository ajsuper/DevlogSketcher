"""On-demand deep research for a single entry.

The planner decides *what* to write about; this step does the codebase research —
reading the relevant files via repo-scoped tools to confirm technical details and
expand a thin pitch into a detailed, accurate outline. It runs lazily (user picks
an entry), not for every idea every run, to keep cost and latency down.

The `claude` backend runs a manual Anthropic tool-use loop with the read-only file
tools in `repo_tools.py`. A `stub` backend remains for offline testing.
"""

from __future__ import annotations

from typing import Callable

from .db import Entry, Store
from .llm import MODEL, first_text, get_client
from .paths import Project
from .repo_tools import TOOL_SCHEMAS, RepoTools

MAX_TURNS = 24  # safety cap on the agentic loop

# A progress sink: called with short status lines as research proceeds. No-op default.
Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _describe_tool(name: str, args: dict) -> str:
    """One-line summary of a tool call for progress output."""
    target = args.get("path") or args.get("pattern") or ""
    return f"{name}({target})" if target else name

_RESEARCH_SYSTEM = (
    "You research one devlog/social post idea against a codebase and produce a "
    "detailed OUTLINE — never the post prose. Use the file tools to confirm the "
    "technical specifics (real file/feature names, before/after, concrete details). "
    "When done, reply with the outline only: a working headline, the key sections/"
    "beats, concrete details worth mentioning, and any caveats."
)


def build_research_prompt(entry: Entry, repo_path: str) -> str:
    return f"""Research this post idea against the repo and return a detailed outline.

Title:       {entry.title}
Audience:    {entry.audience}
Summary:     {entry.summary}
Source refs: {", ".join(entry.source_refs) or "(none)"}

The repo root is the working tree of: {repo_path}
Start by exploring the source refs and relevant files, then write the outline.
Outline only — do not write the post.
"""


def _run_agent(entry: Entry, repo_path: str, progress: Progress = _noop) -> str:
    client = get_client()
    tools = RepoTools(repo_path)
    messages = [{"role": "user", "content": build_research_prompt(entry, repo_path)}]

    for turn in range(1, MAX_TURNS + 1):
        progress(f"Thinking (turn {turn}/{MAX_TURNS})…")
        message = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=_RESEARCH_SYSTEM,
            output_config={"effort": "high"},
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        if message.stop_reason != "tool_use":
            progress("Writing the outline…")
            return first_text(message)

        messages.append({"role": "assistant", "content": message.content})
        results = []
        for block in message.content:
            if block.type == "tool_use":
                progress(f"  exploring repo: {_describe_tool(block.name, block.input)}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": tools.run(block.name, block.input),
                })
        messages.append({"role": "user", "content": results})

    return "(research stopped: hit the tool-use turn limit before finishing)"


def research_entry(
    store: Store,
    project: Project,
    entry_id: int,
    backend: str = "claude",
    progress: Progress = _noop,
) -> Entry:
    entry = store.get_entry(entry_id)
    if entry is None:
        raise ValueError(f"no entry #{entry_id}")

    if backend == "claude":
        outline = _run_agent(entry, project.repo_path, progress)
    elif backend == "stub":
        outline = (
            "[STUB OUTLINE] research backend not run.\n\n"
            f"{build_research_prompt(entry, project.repo_path)}"
        )
    else:
        raise ValueError(f"unknown research backend '{backend}'")

    store.update_entry(entry_id, outline=outline, status="researched")
    return store.get_entry(entry_id)
