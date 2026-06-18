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

Create, edit, or remove them without leaving the CLI (each opens `$EDITOR`):

```bash
devlog templates --edit release-notes   # create (if new) and edit a template
devlog templates --delete release-notes  # remove one
```

Or open the templates directory and edit the `.md` files by hand — the filename
(without `.md`) becomes the audience name. Add or change templates *before* running so
the planner can target them.

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
devlog run --target 3         # aim for 3 new ideas (0 = no target; default)
devlog run --show-prompt      # print what the planner would see, don't call the API
devlog run --backend heuristic  # offline stub, no API call (for testing the pipeline)
```

`--target N` tells the planner to aim for N new entries this run (the web app has a
matching "Target # entries" field). It's a soft goal, not a quota: if there isn't
enough material or the extra ideas would be duplicates, the planner falls short on
purpose — quality always wins over hitting the number. The default, `0`, sets no
target, so an accidental run won't spray out filler.

### Tune the planner prompt

The instructions the planner runs on are themselves editable per project:

```bash
devlog prompt                 # show the current prompt template
devlog prompt --edit          # edit it in $EDITOR
devlog prompt --reset         # discard your edits, back to the default
```

The template uses `{{audiences}}`, `{{templates}}`, `{{existing}}`, and `{{digest}}`
tokens, which are filled with live data each run. `{{digest}}` is required (it's where
recent repository activity goes). `devlog run --show-prompt` shows the fully assembled
result.

## 5b. Review a specific branch

By default `run` reviews whatever branch is checked out. Point it at any branch — and
give each branch its own framing — so the same work reads differently depending on
where it is:

```bash
devlog run --branch dev          # review the dev branch instead of the current checkout
devlog branches                  # list branches and which have custom instructions
devlog branches --edit dev       # open $EDITOR to set this branch's framing
devlog branches --delete dev     # remove a branch's instructions
```

For example, set `dev` to *"frame as work-in-progress; hedge on timelines"* and your
production branch to *"frame as shipped and available now."* The planner picks up the
note for whichever branch it's reviewing. Each idea is tagged with the branch it came
from (`devlog list --branch dev` filters by it). Ideas still **dedupe across branches**,
so one that starts on `dev` gets updated in place — and re-tagged — when it lands on
`main`, rather than spawning a duplicate.

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

Take it into your editor of choice by exporting the entry (title, metadata, summary,
source refs, and outline) to a file:

```bash
devlog export 1                          # Markdown to stdout
devlog export 1 --format txt             # plain text
devlog export 1 --format html -o ./out/  # HTML into a directory (auto-named file)
devlog export 1 -o draft.md              # write to a specific path
```

Formats: `md` (default), `txt`, `html`. In the web app, open an idea and use the
**Export** dropdown to download it in any of the three.

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

Remove ideas you don't want to keep:

```bash
devlog delete 3              # delete one entry
devlog reset                 # delete ALL entries for this project (asks first; --yes skips)
devlog unlink "My Project"   # drop a project from the registry (its stored ideas stay on disk)
```

## 9. Plan when to publish

Give an idea a target publish date and see your calendar. DevlogSketcher doesn't post
for you — this is a content-planning layer on top of the ideas:

```bash
devlog schedule 4 2026-07-01   # set a publish date
devlog schedule 4 none         # clear it
devlog agenda                  # list scheduled ideas by date (flags overdue / today)
```

In the web app, the **📅 Schedule** tab shows the same agenda grouped into Overdue /
Today / Upcoming / Unscheduled, with inline date pickers.

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

### Access control (sharing it beyond your machine)

By default the web app binds to localhost and requires **no sign-in** — it's a personal
tool. If you want to expose it (to teammates, or yourself across machines), gate it with
**access keys** you generate:

```bash
devlog keys add --label alice    # prints a key ONCE — copy it and share it securely
devlog keys list                 # shows labels + hashes (never the raw keys)
devlog keys revoke alice         # by label, id, or hash prefix — effective immediately
```

As soon as one key exists, `devlog web` requires sign-in: visitors get a login screen
and enter their key once (stored in an `HttpOnly`, `SameSite=Strict` cookie; revoking
the key logs them out on their next request). Only SHA-256 hashes are stored on disk,
so the config file never holds a usable secret.

Two things to know:

- **Use HTTPS when exposing it.** The built-in server speaks plain HTTP, so put it
  behind a TLS tunnel or reverse proxy (Cloudflare Tunnel, Tailscale, nginx, …). The
  cookie auto-sets `Secure` when it sees `X-Forwarded-Proto: https`. Don't put raw
  `--host 0.0.0.0` on the open internet.
- Binding to a non-loopback address **with no keys** prints a loud warning — anyone who
  can reach the port would have full access.

It serves a single-page UI that can: link/relink repos, switch between projects, run
the planner and watch live progress, browse and filter ideas, open an idea to read its
**rendered** summary/outline (markdown, not raw text), change its status, delete it, run research
(with the same live progress stream), export an entry to Markdown/text/HTML, pick which
branch to review and set per-branch instructions, schedule publish dates on a calendar
tab, create/edit/delete/reseed audience templates, edit the planner prompt, and inspect
the digest or assembled planner prompt. The server
is stdlib-only (no extra dependencies); `run` and `research` still need the `[ai]` extra
and your `ANTHROPIC_API_KEY`, exactly like the CLI. It binds to localhost only.

## Command reference

| Command | What it does |
|---|---|
| `devlog init [--name N] [--relink P]` | Link the current repo (or re-point after a move) |
| `devlog projects` | List all linked repos |
| `devlog run [--window N] [--branch B] [--target N] [--backend B] [--show-prompt]` | Review history → propose/update ideas |
| `devlog list [--status S] [--audience A] [--branch B]` | Browse entries |
| `devlog show <id>` | Show one entry in full |
| `devlog export <id> [--format md\|txt\|html] [-o PATH]` | Export an entry (outline + context) to a text file (default: Markdown to stdout) |
| `devlog research <id> [--backend B]` | Deepen one entry's outline via codebase research |
| `devlog status <id> <status>` | Set an entry's lifecycle status |
| `devlog delete <id>` / `devlog reset [--yes]` | Delete one entry / all entries for the project |
| `devlog unlink <project>` | Remove a project from the registry |
| `devlog schedule <id> <date\|none>` / `devlog agenda` | Set a publish date / view the schedule |
| `devlog branches [--edit B] [--delete B]` | Per-branch planner instructions |
| `devlog templates [--edit A] [--delete A] [--reseed]` | List / create / edit / delete audience templates; `--reseed` refreshes built-ins to current defaults |
| `devlog prompt [--edit] [--reset]` | View / edit the planner prompt template |
| `devlog digest [--window N]` | Print the raw digest the planner would see (debug) |
| `devlog web [--host H] [--port P] [--no-browser]` | Launch the local web app (full CLI parity) |
| `devlog keys add [--label L]` / `list` / `revoke <target>` | Manage web access keys (sign-in for `devlog web`) |

A global `--repo PATH` works on any command if you're not inside the repo's directory.
