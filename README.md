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
    store.db                    # sqlite post database (entries: branch + publish-date aware)
    templates/                  # audience templates (markdown)
    planner.md                  # custom planner prompt (only if edited)
    branches.json               # per-branch planner instructions (only if set)
```

A repo is identified by the git root of your cwd (or `--repo PATH`).

## Usage

```bash
devlog init                 # link the current repo (--name to override, --relink to re-point after a move)
devlog run                  # review history -> propose/update post ideas (--window N, --branch B, --target N)
devlog list [--status S] [--audience A] [--branch B]
devlog show <id>
devlog export <id>          # export an entry's outline to md/txt/html (--format, -o)
devlog research <id>        # deepen one entry's outline
devlog status <id> <state>  # suggested|researched|in_progress|published|rejected|stale
devlog delete <id>          # delete one entry  (reset = delete all; unlink = drop a project)
devlog schedule <id> <date> # set a target publish date (agenda = see the calendar)
devlog branches             # per-branch planner instructions (--edit/--delete <branch>)
devlog templates            # list audience templates (--edit/--delete <audience> to manage them)
devlog prompt               # view the planner prompt (--edit to tune it, --reset to restore default)
devlog projects             # all linked repos
devlog digest               # debug: print what the planner would see (--branch B)
devlog web                  # local web app with full CLI parity (markdown, branches, scheduling)
devlog keys add             # generate an access key to require sign-in when sharing the web app
```

Reviewing by branch: `devlog run --branch dev` reviews a feature branch and tags ideas
with their branch. Give each branch its own framing with `devlog branches --edit dev`
(e.g. "frame as work-in-progress") so a dev-branch idea reads as WIP and a production
one as shipped. Ideas dedupe across branches, so one that starts on `dev` updates in
place when it lands on `main`.

Sharing the web app beyond localhost? `devlog keys add` turns on access-key sign-in
(see `docs/usage.md`); put it behind an HTTPS tunnel since the server speaks plain HTTP.

Run `devlog init` once per repo. Moving a repo? `devlog init --relink <project>` from
the new location. Plain `init` refuses to touch an already-linked repo, so you can't
accidentally cross-wire two repos to one project.
