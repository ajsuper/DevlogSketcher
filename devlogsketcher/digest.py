"""Build the 'repo digest' that the planner reasons over.

v1 keeps the input cheap and zero-config: commit messages plus changed-file stats
over a trailing window (default 30 days, wider than the weekly cadence so a feature
that lands across several commits stays one story). Designed as a pluggable seam:
richer signals (full diffs, PR descriptions, tags/releases) can be layered in later
without changing the planner contract.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .paths import DevlogError

DEFAULT_WINDOW_DAYS = 30


@dataclass
class Commit:
    sha: str
    short_sha: str
    date: str
    author: str
    subject: str
    body: str
    files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


@dataclass
class Digest:
    repo_path: str
    window_days: int
    since: str
    commits: list[Commit]
    branch: str = ""  # the branch/ref this digest was built from

    @property
    def num_commits(self) -> int:
        return len(self.commits)


def current_branch(repo_path: str | Path) -> str:
    """Name of the checked-out branch, or 'HEAD' when detached."""
    out = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out or "HEAD"


def list_branches(repo_path: str | Path) -> list[str]:
    """Local branch names, with the current branch first."""
    out = subprocess.run(
        ["git", "-C", str(repo_path), "for-each-ref", "--format=%(refname:short)",
         "refs/heads"],
        capture_output=True, text=True,
    ).stdout
    branches = [b for b in out.splitlines() if b.strip()]
    cur = current_branch(repo_path)
    if cur in branches:
        branches.remove(cur)
        branches.insert(0, cur)
    return branches


_SEP = "\x1e"  # record separator unlikely to appear in commit text
_FMT = _SEP.join(["%H", "%h", "%cI", "%an", "%s", "%b"]) + "\x1d"


def build_digest(repo_path: str | Path, window_days: int = DEFAULT_WINDOW_DAYS,
                 ref: str | None = None) -> Digest:
    repo = str(Path(repo_path).resolve())
    since = f"{window_days} days ago"
    branch = ref or current_branch(repo)
    if ref:
        verify = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref],
            capture_output=True, text=True,
        )
        if verify.returncode != 0:
            raise DevlogError(f"no such branch/ref '{ref}' in {repo}")
    log_cmd = ["git", "-C", repo, "log"]
    if ref:
        log_cmd.append(ref)
    log_cmd += [f"--since={since}", f"--pretty=format:{_FMT}", "--no-merges"]
    raw = subprocess.run(log_cmd, capture_output=True, text=True, check=True).stdout

    commits: list[Commit] = []
    for record in raw.split("\x1d"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_SEP)
        if len(parts) < 6:
            continue
        sha, short_sha, date, author, subject, body = parts[:6]
        files, ins, dele = _commit_stats(repo, sha)
        commits.append(Commit(
            sha=sha, short_sha=short_sha, date=date, author=author,
            subject=subject, body=body.strip(),
            files=files, insertions=ins, deletions=dele,
        ))
    return Digest(repo_path=repo, window_days=window_days, since=since,
                  commits=commits, branch=branch)


def _commit_stats(repo: str, sha: str) -> tuple[list[str], int, int]:
    out = subprocess.run(
        ["git", "-C", repo, "show", "--numstat", "--format=", sha],
        capture_output=True, text=True, check=True,
    ).stdout
    files, ins, dele = [], 0, 0
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) != 3:
            continue
        add, rem, path = cols
        files.append(path)
        ins += int(add) if add.isdigit() else 0
        dele += int(rem) if rem.isdigit() else 0
    return files, ins, dele


def render_digest(digest: Digest) -> str:
    """Plain-text rendering handed to the planner (and useful for `digest` debug)."""
    lines = [
        f"Repo: {digest.repo_path}",
        f"Branch: {digest.branch or '(current)'}",
        f"Window: last {digest.window_days} days  ({digest.num_commits} commits)",
        "",
    ]
    for c in digest.commits:
        stat = f"+{c.insertions}/-{c.deletions}, {len(c.files)} files"
        lines.append(f"- {c.short_sha} {c.date[:10]} ({c.author}) — {c.subject}  [{stat}]")
        if c.body:
            for bl in c.body.splitlines():
                lines.append(f"    {bl}")
        for f in c.files[:12]:
            lines.append(f"    · {f}")
        if len(c.files) > 12:
            lines.append(f"    · …and {len(c.files) - 12} more files")
    return "\n".join(lines)
