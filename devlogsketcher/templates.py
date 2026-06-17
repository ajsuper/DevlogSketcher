"""User-defined audience templates.

Each template is a markdown file in the project's ``templates/`` dir describing a
target audience and the tone/angle the planner should aim for. The file's stem is
the audience key stored on entries (e.g. ``general``, ``dev``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SEED_TEMPLATES: dict[str, str] = {
    "general": (
        "# Audience: general\n\n"
        "Public-facing readers and everyday users. They don't care how it was built — "
        "they care what it lets them do, why it matters, and how their day gets better.\n\n"
        "## Tone\n"
        "Plain, warm, benefit-led. Little to no jargon. Talk about outcomes and "
        "experiences, not implementation.\n\n"
        "## Lead with the 'why' and the impact, not the 'what happened'\n"
        "For each idea, the outline should foreground:\n"
        "- **Why it matters** — the problem or friction this removes for the user\n"
        "- **What it lets them do now** — the practical, concrete payoff\n"
        "- **How it changes their experience** — the before/after in their own terms\n"
        "- **Who benefits** — and in which everyday situations it helps\n"
        "Mention the feature itself only as much as needed to make the benefit clear; "
        "the story is the effect, not the mechanism.\n\n"
        "## Good fits\n"
        "- New features, redesigns, and improvements people can feel\n"
        "- Anything that makes the product faster, simpler, safer, or more capable for users\n"
        "- Milestones and releases worth celebrating\n\n"
        "## Avoid\n"
        "- Implementation detail, architecture, and how it works under the hood\n"
        "- Refactors, internal tooling, or changes with no user-visible effect\n"
        "- Leading with 'we changed X' instead of 'here's what you can now do'\n"
    ),
    "dev": (
        "# Audience: dev\n\n"
        "Developers and technically curious readers who want the how and the why.\n\n"
        "## Tone\n"
        "Precise, candid, detail-friendly. Trade-offs and design decisions welcome.\n\n"
        "## Good fits\n"
        "- Architecture changes, interesting bugs, performance work\n"
        "- Tooling, testing, and engineering-process improvements\n\n"
        "## Avoid\n"
        "- Pure marketing framing with no technical substance\n"
    ),
}


@dataclass
class Template:
    audience: str   # file stem, stored on entries
    path: Path
    body: str


def seed_templates(templates_dir: Path) -> None:
    """Create starter templates if the dir is empty (called on init)."""
    templates_dir.mkdir(parents=True, exist_ok=True)
    if any(templates_dir.glob("*.md")):
        return
    for name, body in SEED_TEMPLATES.items():
        (templates_dir / f"{name}.md").write_text(body)


def reseed_templates(templates_dir: Path) -> list[tuple[str, str]]:
    """Refresh the built-in seed templates to current defaults.

    Returns (name, action) per built-in template. Existing files that differ are
    backed up to ``<name>.md.bak`` before being overwritten, so user edits are never
    silently lost. Custom (non-seed) templates are never touched.
    """
    templates_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, str]] = []
    for name, body in SEED_TEMPLATES.items():
        path = templates_dir / f"{name}.md"
        if not path.exists():
            path.write_text(body)
            results.append((name, "created"))
        elif path.read_text() == body:
            results.append((name, "unchanged"))
        else:
            backup = templates_dir / f"{name}.md.bak"
            backup.write_text(path.read_text())
            path.write_text(body)
            results.append((name, f"updated (backup: {backup.name})"))
    return results


def load_templates(templates_dir: Path) -> list[Template]:
    if not templates_dir.exists():
        return []
    return [
        Template(audience=p.stem, path=p, body=p.read_text())
        for p in sorted(templates_dir.glob("*.md"))
    ]
