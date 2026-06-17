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
        "Public-facing readers and general users. They care about *what changed for "
        "them* and why it matters — not implementation detail.\n\n"
        "## Tone\n"
        "Plain, warm, benefit-led. Minimal jargon. Lead with the user-visible outcome.\n\n"
        "## Good fits\n"
        "- New features, redesigns, things people can see or do now\n"
        "- Milestones and releases worth celebrating\n\n"
        "## Avoid\n"
        "- Refactors, internal tooling, anything with no user-visible effect\n"
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


def load_templates(templates_dir: Path) -> list[Template]:
    if not templates_dir.exists():
        return []
    return [
        Template(audience=p.stem, path=p, body=p.read_text())
        for p in sorted(templates_dir.glob("*.md"))
    ]
