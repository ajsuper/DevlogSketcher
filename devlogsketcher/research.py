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

# A worked example, kept short, so the model's section layout and depth stay
# consistent run to run. It's illustrative — the real outline mirrors this shape, not
# its content.
_RESEARCH_TEMPLATE = """\
## Shape

**Headline:** Cutting cold-start time in half by caching the parsed config

- **The hook** — open on the symptom users actually felt: slow startup.
- **What was happening** — explain why the config was re-parsed on every launch.
- **The fix** — introduce the on-disk cache and how it short-circuits the parse.
- **The payoff** — show the before/after numbers and what it means for users.
- **Caveat** — note the one case where the cache is intentionally skipped.

## Details

### The hook
- Startup measured at ~1.8s on a cold run (see `bench/startup.py`).
- Users reported it in issue #214.

### What was happening
- `Config.load()` in `app/config.py` re-read and re-validated all 12 YAML files on
  every launch — no memoization.
- Introduced in commit `a1b2c3d`.

### The fix
- New `ConfigCache` (`app/cache.py`, commit `e4f5a6b`) writes a parsed blob to
  `~/.cache/app/config.bin`.
- Keyed by a hash of the source files' mtimes; `load()` returns the cached object
  when the hash matches.

### The payoff
- Cold start dropped to ~0.9s on the same `bench/startup.py` run — about 50% faster.
- Warm starts are unchanged.

### Caveat
- The cache is bypassed when `$APP_ENV=dev` so config edits take effect immediately.
- Don't claim it's always on.
"""

_RESEARCH_SYSTEM = (
    "You research one devlog/social post idea against a codebase and produce a "
    "detailed OUTLINE — never the post prose. Use the file tools to confirm the "
    "technical specifics (real file/feature names, before/after, concrete details).\n"
    "\n"
    "Your reply has exactly two sections, in this order:\n"
    "\n"
    "## Shape\n"
    "The pure structure of the post: a working headline, then the ordered sections "
    "or beats as a bulleted list — one bullet per section, each a short bold label "
    "and ONE sentence saying what it covers and why it's there. No specifics yet; "
    "this is the skeleton only.\n"
    "\n"
    "## Details\n"
    "Walk the Shape section by section, in the same order. Give each section its own "
    "`###` sub-heading whose title matches that section's label, and under it a few "
    "short bullet points carrying the concrete material the writer will actually use: "
    "real file/feature/function names, before/after values, numbers, commit SHAs, "
    "snippets or quotes worth including, and any caveats or things to avoid claiming. "
    "Keep it scannable — short bullets, one fact each, NOT paragraphs or a wall of "
    "text. This is what makes the eventual post accurate and specific.\n"
    "\n"
    "Still an outline, not the post — do not write finished prose.\n"
    "\n"
    "Follow this format exactly:\n"
    "\n"
    + _RESEARCH_TEMPLATE
)


def build_research_prompt(entry: Entry, repo_path: str) -> str:
    return f"""Research this post idea against the repo and return a detailed outline.

Title:       {entry.title}
Audience:    {entry.audience}
Summary:     {entry.summary}
Source refs: {", ".join(entry.source_refs) or "(none)"}

The repo root is the working tree of: {repo_path}
Start by exploring the source refs and relevant files. Then reply with the two
sections described in your instructions:
  1. ## Shape — the headline and ordered sections as a bulleted list, one sentence
     per section.
  2. ## Details — the same sections again, each as its own ### sub-heading with short
     bullet points of concrete specifics (real names, values, SHAs, snippets,
     caveats) the writer will draw on. Keep it scannable, not a wall of text.
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
