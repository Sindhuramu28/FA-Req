from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request, session


def bundled_path(folder: str) -> str:
    """Resolve folders both from source and from a PyInstaller executable."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, folder)


app = Flask(
    __name__,
    template_folder=bundled_path("templates"),
    static_folder=bundled_path("static"),
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ONLY", "0") == "1",
)


@dataclass(frozen=True)
class ProjectRef:
    organization: str
    project: str
    url: str


class PatVault:
    """Process-memory-only PAT storage, keyed by a random browser session id."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._values[key] = value

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._values.get(key)

    def remove(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


PAT_VAULT = PatVault()

MOCK_ITEMS = [
    {"id": 1042, "type": "Epic", "title": "Unified customer onboarding", "state": "Active", "children": 2, "selected": True},
    {"id": 1051, "type": "Feature", "title": "Identity verification", "state": "Active", "children": 2, "selected": True},
    {"id": 1058, "type": "Requirement", "title": "Verify identity documents", "state": "New", "children": 1, "selected": True},
    {"id": 1062, "type": "Test Case", "title": "Validate passport verification flow", "state": "Design", "children": 0, "selected": True},
    {"id": 1068, "type": "Feature", "title": "Customer notification preferences", "state": "Active", "children": 1, "selected": True},
    {"id": 1074, "type": "Requirement", "title": "Opt in to status notifications", "state": "Approved", "children": 0, "selected": True},
]

URL_RE = re.compile(r"^https://dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/?#]+)", re.I)


def data_directory() -> str:
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    else:
        root = os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(root, "FA-Sync-Console")
    os.makedirs(path, exist_ok=True)
    return path


DATABASE_PATH = os.path.join(data_directory(), "fa-sync.db")


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with database() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_item_mappings (
                source_organization TEXT NOT NULL,
                source_project TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                destination_organization TEXT NOT NULL,
                destination_project TEXT NOT NULL,
                destination_id INTEGER NOT NULL,
                work_item_type TEXT NOT NULL,
                last_source_revision INTEGER,
                first_synced_at TEXT NOT NULL,
                last_synced_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_error TEXT,
                PRIMARY KEY (
                    source_organization, source_project, source_id,
                    destination_organization, destination_project
                )
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                mode TEXT NOT NULL,
                source_project TEXT NOT NULL,
                destinations INTEGER NOT NULL DEFAULT 0,
                created_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL
            );
            """
        )


initialize_database()


def parse_project_url(raw_url: str) -> ProjectRef:
    url = raw_url.strip().rstrip("/")
    match = URL_RE.match(url)
    if not match:
        raise ValueError("Use an Azure DevOps project URL such as https://dev.azure.com/org/project")
    organization = urllib.parse.unquote(match.group("org"))
    project = urllib.parse.unquote(match.group("project"))
    normalized = f"https://dev.azure.com/{urllib.parse.quote(organization)}/{urllib.parse.quote(project)}"
    return ProjectRef(organization, project, normalized)


def azure_request(
    ref: ProjectRef,
    pat: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    project_scoped: bool = True,
) -> Any:
    token = base64.b64encode(f":{pat}".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    base_url = f"https://dev.azure.com/{urllib.parse.quote(ref.organization)}"
    if project_scoped:
        base_url += f"/{urllib.parse.quote(ref.project)}"
    req = urllib.request.Request(
        f"{base_url}/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "FA-Sync-Demo/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode())
            detail = body.get("message", "")
        except Exception:
            pass
        if exc.code in (401, 403):
            raise PermissionError(
                f"Authentication failed for {ref.project}. Check that the PAT is valid for "
                f"the {ref.organization} organization and that your user can access this project."
            ) from exc
        if exc.code == 404:
            raise ValueError(
                f"Project '{ref.project}' was not found in organization '{ref.organization}'."
            ) from exc
        raise RuntimeError(detail or f"Azure DevOps returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError("Azure DevOps could not be reached. Check the network and project URL.") from exc


def session_id() -> str:
    if "vault_id" not in session:
        session["vault_id"] = secrets.token_urlsafe(24)
    return session["vault_id"]


def session_pat() -> str:
    pat = PAT_VAULT.get(session_id())
    if not pat:
        raise PermissionError("Validate the project connections before loading live work items.")
    return pat


def wiql_escape(value: str) -> str:
    return value.replace("'", "''")


def load_source_work_items(ref: ProjectRef, pat: str, marker: str) -> list[dict[str, Any]]:
    # Query at organization scope with an explicit project name. This avoids
    # relying on Azure to infer @project from a copied Boards/browser URL.
    project_name = wiql_escape(ref.project)
    conditions: list[str] = [f"[System.TeamProject] = '{project_name}'"]
    if marker == "tag":
        conditions.append("[System.Tags] CONTAINS 'FA'")
    query = (
        "SELECT [System.Id] FROM WorkItems WHERE "
        + " AND ".join(conditions)
        + " ORDER BY [System.Id]"
    )
    result = azure_request(
        ref,
        pat,
        "_apis/wit/wiql?$top=1000&api-version=7.1",
        method="POST",
        payload={"query": query},
        project_scoped=False,
    )
    ids = [int(item["id"]) for item in result.get("workItems", [])]
    records: list[dict[str, Any]] = []
    for offset in range(0, len(ids), 200):
        batch = ids[offset : offset + 200]
        id_list = ",".join(str(item_id) for item_id in batch)
        path = f"_apis/wit/workitems?ids={id_list}&$expand=Relations&errorPolicy=Omit&api-version=7.1"
        response = azure_request(ref, pat, path)
        for item in response.get("value", []):
            fields = item.get("fields", {})
            child_count = sum(
                1 for relation in item.get("relations", [])
                if relation.get("rel") == "System.LinkTypes.Hierarchy-Forward"
            )
            records.append({
                "id": item["id"],
                "rev": item.get("rev"),
                "type": fields.get("System.WorkItemType", "Unknown"),
                "title": fields.get("System.Title", "Untitled"),
                "state": fields.get("System.State", ""),
                "tags": fields.get("System.Tags", ""),
                "children": child_count,
                "selected": True,
            })
    return records


@app.after_request
def secure_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/demo-items")
def demo_items():
    return jsonify({"items": MOCK_ITEMS, "mode": "demo"})


@app.post("/api/work-items")
def work_items():
    body = request.get_json(silent=True) or {}
    try:
        ref = parse_project_url(str(body.get("source", "")))
        marker = str(body.get("marker", "tag"))
        if marker not in {"tag", "manual"}:
            return jsonify({
                "ok": False,
                "message": "The first live version supports Tag: FA or Manual selection.",
            }), 400
        items = load_source_work_items(ref, session_pat(), marker)
        if not items:
            return jsonify({
                "ok": True,
                "items": [],
                "mode": "live",
                "message": (
                    f"Azure returned 0 work items for {ref.project}. Confirm that the PAT owner "
                    "can open Boards > Work Items in this project and that All work items is selected."
                ),
            })
        return jsonify({
            "ok": True,
            "items": items,
            "mode": "live",
            "message": f"Loaded {len(items)} live work items from {ref.project}.",
        })
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/connect")
def connect():
    body = request.get_json(silent=True) or {}
    pat = str(body.get("pat", "")).strip()
    source_url = str(body.get("source", ""))
    destination_urls = body.get("destinations", [])

    if not pat:
        return jsonify({"ok": False, "message": "Enter a PAT to validate live Azure DevOps access."}), 400
    if not isinstance(destination_urls, list) or not destination_urls:
        return jsonify({"ok": False, "message": "Add at least one destination project."}), 400

    try:
        projects = [("Source", parse_project_url(source_url))]
        projects.extend((f"Destination {i + 1}", parse_project_url(url)) for i, url in enumerate(destination_urls))
        results = []
        for label, ref in projects:
            project_name = urllib.parse.quote(ref.project, safe="")
            info = azure_request(
                ref,
                pat,
                f"_apis/projects/{project_name}?api-version=7.1",
                project_scoped=False,
            )
            results.append({
                "label": label,
                "name": info.get("name", ref.project),
                "status": "Connected",
                "url": ref.url,
            })
        PAT_VAULT.put(session_id(), pat)
        return jsonify({"ok": True, "message": f"Validated access to {len(results)} projects.", "projects": results})
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/disconnect")
def disconnect():
    PAT_VAULT.remove(session_id())
    return jsonify({"ok": True, "message": "PAT removed from this server session."})


@app.post("/api/preview")
def preview():
    body = request.get_json(silent=True) or {}
    selected_ids = body.get("selectedIds", [])
    destinations = body.get("destinations", [])
    fields = body.get("fields", [])
    if not selected_ids:
        return jsonify({"ok": False, "message": "Select at least one work item."}), 400
    if not destinations:
        return jsonify({"ok": False, "message": "Add at least one destination project."}), 400

    try:
        source = parse_project_url(str(body.get("source", "")))
        destination_refs = [parse_project_url(str(url)) for url in destinations]
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    mapped_pairs = 0
    with database() as db:
        for destination in destination_refs:
            placeholders = ",".join("?" for _ in selected_ids)
            row = db.execute(
                f"""SELECT COUNT(*) AS count FROM work_item_mappings
                    WHERE source_organization=? AND source_project=?
                    AND destination_organization=? AND destination_project=?
                    AND source_id IN ({placeholders})""",
                [source.organization, source.project, destination.organization,
                 destination.project, *selected_ids],
            ).fetchone()
            mapped_pairs += int(row["count"])
    total_pairs = len(selected_ids) * len(destination_refs)
    create_count = total_pairs - mapped_pairs
    update_count = mapped_pairs
    return jsonify({
        "ok": True,
        "summary": {
            "items": len(selected_ids),
            "destinations": len(destinations),
            "creates": create_count,
            "updates": update_count,
            "relationships": max(0, len(selected_ids) - 1) * len(destinations),
            "fields": len(fields),
        },
        "message": "Live mapping preview completed. No Azure DevOps data was changed.",
    })


@app.post("/api/sync")
def sync():
    body = request.get_json(silent=True) or {}
    selected_ids = body.get("selectedIds", [])
    destinations = body.get("destinations", [])
    started = time.monotonic()
    if not selected_ids or not destinations:
        return jsonify({"ok": False, "message": "Work items and destinations are required."}), 400

    # This prototype intentionally exercises the complete UI workflow without
    # creating Azure work items. Production writes require an approved field,
    # identity, conflict, deletion, and relationship mapping policy.
    entries = []
    for index, url in enumerate(destinations):
        try:
            ref = parse_project_url(url)
            entries.append({
                "destination": ref.project,
                "status": "Dry run complete",
                "created": max(1, round(len(selected_ids) * 0.67)),
                "updated": len(selected_ids) - max(1, round(len(selected_ids) * 0.67)),
                "failed": 0,
            })
        except ValueError:
            entries.append({"destination": f"Destination {index + 1}", "status": "Invalid URL", "created": 0, "updated": 0, "failed": len(selected_ids)})

    return jsonify({
        "ok": True,
        "runId": f"SYNC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        "duration": f"{max(1, round((time.monotonic() - started) * 1000))} ms",
        "entries": entries,
        "message": "Safe demo run finished. No Azure DevOps work items were created or changed.",
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    if os.environ.get("AUTO_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.1, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    app.run(
        host="127.0.0.1",
        port=port,
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        use_reloader=False,
    )
