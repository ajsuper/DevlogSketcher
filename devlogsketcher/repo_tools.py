"""Read-only, repo-scoped file tools for the research agent's agentic loop.

Every path is resolved and confined to the repo root, so the model can explore
the codebase to confirm technical details but cannot read outside it. These are
plain Python (no ripgrep dependency); they back the Anthropic tool-use loop in
``research.py``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

MAX_FILE_BYTES = 60_000
MAX_GREP_MATCHES = 80
MAX_GLOB_RESULTS = 200

# Directories never worth showing the model.
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

# Tool schemas advertised to Claude (raw JSON Schema for the Messages API).
TOOL_SCHEMAS = [
    {
        "name": "list_directory",
        "description": "List files and subdirectories at a path within the repo. "
                       "Use '.' for the repo root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative directory path"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the repo (truncated if very large).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Repo-relative file path"}},
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search the repo for a regular expression and return matching lines "
                       "with file:line locations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression"},
                "path": {"type": "string", "description": "Repo-relative subtree to search (default '.')"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "Glob pattern, repo-relative"}},
            "required": ["pattern"],
        },
    },
]


class RepoTools:
    def __init__(self, repo_path: str | os.PathLike[str]):
        self.root = Path(repo_path).resolve()

    def _resolve(self, rel: str) -> Path:
        """Resolve a repo-relative path, refusing anything outside the root."""
        target = (self.root / (rel or ".")).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"path escapes the repo: {rel}")
        return target

    def run(self, name: str, args: dict) -> str:
        try:
            if name == "list_directory":
                return self._list_directory(args.get("path", "."))
            if name == "read_file":
                return self._read_file(args["path"])
            if name == "grep":
                return self._grep(args["pattern"], args.get("path", "."))
            if name == "glob":
                return self._glob(args["pattern"])
            return f"unknown tool: {name}"
        except (ValueError, KeyError, OSError) as e:
            return f"error: {e}"

    def _list_directory(self, rel: str) -> str:
        target = self._resolve(rel)
        if not target.is_dir():
            return f"not a directory: {rel}"
        entries = []
        for child in sorted(target.iterdir()):
            if child.name in _SKIP_DIRS:
                continue
            entries.append(f"{child.name}/" if child.is_dir() else child.name)
        return "\n".join(entries) or "(empty)"

    def _read_file(self, rel: str) -> str:
        target = self._resolve(rel)
        if not target.is_file():
            return f"not a file: {rel}"
        data = target.read_bytes()[:MAX_FILE_BYTES]
        text = data.decode("utf-8", errors="replace")
        if target.stat().st_size > MAX_FILE_BYTES:
            text += f"\n... [truncated at {MAX_FILE_BYTES} bytes]"
        return text

    def _grep(self, pattern: str, rel: str) -> str:
        regex = re.compile(pattern)
        base = self._resolve(rel)
        hits: list[str] = []
        for path in self._walk_files(base):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                if regex.search(line):
                    hits.append(f"{path.relative_to(self.root)}:{n}: {line.strip()[:200]}")
                    if len(hits) >= MAX_GREP_MATCHES:
                        hits.append(f"... [stopped at {MAX_GREP_MATCHES} matches]")
                        return "\n".join(hits)
        return "\n".join(hits) or "(no matches)"

    def _glob(self, pattern: str) -> str:
        results = []
        for path in sorted(self.root.glob(pattern)):
            if any(part in _SKIP_DIRS for part in path.relative_to(self.root).parts):
                continue
            if path.is_file():
                results.append(str(path.relative_to(self.root)))
            if len(results) >= MAX_GLOB_RESULTS:
                break
        return "\n".join(results) or "(no matches)"

    def _walk_files(self, base: Path):
        if base.is_file():
            yield base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                yield Path(dirpath) / fn
