"""Local web app with full parity to the CLI.

A stdlib-only HTTP server (no extra runtime deps) that exposes every CLI capability
— init/relink, projects, run, list, show, research, status, templates (list / create
/ edit / delete / reseed), planner-prompt (view / edit / reset), digest, show-prompt
— as a JSON API, with `run` and `research` streaming live progress (NDJSON) so the UI
shows what the CLI prints to the terminal. The frontend is a single static HTML file
served at `/`.

The planning `run` is resumable: it executes in a background thread (one at a time,
tracked by ``RUNS``) that outlives the HTTP connection, so a reload or disconnect
doesn't kill it. Clients reconnect via GET /api/run (status + buffered events) and
GET /api/run/stream?since=N (replay from N, then follow live).

When any access key exists (`devlog keys add`), every API route except health and
login/logout requires a valid key, presented via an HttpOnly cookie set at sign-in;
with no keys configured the server stays open (localhost-only personal use).
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .auth import auth_enabled, load_keys, verify_key
from .branches import (
    delete_branch_note,
    get_branch_note,
    load_branch_notes,
    set_branch_note,
)
from .db import STATUSES, Entry, Store
from .digest import (
    DEFAULT_WINDOW_DAYS,
    build_digest,
    current_branch,
    list_branches,
    render_digest,
)
from .export import EXPORT_FORMATS, MIME, export_filename, render_entry
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
from .planner import (
    PLANNER_PLACEHOLDERS,
    DEFAULT_PLANNER_PROMPT,
    apply_proposals,
    build_planner_prompt,
    load_planner_prompt_template,
    plan,
    planner_prompt_is_custom,
    reset_planner_prompt_template,
    save_planner_prompt_template,
)
from .research import research_entry
from .templates import (
    delete_template,
    load_templates,
    reseed_templates,
    save_template,
    seed_templates,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RunJob:
    """A single, resumable planning run. Progress events accumulate in a buffer so a
    client that reloads or reconnects can replay from any index and then follow live —
    the work runs in a daemon thread that outlives any one HTTP connection."""

    def __init__(self, job_id: int, project_id: str, params: dict):
        self.id = job_id
        self.project_id = project_id
        self.params = params
        self.events: list[dict] = []
        self.done = False
        self.started_at = time.time()
        self._cond = threading.Condition()

    def emit(self, event: dict) -> None:
        with self._cond:
            self.events.append(event)
            if event.get("type") in ("done", "error"):
                self.done = True
            self._cond.notify_all()

    def follow(self, since: int = 0, heartbeat: float = 10.0):
        """Yield buffered events from index ``since`` onward, then live ones as they
        arrive; yield ``None`` as a heartbeat when idle so the caller can keep the
        connection warm and notice client disconnects. Returns once the job is done
        and the caller has caught up."""
        index = max(0, since)
        while True:
            batch: list[dict] = []
            beat = False
            with self._cond:
                while index >= len(self.events) and not self.done:
                    if not self._cond.wait(timeout=heartbeat):
                        beat = True
                        break
                if index < len(self.events):
                    batch = self.events[index:]
                    index += len(batch)
                elif self.done:
                    return
            for ev in batch:
                yield ev
            if beat:
                yield None

    def snapshot(self) -> dict:
        with self._cond:
            return {"id": self.id, "project_id": self.project_id,
                    "params": self.params, "done": self.done,
                    "running": not self.done, "started_at": self.started_at,
                    "events": list(self.events)}


class RunManager:
    """One planning run at a time across the whole server. Dispatching while a run is
    in flight returns the existing job instead of starting a second."""

    def __init__(self):
        self._lock = threading.Lock()
        self._job: RunJob | None = None
        self._seq = 0

    def current(self) -> RunJob | None:
        with self._lock:
            return self._job

    def start(self, project, params, worker) -> tuple[RunJob, bool]:
        """Start a new job, or return the in-flight one. The bool is True when a new
        job was started, False when an already-running job was attached to."""
        with self._lock:
            if self._job is not None and not self._job.done:
                return self._job, False
            self._seq += 1
            job = RunJob(self._seq, project.id, params)
            self._job = job
        threading.Thread(target=worker, args=(job, project, params), daemon=True).start()
        return job, True


RUNS = RunManager()


def _plan_worker(job: RunJob, project: Project, params: dict) -> None:
    """Run the planner end to end, recording progress on ``job``. Runs in a daemon
    thread so the run survives client disconnects and page reloads."""
    window = params["window"]
    backend = params["backend"]
    branch = params.get("branch")
    target = params.get("target", 0)
    try:
        digest = build_digest(project.repo_path, window, ref=branch)
        job.emit({"type": "progress",
                  "message": f"Scanning `{digest.branch}` (last {window} days)…"})
        job.emit({"type": "progress",
                  "message": f"{digest.num_commits} commit(s) in window."})
        if digest.num_commits == 0:
            return job.emit({"type": "done", "entries": [],
                             "summary": {"created": 0, "updated": 0, "run_id": None},
                             "message": "No commits in window — nothing to plan."})
        store = Store(project.db_path)
        try:
            templates = load_templates(project.templates_dir)
            existing = store.list_entries()
            run_id = store.create_run(window_days=digest.window_days, since=digest.since,
                                      num_commits=digest.num_commits, branch=digest.branch)
            job.emit({"type": "progress",
                      "message": f"Reviewing against {len(existing)} existing idea(s)."})
            label = "heuristic stub (offline)" if backend == "heuristic" else "Claude (Opus 4.8)"
            target_note = f" (target: {target} new)" if target else ""
            job.emit({"type": "progress",
                      "message": f"Planning with {label}{target_note} — this can take a minute…"})
            prompt_template = load_planner_prompt_template(project)
            branch_note = get_branch_note(project, digest.branch)
            proposals = plan(digest, store, templates, backend=backend,
                             prompt_template=prompt_template, branch_note=branch_note,
                             target=target,
                             progress=lambda msg: job.emit({"type": "progress", "message": msg}))
            results = apply_proposals(store, proposals, run_id, branch=digest.branch)
            entries = [entry_dict(store.get_entry(eid), action) for eid, action in results]
            created = sum(1 for _, a in results if a == "created")
            updated = sum(1 for _, a in results if a == "updated")
            job.emit({"type": "done", "entries": entries,
                      "summary": {"created": created, "updated": updated, "run_id": run_id}})
        finally:
            store.close()
    except DevlogError as e:
        job.emit({"type": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        job.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})


def _valid_date(s: str) -> bool:
    try:
        time.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


# --- serialization --------------------------------------------------------

def entry_dict(e: Entry, action: str | None = None) -> dict:
    d = {
        "id": e.id, "created_at": e.created_at, "updated_at": e.updated_at,
        "audience": e.audience, "status": e.status, "title": e.title,
        "summary": e.summary, "outline": e.outline, "source_refs": e.source_refs,
        "run_id": e.run_id, "branch": e.branch, "scheduled_for": e.scheduled_for,
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
    def _json(self, obj, status=200, cookies=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for c in (cookies or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    # -- auth --
    AUTH_COOKIE = "devlog_key"

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except Exception:  # noqa: BLE001 — malformed cookie header
            return None
        m = jar.get(name)
        return m.value if m else None

    def _current_key(self):
        return verify_key(self._cookie(self.AUTH_COOKIE) or "")

    def _auth_cookie(self, raw, clear=False):
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        attrs = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")
        if clear:
            return f"{self.AUTH_COOKIE}=; Max-Age=0; {attrs}"
        return f"{self.AUTH_COOKIE}={raw}; Max-Age=2592000; {attrs}"

    def _is_public(self, method, parts):
        """Routes reachable without a key: the SPA shell, health, and login/logout."""
        if method == "GET":
            return not parts or parts == ["api", "health"]
        if method == "POST":
            return parts in (["api", "login"], ["api", "logout"])
        return False

    def _require_auth(self, method, parts) -> bool:
        if not auth_enabled() or self._is_public(method, parts):
            return True
        if self._current_key() is not None:
            return True
        self._error("authentication required", 401)
        return False

    def _login(self):
        body = self._read_body()
        key = verify_key(body.get("key", ""))
        if key is None:
            return self._error("invalid access key", 401)
        return self._json({"ok": True, "label": key.label},
                          cookies=[self._auth_cookie(body["key"])])

    def _logout(self):
        return self._json({"ok": True}, cookies=[self._auth_cookie("", clear=True)])

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

    def _send_download(self, content: str, filename: str, ctype: str):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            if not self._require_auth("GET", parts):
                return
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
            if parts == ["api", "login"]:
                return self._login()
            if parts == ["api", "logout"]:
                return self._logout()
            if not self._require_auth("POST", parts):
                return
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
                               "default_window": DEFAULT_WINDOW_DAYS,
                               "auth_required": auth_enabled(),
                               "authed": self._current_key() is not None})
        if parts == ["api", "projects"]:
            return self._json([project_dict(p) for p in load_registry().values()])
        if parts == ["api", "run"]:
            job = RUNS.current()
            return self._json({"running": bool(job and not job.done),
                               "job": job.snapshot() if job else None})
        if parts == ["api", "run", "stream"]:
            job = RUNS.current()
            if job is None:
                self._stream_start()
                return self._emit({"type": "idle"})
            return self._stream_job(job, since=int(q.get("since", ["0"])[0]))

        # /api/projects/{id}/...
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "projects":
            project = get_project(parts[2])
            sub = parts[3:]
            if sub == ["entries"]:
                store = Store(project.db_path)
                entries = store.list_entries(
                    status=(q.get("status", [None])[0] or None),
                    audience=(q.get("audience", [None])[0] or None),
                    branch=(q.get("branch", [None])[0] or None))
                store.close()
                return self._json([entry_dict(e) for e in entries])
            if sub == ["branches"]:
                branches = list_branches(project.repo_path)
                return self._json({"branches": branches,
                                   "current": current_branch(project.repo_path),
                                   "notes": load_branch_notes(project)})
            if len(sub) == 2 and sub[0] == "branches":
                return self._json({"branch": sub[1],
                                   "note": get_branch_note(project, sub[1])})
            if len(sub) == 2 and sub[0] == "entries":
                store = Store(project.db_path)
                e = store.get_entry(int(sub[1]))
                store.close()
                if e is None:
                    return self._error(f"no entry #{sub[1]}", 404)
                return self._json(entry_dict(e))
            if len(sub) == 3 and sub[0] == "entries" and sub[2] == "export":
                fmt = q.get("format", ["md"])[0]
                if fmt not in EXPORT_FORMATS:
                    return self._error(f"unknown export format '{fmt}'")
                store = Store(project.db_path)
                e = store.get_entry(int(sub[1]))
                store.close()
                if e is None:
                    return self._error(f"no entry #{sub[1]}", 404)
                return self._send_download(
                    render_entry(e, fmt), export_filename(e, fmt), MIME[fmt])
            if sub == ["templates"]:
                tpls = load_templates(project.templates_dir)
                return self._json([{"audience": t.audience, "body": t.body} for t in tpls])
            if sub == ["digest"]:
                window = int(q.get("window", [DEFAULT_WINDOW_DAYS])[0])
                ref = q.get("branch", [None])[0] or None
                digest = build_digest(project.repo_path, window, ref=ref)
                return self._json({"window": window, "num_commits": digest.num_commits,
                                   "branch": digest.branch, "text": render_digest(digest)})
            if sub == ["prompt"]:
                window = int(q.get("window", [DEFAULT_WINDOW_DAYS])[0])
                ref = q.get("branch", [None])[0] or None
                digest = build_digest(project.repo_path, window, ref=ref)
                store = Store(project.db_path)
                existing = store.list_entries()
                store.close()
                templates = load_templates(project.templates_dir)
                template_text = load_planner_prompt_template(project)
                note = get_branch_note(project, digest.branch)
                return self._json({"prompt": build_planner_prompt(
                    digest, existing, templates, template_text, note)})
            if sub == ["planner-prompt"]:
                return self._json({
                    "prompt": load_planner_prompt_template(project),
                    "is_custom": planner_prompt_is_custom(project),
                    "default": DEFAULT_PLANNER_PROMPT,
                    "placeholders": list(PLANNER_PLACEHOLDERS),
                })
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
                    body.get("backend", "claude"),
                    body.get("branch") or None,
                    max(0, int(body.get("target", 0) or 0)))
            if sub == ["unlink"]:
                projects = load_registry()
                if project.id in projects:
                    del projects[project.id]
                    save_registry(projects)
                return self._json({"unlinked": project.id})
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
            if len(sub) == 3 and sub[0] == "entries" and sub[2] == "schedule":
                body = self._read_body()
                date = (body.get("scheduled_for") or "").strip()
                if date and not _valid_date(date):
                    return self._error("scheduled_for must be YYYY-MM-DD or empty")
                store = Store(project.db_path)
                if store.get_entry(int(sub[1])) is None:
                    store.close()
                    return self._error(f"no entry #{sub[1]}", 404)
                store.update_entry(int(sub[1]), scheduled_for=date)
                e = store.get_entry(int(sub[1]))
                store.close()
                return self._json(entry_dict(e))
            if len(sub) == 3 and sub[0] == "entries" and sub[2] == "delete":
                store = Store(project.db_path)
                ok = store.delete_entry(int(sub[1]))
                store.close()
                if not ok:
                    return self._error(f"no entry #{sub[1]}", 404)
                return self._json({"deleted": int(sub[1])})
            if sub == ["entries", "clear"]:
                store = Store(project.db_path)
                n = store.clear_entries()
                store.close()
                return self._json({"cleared": n})
            if len(sub) == 2 and sub[0] == "branches":
                body = self._read_body()
                set_branch_note(project, sub[1], body.get("note", ""))
                return self._json({"branch": sub[1],
                                   "note": get_branch_note(project, sub[1])})
            if len(sub) == 3 and sub[0] == "branches" and sub[2] == "delete":
                delete_branch_note(project, sub[1])
                return self._json({"branch": sub[1], "note": ""})
            if sub == ["templates", "reseed"]:
                results = reseed_templates(project.templates_dir)
                return self._json([{"name": n, "action": a} for n, a in results])
            if sub == ["templates"]:
                body = self._read_body()
                t = save_template(project.templates_dir,
                                  (body.get("audience") or "").strip(),
                                  body.get("body", ""))
                return self._json({"audience": t.audience, "body": t.body})
            if len(sub) == 3 and sub[0] == "templates" and sub[2] == "delete":
                if not delete_template(project.templates_dir, sub[1]):
                    return self._error(f"no template '{sub[1]}'", 404)
                return self._json({"deleted": sub[1]})
            if sub == ["planner-prompt"]:
                body = self._read_body()
                save_planner_prompt_template(project, body.get("prompt", ""))
                return self._json({"ok": True, "is_custom": True})
            if sub == ["planner-prompt", "reset"]:
                reset_planner_prompt_template(project)
                return self._json({"ok": True, "is_custom": False,
                                   "prompt": DEFAULT_PLANNER_PROMPT})
        return self._error("not found", 404)

    # -- streaming operations --
    def _stream_run(self, project: Project, window: int, backend: str,
                    branch: str | None = None, target: int = 0):
        """Dispatch a planning run (or attach to the one already in flight) and stream
        its progress. The run itself executes in a background thread via RUNS, so a
        dropped connection or page reload doesn't kill it — the client just reconnects
        through GET /api/run + /api/run/stream."""
        current = RUNS.current()
        if current is not None and not current.done and current.project_id != project.id:
            self._stream_start()
            return self._emit({"type": "error",
                               "message": "a planning run is already in progress for "
                                          "another project — wait for it to finish."})
        params = {"window": window, "backend": backend, "branch": branch, "target": target}
        job, _started = RUNS.start(project, params, _plan_worker)
        self._stream_job(job, since=0)

    def _stream_job(self, job: RunJob, since: int = 0):
        """Stream a job's events from ``since`` to the client until it finishes. If the
        client disconnects, the worker thread keeps running — we just stop writing."""
        self._stream_start()
        try:
            for ev in job.follow(since=since):
                self._emit({"type": "heartbeat"} if ev is None else ev)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; the background run is unaffected

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

    loopback = host in ("127.0.0.1", "::1", "localhost", "")
    if auth_enabled():
        print(f"Access-key auth is ON ({len(load_keys())} key(s)). "
              "Users sign in with a key from `devlog keys add`.")
        if not loopback:
            print("Note: this server speaks plain HTTP — put it behind an HTTPS "
                  "tunnel/proxy so keys aren't sent in the clear.")
    elif not loopback:
        print("\n  ⚠  WARNING: bound to a non-loopback address with NO access keys.")
        print("     Anyone who can reach this port has full access.")
        print("     Run `devlog keys add` to require sign-in, then restart.\n")
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
