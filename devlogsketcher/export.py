"""Render a post entry to common text formats for export.

Markdown, plain text, and HTML — stdlib-only and dependency-free, matching the rest
of the tool. The CLI (`devlog export`) and the web app both go through these renderers
so a downloaded file is identical regardless of which interface produced it.

Each document leads with the entry title and metadata, then the summary, source refs,
and the outline — the outline is the point of the export, but the surrounding context
makes the file useful on its own.
"""

from __future__ import annotations

import html as _html
import re

from .db import Entry
from .paths import DevlogError


def _meta_line(entry: Entry) -> str:
    parts = [entry.audience, entry.status]
    if entry.branch:
        parts.append(f"branch {entry.branch}")
    if entry.scheduled_for:
        parts.append(f"scheduled {entry.scheduled_for}")
    parts.append(f"created {entry.created_at}")
    parts.append(f"updated {entry.updated_at}")
    return " · ".join(parts)


# --- Markdown -------------------------------------------------------------

def entry_to_markdown(entry: Entry) -> str:
    refs = "\n".join(f"- `{r}`" for r in entry.source_refs) or "_none_"
    outline = entry.outline or "_Not researched yet._"
    return (
        f"# {entry.title}\n\n"
        f"> {_meta_line(entry)}\n\n"
        f"## Summary\n\n{entry.summary or '_none_'}\n\n"
        f"## Source refs\n\n{refs}\n\n"
        f"## Outline\n\n{outline}\n"
    )


# --- plain text -----------------------------------------------------------

def _strip_inline(s: str) -> str:
    """Drop inline markdown markers for a clean plain-text reading."""
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*\n]+)\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", s)
    return s


def markdown_to_text(md: str) -> str:
    out: list[str] = []
    for line in md.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            out.append(_strip_inline(h.group(2)).upper())
            continue
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1- ", line)
        out.append(_strip_inline(line))
    return "\n".join(out)


def entry_to_text(entry: Entry) -> str:
    refs = ", ".join(entry.source_refs) or "none"
    outline = markdown_to_text(entry.outline) if entry.outline else "Not researched yet."
    rule = "=" * 70
    return (
        f"{entry.title}\n{rule}\n"
        f"{_meta_line(entry)}\n\n"
        f"SUMMARY\n{_strip_inline(entry.summary or 'none')}\n\n"
        f"SOURCE REFS\n{refs}\n\n"
        f"OUTLINE\n{outline}\n"
    )


# --- HTML -----------------------------------------------------------------
# A compact, self-contained Markdown -> HTML pass mirroring the web frontend's
# renderer, so a server-side export matches what the UI shows.

def _safe_url(url: str) -> str:
    return url if re.match(r"^(https?://|mailto:|#|/)", url, re.I) else "#"


def _md_inline_html(t: str) -> str:
    t = _html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(^|[^*])\*([^*\n]+)\*", r"\1<em>\2</em>", t)
    t = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{_html.escape(_safe_url(m.group(2)))}">{m.group(1)}</a>',
        t,
    )
    return t


def markdown_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    is_ul = lambda l: re.match(r"^\s*[-*+]\s+", l)  # noqa: E731
    is_ol = lambda l: re.match(r"^\s*\d+\.\s+", l)  # noqa: E731
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("```"):
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{_html.escape(chr(10).join(buf))}</code></pre>")
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{_md_inline_html(h.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^\s*>\s?", line):
            buf = []
            while i < n and re.match(r"^\s*>\s?", lines[i]):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{_md_inline_html(' '.join(buf))}</blockquote>")
            continue
        if is_ul(line):
            out.append("<ul>")
            while i < n and is_ul(lines[i]):
                out.append(f"<li>{_md_inline_html(re.sub(r'^\s*[-*+]\s+', '', lines[i]))}</li>")
                i += 1
            out.append("</ul>")
            continue
        if is_ol(line):
            out.append("<ol>")
            while i < n and is_ol(lines[i]):
                out.append(f"<li>{_md_inline_html(re.sub(r'^\s*\d+\.\s+', '', lines[i]))}</li>")
                i += 1
            out.append("</ol>")
            continue
        if line.strip() == "":
            i += 1
            continue
        para: list[str] = []
        while (i < n and lines[i].strip() != ""
               and not re.match(r"^(#{1,6})\s|^```|^\s*>\s?", lines[i])
               and not is_ul(lines[i]) and not is_ol(lines[i])):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{_md_inline_html(' '.join(para))}</p>")
    return "\n".join(out)


def entry_to_html(entry: Entry) -> str:
    body = markdown_to_html(entry_to_markdown(entry))
    title = _html.escape(entry.title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ max-width:46rem; margin:2.5rem auto; padding:0 1.25rem;
         font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; color:#1a1d27; }}
  h1 {{ line-height:1.2; }} h2 {{ margin-top:1.8rem; }}
  blockquote {{ margin:1rem 0; padding:.2rem 1rem; border-left:3px solid #d0d4dd; color:#5a6273; }}
  code {{ background:#f0f2f6; border-radius:4px; padding:.1em .35em; font-size:.9em; }}
  pre {{ background:#f0f2f6; padding:1rem; border-radius:8px; overflow:auto; }}
  pre code {{ background:none; padding:0; }}
  a {{ color:#2c5fd0; }}
</style>
</head>
<body>
<article>
{body}
</article>
</body>
</html>
"""


# --- dispatch -------------------------------------------------------------

_RENDERERS = {
    "md": entry_to_markdown,
    "txt": entry_to_text,
    "html": entry_to_html,
}
EXPORT_FORMATS = tuple(_RENDERERS)  # ("md", "txt", "html")
MIME = {"md": "text/markdown", "txt": "text/plain", "html": "text/html"}


def render_entry(entry: Entry, fmt: str) -> str:
    if fmt not in _RENDERERS:
        raise DevlogError(
            f"unknown export format '{fmt}'. One of: {', '.join(EXPORT_FORMATS)}")
    return _RENDERERS[fmt](entry)


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (slug[:40].strip("-")) or "entry"


def export_filename(entry: Entry, fmt: str) -> str:
    return f"entry-{entry.id}-{_slug(entry.title)}.{fmt}"
