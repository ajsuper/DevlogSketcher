"""``devlog`` command-line entry point."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .auth import generate_key, load_keys, revoke_key
from .branches import (
    branch_notes_path,
    delete_branch_note,
    get_branch_note,
    load_branch_notes,
    set_branch_note,
)
from .db import STATUSES, Store
from .digest import (
    DEFAULT_WINDOW_DAYS,
    build_digest,
    current_branch,
    list_branches,
    render_digest,
)
from .paths import (
    DevlogError,
    Project,
    find_by_name_or_id,
    find_by_repo,
    git_root,
    load_registry,
    new_project_id,
    projects_root,
    resolve_current_project,
    save_registry,
)
from .planner import (
    apply_proposals,
    build_planner_prompt,
    load_planner_prompt_template,
    plan,
    planner_prompt_is_custom,
    planner_prompt_path,
    reset_planner_prompt_template,
    save_planner_prompt_template,
)
from .research import research_entry
from .templates import (
    delete_template,
    load_templates,
    reseed_templates,
    seed_templates,
    template_stub,
    valid_audience,
)


def _open_editor(path: Path) -> bool:
    """Open ``path`` in the user's $EDITOR; returns True if the file changed."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    before = path.read_text() if path.exists() else None
    try:
        subprocess.call([*shlex.split(editor), str(path)])
    except FileNotFoundError:
        raise DevlogError(
            f"could not launch editor '{editor}'. Set $EDITOR, or edit the file "
            f"directly:\n  {path}"
        )
    after = path.read_text() if path.exists() else None
    return before != after


def info(msg: str) -> None:
    """Progress/status line — goes to stderr so stdout stays the actual results."""
    print(msg, file=sys.stderr, flush=True)


