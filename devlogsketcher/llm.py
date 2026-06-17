"""Shared Anthropic client setup for the planner and research backends.

Both backends drive Claude Opus 4.8 through the official Anthropic SDK. The SDK
is an optional dependency (the `ai` extra) so the core CLI stays zero-dependency;
this module fails with a clear, actionable error when it's missing.
"""

from __future__ import annotations

import os

from .paths import DevlogError

# Opus 4.8: most capable, state-of-the-art long-horizon agentic + knowledge work.
MODEL = "claude-opus-4-8"


def get_client():
    try:
        import anthropic
    except ImportError:
        raise DevlogError(
            "the AI backend needs the Anthropic SDK.\n"
            "  Install it with:  pip install 'devlogsketcher[ai]'"
        )
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise DevlogError(
            "no Anthropic credentials found.\n"
            "  Set ANTHROPIC_API_KEY (or run `ant auth login`)."
        )
    return anthropic.Anthropic()


def first_text(message) -> str:
    """The first text block of a response (skips thinking blocks)."""
    return next((b.text for b in message.content if b.type == "text"), "")
