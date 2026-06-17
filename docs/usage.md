# Using DevlogSketcher

A direct, step-by-step walkthrough. DevlogSketcher reviews a repo's git history and
proposes **outlined** devlog/social post ideas into a per-project database. It decides
*what to write about* — it never writes the post.

## 1. Install

Requires Python ≥ 3.11. The core CLI is zero-dependency; the `[ai]` extra adds the
Anthropic SDK used by the planner and research agent. Without it, only the offline
stub backends work.

### Recommended: install with pipx (run `devlog` from anywhere)

[pipx](https://pipx.pypa.io) installs the tool into its own isolated environment and
puts `devlog` on your PATH — so you **never have to activate a venv** to use it.

```bash
# one-time: install pipx if you don't have it
python3 -m pip install --user pipx
python3 -m pipx ensurepath          # adds pipx's bin dir to PATH; restart your shell

# install DevlogSketcher from a clone of this repo, with the AI backend:
cd /path/to/DevlogSketcher
pipx install '.[ai]'

devlog --version                    # works from any directory now
```

To update after pulling new changes: `pipx reinstall devlogsketcher` (or
`pipx install --force '.[ai]'`). To remove it: `pipx uninstall devlogsketcher`.

> Prefer an always-live checkout? `pipx install -e '.[ai]'` installs it **editable**,
> so code changes take effect without reinstalling — still runnable from anywhere.

### Alternative: a plain venv (dev workflow)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[ai]'
```

This also gives you `devlog`, but only while the venv is activated.

## 2. Set credentials

The AI backends call Claude, so set an API key — put this in your shell profile
(`~/.bashrc`, `~/.zshrc`, …) so it's always available, including to the pipx-installed
`devlog`:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

(`ANTHROPIC_AUTH_TOKEN` / `ant auth login` also work.) You only need this for `run`
and `research`; `list`/`show`/`status` never call the API.

## 3. Link a repo

`cd` into the repo you want to write about and link it once:

```bash
cd /path/to/your/project
devlog init
```

This registers the repo and creates its state under
`~/.local/share/devlogsketcher/projects/<name>-<hash>/` (a SQLite database plus seeded
audience templates). **Nothing is written into your repo.**

- Name it explicitly: `devlog init --name "My Project"`
- Already linked? `init` refuses, to prevent accidentally wiring two repos together.
- Moved the repo? Re-point it from the new location: `devlog init --relink "My Project"`
- See everything you've linked: `devlog projects`

## 4. (Optional) Tune audience templates

Each project ships with two templates — `general` and `dev` — that steer the tone and
scope of ideas. They're plain markdown you edit directly.

```bash
devlog templates            # lists them and prints the templates directory path
```

Open that directory and edit the `.md` files, or add a new one (e.g.
`release-notes.md`) — the filename (without `.md`) becomes the audience name. Add or
change templates *before* running so the planner can target them.

Templates are only seeded when a project is first linked, so projects you linked
before a default changed keep their old copies. To refresh the built-in templates
(`general`, `dev`) to the current defaults:

```bash
devlog templates --reseed
```

Any built-in template you've edited is backed up to `<name>.md.bak` before being
overwritten; up-to-date ones are left alone, missing ones are recreated, and your own
custom templates are never touched.

## 5. Generate post ideas

```bash
devlog run
```

This builds a digest of recent commits (default: a **30-day** trailing window, wider
than a weekly cadence so a multi-commit feature stays one story), sends it to the
planner along with your existing entries, and writes new/updated ideas to the database.
It dedupes by meaning against what's already there, so running weekly won't re-suggest
the same idea.

Useful flags:

```bash
devlog run --window 60        # look back 60 days instead of 30
devlog run --show-prompt      # print what the planner would see, don't call the API
devlog run --backend heuristic  # offline stub, no API call (for testing the pipeline)
```

## 6. Browse the database

```bash
devlog list                       # everything, newest first
devlog list --status suggested    # only un-actioned ideas
devlog list --audience dev        # only dev-targeted ideas
devlog show 1                     # full entry: summary, source refs, outline
```

## 7. Deepen the one you'll write

The planner gives a pitch + a thin outline. When you're ready to actually write a post,
have the research agent dig into the codebase and flesh it out:

```bash
devlog research 1
```

This runs an agentic loop with read-only, repo-scoped file tools (`read_file`, `grep`,
`glob`, `list_directory`), confirms the technical specifics, expands the outline with
concrete details and caveats, and flips the entry to `researched`. Run it only on the
ideas you intend to write — it's the expensive step, so it's on-demand, not automatic.

```bash
devlog research 1 --backend stub   # offline placeholder, no API call
```

Then read the result and write your post:

```bash
devlog show 1
```

## 8. Track what you've done

Move an entry through its lifecycle so future runs know what's handled:

```bash
devlog status 1 in_progress
devlog status 1 published     # done — drops out of the candidate pool
devlog status 1 rejected      # not writing it — also drops out
```

Statuses: `suggested` → `researched` → `in_progress` → `published`, plus `rejected`
and `stale` (superseded by later changes; flagged for refresh on future runs).
Published and rejected entries are excluded from future dedup/proposals, so the planner
won't suggest them again.

## Typical weekly loop

```bash
cd ~/code/my-project
devlog run                    # 1. propose ideas from the last 30 days
devlog list --status suggested  # 2. skim the new ideas
devlog research 4             # 3. deepen the one you'll write
devlog show 4                 # 4. read the outline, write your post
devlog status 4 published     # 5. mark it done
```

## Web app

Prefer a UI? Everything above is also available in a local web app with full parity:

```bash
devlog web                 # starts on http://127.0.0.1:8765 and opens your browser
devlog web --port 9000 --no-browser
```

It serves a single-page UI that can: link/relink repos, switch between projects, run
the planner and watch live progress, browse and filter ideas, open an idea to read its
summary/outline, change its status, run research (with the same live progress stream),
view and reseed templates, and inspect the digest or planner prompt. The server is
stdlib-only (no extra dependencies); `run` and `research` still need the `[ai]` extra
and your `ANTHROPIC_API_KEY`, exactly like the CLI. It binds to localhost only.

## Command reference

| Command | What it does |
|---|---|
| `devlog init [--name N] [--relink P]` | Link the current repo (or re-point after a move) |
| `devlog projects` | List all linked repos |
| `devlog run [--window N] [--backend B] [--show-prompt]` | Review history → propose/update ideas |
| `devlog list [--status S] [--audience A]` | Browse entries |
| `devlog show <id>` | Show one entry in full |
| `devlog research <id> [--backend B]` | Deepen one entry's outline via codebase research |
| `devlog status <id> <status>` | Set an entry's lifecycle status |
| `devlog templates [--reseed]` | List audience templates (edit the markdown directly); `--reseed` refreshes built-ins to current defaults |
| `devlog digest [--window N]` | Print the raw digest the planner would see (debug) |
| `devlog web [--host H] [--port P] [--no-browser]` | Launch the local web app (full CLI parity) |

A global `--repo PATH` works on any command if you're not inside the repo's directory.
