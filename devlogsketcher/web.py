"""Local web app with full parity to the CLI.

A stdlib-only HTTP server (no extra runtime deps) that exposes every CLI capability
— init/relink, projects, run, list, show, research, status, templates (+reseed),
digest, show-prompt — as a JSON API, with `run` and `research` streaming live
progress (NDJSON) so the UI shows what the CLI prints to the terminal. The frontend
is a single static HTML file served at `/`.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import STATUSES, Entry, Store
from .digest import DEFAULT_WINDOW_DAYS, build_digest, render_digest
from .paths import (
    DevlogError,
    Project,
    find_by_name_or_id,
    find_by_repo,
    git_root,
    load_registry,
    new_project_id,
    save_registry,
)
from .planner import apply_proposals, build_planner_prompt, plan
from .research import research_entry
from .templates import load_templates, reseed_templates, seed_templates

STATIC_DIR = Path(__file__).resolve().parent / "static"


# --- serialization --------------------------------------------------------

def entry_dict(e: Entry, action: str | None = None) -> dict:
    d = {
        "id": e.id, "created_at": e.created_at, "updated_at": e.updated_at,
        "audience": e.audience, "status": e.status, "title": e.title,
        "summary": e.summary, "outline": e.outline, "source_refs": e.source_refs,
        "run_id": e.run_id,
    }
    if action:
        d["action"] = action
    return d


def project_dict(p: Project) -> dict:
    return {"id": p.id, "name": p.name, "repo_path": p.repo_path,
            "created_at": p.created_at}


# --- shared operations (mirror the CLI command bodies) --------------------

def get_project(project_id: str) -> Project:
    projects = load_registry()
    if project_id not in projects:
        raise DevlogError(f"unknown project '{project_id}'")
    return projects[project_id]


def init_project(repo_path: str, name: str | None, relink: str | None) -> Project:
    root = git_root(repo_path)
    projects = load_registry()
    existing = find_by_repo(projects, str(root))

    if relink:
        target = find_by_name_or_id(projects, relink)
        if target is None:
            raise DevlogError(f"no existing project named/id '{relink}' to relink")
        if existing is not None and existing.id != target.id:
            raise DevlogError(
                f"this repo is already linked to project '{existing.name}'; not relinking")
        target.repo_path = str(root)
        save_registry(projects)
        target.templates_dir.mkdir(parents=True, exist_ok=True)
        return target

    if existing is not None:
        raise DevlogError(
            f"repo is already linked to project '{existing.name}' ({existing.id})")

    name = name or root.name
    pid = new_project_id(name, str(root))
    project = Project(id=pid, name=name, repo_path=str(root),
                      created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    projects[pid] = project
    save_registry(projects)
    seed_templates(project.templates_dir)
    Store(project.db_path).close()
    return project


# --- HTTP handler ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DevlogSketcher"

    def log_message(self, *args):  # quieter console
        pass

    # -- helpers --
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"error": message}, status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _emit(self, obj):
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise

    def _send_static(self, name: str):
        path = STATIC_DIR / name
        if not path.is_file():
            self._error("not found", 404)
            return
        body = path.read_bytes()
        ctype = "text/html" if name.endswith(".html") else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing --
    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        try:
            self._route_get(parts, q)
        except DevlogError as e:
            self._error(str(e))
        except Exception as e:  # noqa: BLE001
            self._error(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        try:
            self._route_post(parts, q)
        except DevlogError as e:
            self._error(str(e))
        except Exception as e:  # noqa: BLE001
            self._error(f"{type(e).__name__}: {e}", 500)

    def _route_get(self, parts, q):
        if not parts:
            return self._send_static("index.html")
        if parts == ["api", "health"]:
            return self._json({"ok": True, "statuses": list(STATUSES),
                               "default_window": DEFAULT_WINDOW_DAYS})
        if parts == ["api", "projects"]:
            return self._json([project_dict(p) for p in load_registry().values()])

        # /api/projects/{id}/...
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "projects":
            project = get_project(parts[2])
            sub = parts[3:]
            if sub == ["entries"]:
                store = Store(project.db_path)
                entries = store.list_entries(
                    status=(q.get("status", [None])[0] or None),
                    audience=(q.get("audience", [None])[0] or None))
                store.close()
                return self._json([entry_dict(e) for e in entries])
            if len(sub) == 2 and sub[0] == "entries":
                store = Store(project.db_path)
                e = store.get_entry(int(sub[1]))
                store.close()
                if e is None:
                    return self._error(f"no entry #{sub[1]}", 404)
                return self._json(entry_dict(e))
            if sub == ["templates"]:
                tpls = load_templates(project.templates_dir)
                return self._json([{"audience": t.audience, "body": t.body} for t in tpls])
            if sub == ["digest"]:
                window = int(q.get("window", [DEFAULT_WINDOW_DAYS])[0])
                digest = build_digest(project.repo_path, window)
                return self._json({"window": window, "num_commits": digest.num_commits,
                                   "text": render_digest(digest)})
            if sub == ["prompt"]:
                window = int(q.get("window", [DEFAULT_WINDOW_DAYS])[0])
                digest = build_digest(project.repo_path, window)
                store = Store(project.db_path)
                existing = store.list_entries()
                store.close()
                templates = load_templates(project.templates_dir)
                return self._json({"prompt": build_planner_prompt(digest, existing, templates)})
        return self._error("not found", 404)

    def _route_post(self, parts, q):
        if parts == ["api", "projects"]:
            body = self._read_body()
            project = init_project(body.get("repo_path", "."), body.get("name"),
                                   body.get("relink"))
            return self._json(project_dict(project))

        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "projects":
            project = get_project(parts[2])
            sub = parts[3:]
            if sub == ["run"]:
                body = self._read_body()
                return self._stream_run(
                    project,
                    int(body.get("window", DEFAULT_WINDOW_DAYS)),
                    body.get("backend", "claude"))
            if len(sub) == 2 and sub[0] == "research":
                body = self._read_body()
                return self._stream_research(project, int(sub[1]),
                                             body.get("backend", "claude"))
            if len(sub) == 3 and sub[0] == "entries" and sub[2] == "status":
                body = self._read_body()
                new_status = body.get("status")
                if new_status not in STATUSES:
                    return self._error(f"invalid status '{new_status}'")
                store = Store(project.db_path)
                e = store.get_entry(int(sub[1]))
                if e is None:
                    store.close()
                    return self._error(f"no entry #{sub[1]}", 404)
                store.update_entry(int(sub[1]), status=new_status)
                e = store.get_entry(int(sub[1]))
                store.close()
                return self._json(entry_dict(e))
            if sub == ["templates", "reseed"]:
                results = reseed_templates(project.templates_dir)
                return self._json([{"name": n, "action": a} for n, a in results])
        return self._error("not found", 404)

    # -- streaming operations --
    def _stream_run(self, project: Project, window: int, backend: str):
        self._stream_start()
        try:
            self._emit({"type": "progress", "message": f"Scanning git history (last {window} days)…"})
            digest = build_digest(project.repo_path, window)
            self._emit({"type": "progress", "message": f"{digest.num_commits} commit(s) in window."})
            if digest.num_commits == 0:
                return self._emit({"type": "done", "entries": [],
                                   "summary": {"created": 0, "updated": 0, "run_id": None},
                                   "message": "No commits in window — nothing to plan."})
            store = Store(project.db_path)
            templates = load_templates(project.templates_dir)
            existing = store.list_entries()
            run_id = store.create_run(window_days=digest.window_days, since=digest.since,
                                      num_commits=digest.num_commits)
            self._emit({"type": "progress", "message": f"Reviewing against {len(existing)} existing idea(s)."})
            label = "heuristic stub (offline)" if backend == "heuristic" else "Claude (Opus 4.8)"
            self._emit({"type": "progress", "message": f"Planning with {label} — this can take a minute…"})
            proposals = plan(digest, store, templates, backend=backend)
            results = apply_proposals(store, proposals, run_id)
            entries = [entry_dict(store.get_entry(eid), action) for eid, action in results]
            store.close()
            created = sum(1 for _, a in results if a == "created")
            updated = sum(1 for _, a in results if a == "updated")
            self._emit({"type": "done", "entries": entries,
                        "summary": {"created": created, "updated": updated, "run_id": run_id}})
        except DevlogError as e:
            self._emit({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            self._emit({"type": "error", "message": f"{type(e).__name__}: {e}"})

    def _stream_research(self, project: Project, entry_id: int, backend: str):
        self._stream_start()
        store = Store(project.db_path)
        try:
            target = store.get_entry(entry_id)
            if target is None:
                return self._emit({"type": "error", "message": f"no entry #{entry_id}"})
            self._emit({"type": "progress", "message": f"Researching #{target.id}: {target.title}"})

            def progress(msg):
                self._emit({"type": "progress", "message": msg})

            entry = research_entry(store, project, entry_id, backend=backend, progress=progress)
            self._emit({"type": "done", "entry": entry_dict(entry)})
        except (DevlogError, ValueError) as e:
            self._emit({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            self._emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            store.close()


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"DevlogSketcher web app running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