def _age(iso: str) -> str:
    """Compact relative age like '3d ago' for an ISO-UTC timestamp ('…Z')."""
    try:
        then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "?"
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 0:
        return "just now"
    for label, span in (("y", 31536000), ("mo", 2592000), ("d", 86400),
                        ("h", 3600), ("m", 60)):
        n = int(secs // span)
        if n >= 1:
            return f"{n}{label} ago"
    return "just now"


# --- commands -------------------------------------------------------------

def cmd_init(args) -> int:
    root = git_root(args.repo)
    projects = load_registry()
    existing = find_by_repo(projects, str(root))

    if args.relink:
        target = find_by_name_or_id(projects, args.relink)
        if target is None:
            raise DevlogError(f"no existing project named/id '{args.relink}' to relink")
        if existing is not None and existing.id != target.id:
            raise DevlogError(
                f"this repo is already linked to project '{existing.name}' "
                f"({existing.id}); not relinking"
            )
        target.repo_path = str(root)
        save_registry(projects)
        target.templates_dir.mkdir(parents=True, exist_ok=True)
        print(f"Relinked project '{target.name}' ({target.id})\n  -> {root}")
        return 0

    # Plain init: refuse if this repo is already registered (guards accidental linking).
    if existing is not None:
        raise DevlogError(
            f"repo is already linked to project '{existing.name}' ({existing.id}).\n"
            f"Use `devlog init --relink {existing.name}` to re-point it after a move."
        )

    name = args.name or root.name
    pid = new_project_id(name, str(root))
    project = Project(
        id=pid, name=name, repo_path=str(root),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    projects[pid] = project
    save_registry(projects)
    seed_templates(project.templates_dir)
    Store(project.db_path).close()  # create the db file
    print(f"Linked '{name}' ({pid})")
    print(f"  repo:      {root}")
    print(f"  state:     {project.dir}")
    print(f"  templates: {project.templates_dir} (seeded: general, dev)")
    return 0


def cmd_projects(args) -> int:
    projects = load_registry()
    if not projects:
        print("No projects linked. Run `devlog init` inside a repo.")
        return 0
    for p in projects.values():
        print(f"{p.id}\n  name: {p.name}\n  repo: {p.repo_path}")
    return 0


def cmd_run(args) -> int:
    project = resolve_current_project(args.repo)
    templates = load_templates(project.templates_dir)
    prompt_template = load_planner_prompt_template(project)
    ref = args.branch or None

    if args.show_prompt:
        digest = build_digest(project.repo_path, args.window, ref=ref)
        note = get_branch_note(project, digest.branch)
        store = Store(project.db_path)
        print(build_planner_prompt(digest, store.list_entries(), templates,
                                   prompt_template, note))
        store.close()
        return 0

    info(f"Project: {project.name}  ({project.repo_path})")
    info(f"Audiences: {', '.join(t.audience for t in templates) or 'none'}")
    digest = build_digest(project.repo_path, args.window, ref=ref)
    branch_note = get_branch_note(project, digest.branch)
    info(f"Reviewing branch '{digest.branch}'"
         + (" (with branch-specific instructions)" if branch_note else ""))
    info(f"Scanning git history (last {args.window} days)…")
    info(f"  {digest.num_commits} commit(s) in window.")

    if digest.num_commits == 0:
        info("Nothing to plan — no commits in this window. Try a wider --window.")
        return 0

    store = Store(project.db_path)
    existing = store.list_entries()
    run_id = store.create_run(
        window_days=digest.window_days, since=digest.since,
        num_commits=digest.num_commits, branch=digest.branch,
    )

    info(f"Reviewing against {len(existing)} existing idea(s).")
    if args.backend == "heuristic":
        info("Planning with heuristic stub (offline)…")
    else:
        info("Planning with Claude (Opus 4.8), deduping by meaning — "
             "this can take a minute…")
    proposals = plan(digest, store, templates, backend=args.backend,
                     prompt_template=prompt_template, branch_note=branch_note,
                     target=max(0, args.target), progress=info)
    results = apply_proposals(store, proposals, run_id, branch=digest.branch)
    store.close()

    created = sum(1 for _, a in results if a == "created")
    updated = sum(1 for _, a in results if a == "updated")
    info(f"  Planner returned {len(proposals)} proposal(s): "
         f"{created} new, {updated} updated.")

    if not results:
        print("No new post ideas this run.")
        return 0
    print(f"Run #{run_id}: {created} new, {updated} updated.")
    if args.backend == "heuristic":
        print("(heuristic stub backend — use the default 'claude' backend for real ideas)")
    for eid, action in results:
        line = store_entry_line(project, eid)
        if line:
            print(f"  [{action}] {line}")
    return 0


def store_entry_line(project: Project, entry_id: int) -> str | None:
    store = Store(project.db_path)
    e = store.get_entry(entry_id)
    store.close()
    if not e:
        return None
    return f"#{e.id} [{e.audience}/{e.status}] {e.title}"


def cmd_list(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    entries = store.list_entries(status=args.status, audience=args.audience,
                                 branch=args.branch)
    store.close()
    if not entries:
        print("No matching entries.")
        return 0
    for e in entries:
        tags = f"#{e.id:>3} [{e.audience}/{e.status}]"
        if e.branch:
            tags += f" {{{e.branch}}}"
        print(f"{tags} {e.title}")
        meta = f"      created {e.created_at[:10]} ({_age(e.created_at)})"
        if e.scheduled_for:
            meta += f"  · scheduled {e.scheduled_for}"
        print(meta)
        if e.summary:
            print(f"      {e.summary}")
    return 0


def cmd_show(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    e = store.get_entry(args.id)
    store.close()
    if e is None:
        raise DevlogError(f"no entry #{args.id}")
    print(f"#{e.id}  [{e.audience}/{e.status}]  {e.title}")
    print(f"created {e.created_at} ({_age(e.created_at)})  "
          f"updated {e.updated_at} ({_age(e.updated_at)})")
    print(f"branch: {e.branch or '(none)'}    scheduled: {e.scheduled_for or '(not scheduled)'}")
    print(f"\nSummary:\n  {e.summary or '(none)'}")
    print(f"\nSource refs: {', '.join(e.source_refs) or '(none)'}")
    print(f"\nOutline:\n{e.outline or '  (not researched yet — run `devlog research %d`)' % e.id}")
    return 0


def cmd_research(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    target = store.get_entry(args.id)
    if target is None:
        store.close()
        raise DevlogError(f"no entry #{args.id}")
    if args.backend != "stub":
        info(f"Researching #{target.id}: {target.title}")
        info(f"Agent (Opus 4.8) is reading {project.name} to flesh out the outline…")
    try:
        e = research_entry(store, project, args.id,
                           backend=args.backend, progress=info)
    finally:
        store.close()
    info("Done.")
    print(f"Researched #{e.id} -> status '{e.status}'.")
    print("View it with `devlog show %d`." % e.id)
    return 0


def cmd_export(args) -> int:
    from .export import EXPORT_FORMATS, export_filename, render_entry
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    e = store.get_entry(args.id)
    store.close()
    if e is None:
        raise DevlogError(f"no entry #{args.id}")
    content = render_entry(e, args.format)
    if args.output and args.output != "-":
        path = Path(args.output)
        if path.is_dir():
            path = path / export_filename(e, args.format)
        path.write_text(content)
        print(f"Exported #{e.id} -> {path}")
    else:
        sys.stdout.write(content)
    return 0


def cmd_status(args) -> int:
    if args.new_status not in STATUSES:
        raise DevlogError(
            f"invalid status '{args.new_status}'. One of: {', '.join(STATUSES)}"
        )
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    e = store.get_entry(args.id)
    if e is None:
        store.close()
        raise DevlogError(f"no entry #{args.id}")
    store.update_entry(args.id, status=args.new_status)
    store.close()
    print(f"#{args.id}: {e.status} -> {args.new_status}")
    return 0


def cmd_templates(args) -> int:
    project = resolve_current_project(args.repo)
    tdir = project.templates_dir
    if args.reseed:
        for name, action in reseed_templates(tdir):
            print(f"  {name:<12} {action}")
        print(f"Reseeded built-in templates in {tdir}")
        return 0
    if args.delete:
        if delete_template(tdir, args.delete):
            print(f"Deleted template '{args.delete}'")
            return 0
        raise DevlogError(f"no template '{args.delete}'")
    if args.edit:
        if not valid_audience(args.edit):
            raise DevlogError(
                f"invalid audience name '{args.edit}'; use letters, digits, '-' or '_'")
        path = tdir / f"{args.edit}.md"
        created = not path.exists()
        if created:
            tdir.mkdir(parents=True, exist_ok=True)
            path.write_text(template_stub(args.edit))
        _open_editor(path)
        print(f"{'Created' if created else 'Saved'} template '{args.edit}'\n  -> {path}")
        return 0
    templates = load_templates(tdir)
    print(f"Templates dir: {project.templates_dir}")
    if not templates:
        print("  (none — add markdown files named <audience>.md)")
        return 0
    for t in templates:
        first_line = next((l for l in t.body.splitlines() if l.strip()), "")
        print(f"  {t.audience:<12} {first_line}")
    return 0


def cmd_prompt(args) -> int:
    project = resolve_current_project(args.repo)
    if args.reset:
        if reset_planner_prompt_template(project):
            print("Planner prompt reset to the default.")
        else:
            print("Already using the default planner prompt.")
        return 0
    if args.edit:
        path = planner_prompt_path(project)
        if not path.exists():
            project.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(load_planner_prompt_template(project))
        _open_editor(path)
        # Re-save through the validator so a removed {{digest}} is caught, not stored.
        save_planner_prompt_template(project, path.read_text())
        print(f"Saved planner prompt\n  -> {path}")
        return 0
    label = "custom" if planner_prompt_is_custom(project) else "default"
    print(f"# planner prompt ({label}) — edit with `devlog prompt --edit`")
    print(load_planner_prompt_template(project))
    return 0


def cmd_keys(args) -> int:
    if args.keys_cmd == "add":
        key, raw = generate_key(args.label)
        print("Access key created. Copy it now — it is NOT shown again:\n")
        print(f"    {raw}\n")
        print(f"  label: {key.label or '(none)'}    id: {key.id}")
        print("\nUsers sign in with this key at `devlog web`. "
              "Web auth is now required.")
        return 0
    if args.keys_cmd == "revoke":
        key = revoke_key(args.target)
        if key is None:
            raise DevlogError(f"no key matching '{args.target}' (try `devlog keys list`)")
        print(f"Revoked key '{key.label or key.id}' ({key.id}).")
        return 0
    # list
    keys = load_keys()
    if not keys:
        print("No access keys configured — the web app is OPEN (no sign-in required).")
        print("Run `devlog keys add` to require a key.")
        return 0
    print(f"{len(keys)} access key(s) — web sign-in is required:")
    for k in keys:
        print(f"  {k.id}  {k.label or '(no label)':<18} "
              f"created {k.created_at[:10]}  sha256:{k.hash[:12]}…")
    return 0


def cmd_web(args) -> int:
    from .web import serve
    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_digest(args) -> int:
    project = resolve_current_project(args.repo)
    digest = build_digest(project.repo_path, args.window, ref=args.branch or None)
    print(render_digest(digest))
    return 0


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def cmd_delete(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    e = store.get_entry(args.id)
    if e is None:
        store.close()
        raise DevlogError(f"no entry #{args.id}")
    store.delete_entry(args.id)
    store.close()
    print(f"Deleted #{args.id}: {e.title}")
    return 0


def cmd_reset(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    count = len(store.list_entries())
    if count == 0:
        store.close()
        print("No entries to clear.")
        return 0
    if not args.yes:
        store.close()
        reply = input(f"Delete ALL {count} entries for '{project.name}'? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0
        store = Store(project.db_path)
    n = store.clear_entries()
    store.close()
    print(f"Cleared {n} entr{'y' if n == 1 else 'ies'} from '{project.name}'.")
    return 0


def cmd_unlink(args) -> int:
    projects = load_registry()
    target = find_by_name_or_id(projects, args.project)
    if target is None:
        raise DevlogError(f"no project named/id '{args.project}'")
    del projects[target.id]
    save_registry(projects)
    print(f"Unlinked '{target.name}' ({target.id}).")
    print(f"Its stored ideas remain at {target.dir} — delete that folder to remove them.")
    return 0


def cmd_branches(args) -> int:
    project = resolve_current_project(args.repo)
    if args.delete:
        if delete_branch_note(project, args.delete):
            print(f"Removed branch instructions for '{args.delete}'.")
            return 0
        raise DevlogError(f"no branch instructions for '{args.delete}'")
    if args.edit:
        path = branch_notes_path(project)
        # Edit a temp file holding just this branch's note, then save it back.
        import tempfile
        existing = get_branch_note(project, args.edit)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write(existing or _branch_note_stub(args.edit))
            tmp = Path(tf.name)
        _open_editor(tmp)
        set_branch_note(project, args.edit, tmp.read_text())
        tmp.unlink(missing_ok=True)
        note = get_branch_note(project, args.edit)
        print(f"{'Saved' if note else 'Cleared'} instructions for branch '{args.edit}'"
              + (f"\n  -> {path}" if note else ""))
        return 0
    # list
    notes = load_branch_notes(project)
    try:
        branches = list_branches(project.repo_path)
    except Exception:  # noqa: BLE001 — repo may be unavailable
        branches = []
    cur = current_branch(project.repo_path) if branches else ""
    print(f"Branches in {project.name} (★ = current, ✎ = has instructions):")
    for b in branches:
        marks = ("★" if b == cur else " ") + ("✎" if b in notes else " ")
        first = (notes.get(b, "").splitlines() or [""])[0]
        print(f"  {marks} {b:<24} {first}")
    extra = [b for b in notes if b not in branches]
    for b in extra:
        first = (notes.get(b, "").splitlines() or [""])[0]
        print(f"   ✎ {b:<24} {first}  (no such local branch)")
    if not branches and not notes:
        print("  (none)")
    return 0


def _branch_note_stub(branch: str) -> str:
    return (f"# Instructions for the '{branch}' branch\n\n"
            "How should the planner frame ideas reviewed from this branch?\n"
            "e.g. 'Frame as work-in-progress; hedge on timelines.' or\n"
            "'Frame as shipped and available now.'\n")


def cmd_schedule(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    e = store.get_entry(args.id)
    if e is None:
        store.close()
        raise DevlogError(f"no entry #{args.id}")
    date = "" if args.date.lower() in ("none", "clear", "-") else args.date
    if date and not _valid_date(date):
        store.close()
        raise DevlogError(f"date must be YYYY-MM-DD (or 'none' to clear), got '{args.date}'")
    store.update_entry(args.id, scheduled_for=date)
    store.close()
    if date:
        print(f"#{args.id} scheduled for {date}.")
    else:
        print(f"#{args.id} unscheduled.")
    return 0


def cmd_agenda(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    scheduled = [e for e in store.list_entries() if e.scheduled_for]
    store.close()
    if not scheduled:
        print("Nothing scheduled. Set a date with `devlog schedule <id> YYYY-MM-DD`.")
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scheduled.sort(key=lambda e: e.scheduled_for)
    print("Publishing agenda:")
    for e in scheduled:
        when = e.scheduled_for
        marker = "⚠ overdue" if when < today else ("● today" if when == today else "")
        print(f"  {when}  #{e.id:>3} [{e.audience}/{e.status}] {e.title}  {marker}".rstrip())
    return 0


# --- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="devlog", description=__doc__)
    p.add_argument("--version", action="version", version=f"devlogsketcher {__version__}")
    p.add_argument("--repo", help="repo path (default: git root of cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="link the current repo to a project")
    s.add_argument("--name", help="project name (default: repo dir name)")
    s.add_argument("--relink", metavar="PROJECT",
                   help="re-point an existing project (by name/id) at this repo path")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("projects", help="list all linked projects")
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("run", help="review history and propose/update post ideas")
    s.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                   help=f"trailing look-back in days (default {DEFAULT_WINDOW_DAYS})")
    s.add_argument("--branch", help="branch to review (default: current checkout)")
    s.add_argument("--target", type=int, default=0, metavar="N",
                   help="aim for N new entries (0 = no target; quality always wins)")
    s.add_argument("--backend", default="claude", choices=["claude", "heuristic"],
                   help="planner backend (default: claude; heuristic = offline stub)")
    s.add_argument("--show-prompt", action="store_true",
                   help="print the planner prompt instead of running")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("list", help="list post entries")
    s.add_argument("--status", choices=STATUSES)
    s.add_argument("--audience")
    s.add_argument("--branch", help="only entries from this branch")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show one entry in full")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("research", help="deepen one entry's outline (codebase research)")
    s.add_argument("id", type=int)
    s.add_argument("--backend", default="claude", choices=["claude", "stub"],
                   help="research backend (default: claude; stub = offline)")
    s.set_defaults(func=cmd_research)

    s = sub.add_parser("export", help="export an entry (outline + context) to a text file")
    s.add_argument("id", type=int)
    s.add_argument("--format", default="md", choices=["md", "txt", "html"],
                   help="output format (default: md)")
    s.add_argument("--output", "-o", metavar="PATH",
                   help="write to PATH (or a directory); default: stdout")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="set an entry's lifecycle status")
    s.add_argument("id", type=int)
    s.add_argument("new_status", help=f"one of: {', '.join(STATUSES)}")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("delete", help="delete one entry")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("reset", help="delete ALL entries for this project")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("unlink", help="remove a project from the registry")
    s.add_argument("project", help="project name or id")
    s.set_defaults(func=cmd_unlink)

    s = sub.add_parser("schedule", help="set/clear an entry's target publish date")
    s.add_argument("id", type=int)
    s.add_argument("date", help="YYYY-MM-DD, or 'none' to clear")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("agenda", help="list scheduled entries by date")
    s.set_defaults(func=cmd_agenda)

    s = sub.add_parser("branches", help="per-branch planner instructions")
    s.add_argument("--edit", metavar="BRANCH",
                   help="create/edit instructions for a branch in $EDITOR")
    s.add_argument("--delete", metavar="BRANCH", help="remove a branch's instructions")
    s.set_defaults(func=cmd_branches)

    s = sub.add_parser("templates", help="list/create/edit this project's audience templates")
    s.add_argument("--edit", metavar="AUDIENCE",
                   help="create (if new) and open a template in $EDITOR")
    s.add_argument("--delete", metavar="AUDIENCE", help="delete a template")
    s.add_argument("--reseed", action="store_true",
                   help="refresh built-in templates to current defaults "
                        "(existing edits backed up to <name>.md.bak)")
    s.set_defaults(func=cmd_templates)

    s = sub.add_parser("prompt", help="view/edit the planner prompt template")
    s.add_argument("--edit", action="store_true", help="open the prompt in $EDITOR")
    s.add_argument("--reset", action="store_true",
                   help="discard the custom prompt and use the default")
    s.set_defaults(func=cmd_prompt)

    s = sub.add_parser("digest", help="print the repo digest the planner would see")
    s.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS)
    s.add_argument("--branch", help="branch to digest (default: current checkout)")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("web", help="launch the local web app (full CLI parity)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true",
                   help="don't auto-open a browser")
    s.set_defaults(func=cmd_web)

    s = sub.add_parser("keys", help="manage web access keys (sign-in for `devlog web`)")
    ks = s.add_subparsers(dest="keys_cmd", required=True)
    ka = ks.add_parser("add", help="generate a new access key (printed once)")
    ka.add_argument("--label", default="", help="a name to remember this key by")
    ka.set_defaults(func=cmd_keys)
    kl = ks.add_parser("list", help="list configured keys (hashes only)")
    kl.set_defaults(func=cmd_keys)
    kr = ks.add_parser("revoke", help="revoke a key by id, label, or hash prefix")
    kr.add_argument("target")
    kr.set_defaults(func=cmd_keys)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DevlogError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
