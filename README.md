# DevlogSketcher

Reviews a repo's git history on a cadence (run it weekly) and proposes **outlined**
devlog / social-post ideas into a per-project database. It decides *what's worth
writing about* — it does **not** write the post. You keep the voice; it does the
"what should I even post about this week" thinking and remembers what's already been
covered.

## Why it's useful

- **Outlines, not slop.** It proposes a pitch + outline; you write the actual post.
- **Memory across runs.** A persistent post database means ideas get deduped, ideas
  you've published or rejected drop out, and stories that span weeks stay one post.
- **Audience targeting.** User-defined templates (`general`, `dev`, …) steer tone and
  scope per idea.

## Two-stage design

1. **Planner** reviews the repo digest + existing entries and proposes/updates ideas
   (cheap, runs every cadence).
2. **Research** runs *on demand* for one entry — a codebase-research step that reads
   the relevant files to turn a thin pitch into a detailed, accurate outline.

The AI backends (Claude planner, Agent-SDK research subagent) are stubbed in v1 so
the whole pipeline runs end-to-end; swap them in without changing the CLI.

## State lives outside your repos

Nothing is written into a target repo or this one. All state is under the XDG data
dir, keyed by repo:

```
~/.local/share/devlogsketcher/
  registry.json                 # repo path -> project
  projects/<name>-<hash>/
    store.db                    # sqlite post database
    templates/                  # audience templates (markdown)
```

A repo is identified by the git root of your cwd (or `--repo PATH`).

## Usage

```bash
devlog init                 # link the current repo (--name to override, --relink to re-point after a move)
devlog run                  # review history -> propose/update post ideas (--window N days, default 30)
devlog list [--status S] [--audience A]
devlog show <id>
devlog research <id>        # deepen one entry's outline
devlog status <id> <state>  # suggested|researched|in_progress|published|rejected|stale
devlog templates            # list audience templates (edit the markdown files directly)
devlog projects             # all linked repos
devlog digest               # debug: print what the planner would see
```

Run `devlog init` once per repo. Moving a repo? `devlog init --relink <project>` from
the new location. Plain `init` refuses to touch an already-linked repo, so you can't
accidentally cross-wire two repos to one project.
