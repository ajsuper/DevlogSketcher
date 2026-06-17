"""``devlog`` command-line entry point."""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__
from .db import STATUSES, Store
from .digest import DEFAULT_WINDOW_DAYS, build_digest, render_digest
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
from .planner import apply_proposals, plan
from .research import research_entry
from .templates import load_templates, seed_templates


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
    digest = build_digest(project.repo_path, args.window)

    if args.show_prompt:
        from .planner import build_planner_prompt
        store = Store(project.db_path)
        print(build_planner_prompt(digest, store.list_entries(), templates))
        store.close()
        return 0

    store = Store(project.db_path)
    run_id = store.create_run(
        window_days=digest.window_days, since=digest.since,
        num_commits=digest.num_commits,
    )
    proposals = plan(digest, store, templates, backend=args.backend)
    touched = apply_proposals(store, proposals, run_id)
    store.close()

    print(f"Run #{run_id}: {digest.num_commits} commits over {digest.window_days}d "
          f"-> {len(touched)} entr{'y' if len(touched) == 1 else 'ies'} touched.")
    if args.backend == "heuristic":
        print("(heuristic stub backend — wire up the Claude planner for real ideas)")
    for eid in touched:
        e = store_entry_line(project, eid)
        if e:
            print(f"  {e}")
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
    entries = store.list_entries(status=args.status, audience=args.audience)
    store.close()
    if not entries:
        print("No matching entries.")
        return 0
    for e in entries:
        print(f"#{e.id:>3} [{e.audience}/{e.status}] {e.title}")
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
    print(f"created {e.created_at}  updated {e.updated_at}")
    print(f"\nSummary:\n  {e.summary or '(none)'}")
    print(f"\nSource refs: {', '.join(e.source_refs) or '(none)'}")
    print(f"\nOutline:\n{e.outline or '  (not researched yet — run `devlog research %d`)' % e.id}")
    return 0


def cmd_research(args) -> int:
    project = resolve_current_project(args.repo)
    store = Store(project.db_path)
    try:
        e = research_entry(store, project, args.id, backend=args.backend)
    finally:
        store.close()
    print(f"Researched #{e.id} -> status '{e.status}'.")
    print("View it with `devlog show %d`." % e.id)
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
    templates = load_templates(project.templates_dir)
    print(f"Templates dir: {project.templates_dir}")
    if not templates:
        print("  (none — add markdown files named <audience>.md)")
        return 0
    for t in templates:
        first_line = next((l for l in t.body.splitlines() if l.strip()), "")
        print(f"  {t.audience:<12} {first_line}")
    return 0


def cmd_digest(args) -> int:
    project = resolve_current_project(args.repo)
    digest = build_digest(project.repo_path, args.window)
    print(render_digest(digest))
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
    s.add_argument("--backend", default="claude", choices=["claude", "heuristic"],
                   help="planner backend (default: claude; heuristic = offline stub)")
    s.add_argument("--show-prompt", action="store_true",
                   help="print the planner prompt instead of running")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("list", help="list post entries")
    s.add_argument("--status", choices=STATUSES)
    s.add_argument("--audience")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="show one entry in full")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("research", help="deepen one entry's outline (codebase research)")
    s.add_argument("id", type=int)
    s.add_argument("--backend", default="claude", choices=["claude", "stub"],
                   help="research backend (default: claude; stub = offline)")
    s.set_defaults(func=cmd_research)

    s = sub.add_parser("status", help="set an entry's lifecycle status")
    s.add_argument("id", type=int)
    s.add_argument("new_status", help=f"one of: {', '.join(STATUSES)}")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("templates", help="list this project's audience templates")
    s.set_defaults(func=cmd_templates)

    s = sub.add_parser("digest", help="print the repo digest the planner would see")
    s.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS)
    s.set_defaults(func=cmd_digest)

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
