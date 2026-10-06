from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
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
CREDENTIAL_TARGET = "SyncWorkTrack PAT"
LEGACY_CREDENTIAL_TARGET = "Azure WorkSync PAT"
DESTINATION_TAG = "FA-Synced"


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def save_os_credential(pat: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Secure PAT storage is available only on Windows.")
    blob = pat.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob)
    credential = CREDENTIALW()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = CREDENTIAL_TARGET
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = "Azure DevOps PAT"
    if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError()


def read_windows_credential(target: str) -> str | None:
    if sys.platform != "win32":
        return None
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not ctypes.windll.advapi32.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        ctypes.windll.advapi32.CredFree(pointer)


def load_os_credential() -> str | None:
    return read_windows_credential(CREDENTIAL_TARGET) or read_windows_credential(LEGACY_CREDENTIAL_TARGET)


def delete_os_credential() -> None:
    if sys.platform == "win32":
        ctypes.windll.advapi32.CredDeleteW(CREDENTIAL_TARGET, 1, 0)
        ctypes.windll.advapi32.CredDeleteW(LEGACY_CREDENTIAL_TARGET, 1, 0)

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
            CREATE TABLE IF NOT EXISTS synced_comments (
                source_organization TEXT NOT NULL,
                source_project TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                source_comment_id INTEGER NOT NULL,
                destination_organization TEXT NOT NULL,
                destination_project TEXT NOT NULL,
                destination_id INTEGER NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (
                    source_organization, source_project, source_id, source_comment_id,
                    destination_organization, destination_project
                )
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


def validate_project_direction(source: ProjectRef, destinations: list[ProjectRef]) -> None:
    source_key = (source.organization.casefold(), source.project.casefold())
    destination_keys = [
        (item.organization.casefold(), item.project.casefold()) for item in destinations
    ]
    if source_key in destination_keys:
        raise ValueError("The source project cannot also be a destination project.")
    if len(destination_keys) != len(set(destination_keys)):
        raise ValueError("Each destination project can be added only once.")


def azure_request(
    ref: ProjectRef,
    pat: str,
    path: str,
    *,
    method: str = "GET",
    payload: Any | None = None,
    project_scoped: bool = True,
    content_type: str = "application/json",
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
            "Content-Type": content_type,
            "User-Agent": "SyncWorkTrack/1.0",
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
                f"the {ref.organization} organization, has Work Items: Read & write scope, "
                f"and that your user can access this project."
            ) from exc
        if exc.code == 404:
            raise ValueError(detail or f"Azure resource was not found in project '{ref.project}'.") from exc
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


def fetch_work_items(ref: ProjectRef, pat: str, ids: list[int]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for offset in range(0, len(ids), 200):
        batch = ids[offset : offset + 200]
        id_list = ",".join(str(item_id) for item_id in batch)
        response = azure_request(
            ref, pat,
            f"_apis/wit/workitems?ids={id_list}&$expand=Relations&errorPolicy=Omit&api-version=7.1",
        )
        records.extend(response.get("value", []))
    return records


FIELD_MAP = {
    "Title": "System.Title",
    "Description": "System.Description",
    "Tags": "System.Tags",
}


def impact_field_reference(fields: dict[str, Any]) -> str | None:
    for reference_name in fields:
        normalized = re.sub(r"[^a-z]", "", reference_name.casefold())
        if "impactassessment" in normalized:
            return reference_name
    return None


def work_item_patch(
    item: dict[str, Any], selected_fields: list[str], *, include_hyperlinks: bool = True,
    require_title: bool = True,
) -> list[dict[str, Any]]:
    source_fields = item.get("fields", {})
    operations: list[dict[str, Any]] = []
    chosen = set(selected_fields)
    if require_title:
        chosen.add("Title")
    for label, reference_name in FIELD_MAP.items():
        if label not in chosen or reference_name not in source_fields:
            continue
        operations.append({
            "op": "add",
            "path": f"/fields/{reference_name}",
            "value": source_fields[reference_name],
        })
    if "Impact assessment" in chosen:
        reference_name = impact_field_reference(source_fields)
        if reference_name:
            operations.append({
                "op": "add",
                "path": f"/fields/{reference_name}",
                "value": source_fields[reference_name],
            })
    tags_operation = next(
        (operation for operation in operations if operation["path"] == "/fields/System.Tags"),
        None,
    )
    source_tags = str(tags_operation["value"] if tags_operation else "")
    tag_values = [value.strip() for value in source_tags.split(";") if value.strip()]
    if DESTINATION_TAG.casefold() not in {value.casefold() for value in tag_values}:
        tag_values.append(DESTINATION_TAG)
    if tags_operation:
        tags_operation["value"] = "; ".join(tag_values)
    else:
        operations.append({
            "op": "add", "path": "/fields/System.Tags", "value": "; ".join(tag_values)
        })
    if "Hyperlinks" in chosen and include_hyperlinks:
        for relation in item.get("relations", []):
            if relation.get("rel") == "Hyperlink" and relation.get("url"):
                operations.append({
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "Hyperlink",
                        "url": relation["url"],
                        "attributes": {"comment": "Copied by SyncWorkTrack"},
                    },
                })
    return operations


def save_mapping(
    source: ProjectRef,
    destination: ProjectRef,
    source_item: dict[str, Any],
    destination_id: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    fields = source_item.get("fields", {})
    with database() as db:
        db.execute(
            """INSERT INTO work_item_mappings (
                source_organization, source_project, source_id,
                destination_organization, destination_project, destination_id,
                work_item_type, last_source_revision, first_synced_at,
                last_synced_at, status, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)
            ON CONFLICT(source_organization, source_project, source_id,
                        destination_organization, destination_project)
            DO UPDATE SET destination_id=excluded.destination_id,
                          work_item_type=excluded.work_item_type,
                          last_source_revision=excluded.last_source_revision,
                          last_synced_at=excluded.last_synced_at,
                          status='active', last_error=NULL""",
            (source.organization, source.project, int(source_item["id"]),
             destination.organization, destination.project, destination_id,
             fields.get("System.WorkItemType", "Unknown"), source_item.get("rev"),
             now, now),
        )


def related_work_item_id(url: str) -> int | None:
    match = re.search(r"/workItems/(\d+)(?:\?.*)?$", url, re.I)
    return int(match.group(1)) if match else None


def destination_mappings(
    source: ProjectRef, destination: ProjectRef, source_ids: list[int]
) -> dict[int, int]:
    if not source_ids:
        return {}
    placeholders = ",".join("?" for _ in source_ids)
    with database() as db:
        rows = db.execute(
            f"""SELECT source_id, destination_id FROM work_item_mappings
                WHERE source_organization=? AND source_project=?
                AND destination_organization=? AND destination_project=?
                AND source_id IN ({placeholders})""",
            (source.organization, source.project, destination.organization,
             destination.project, *source_ids),
        ).fetchall()
    return {int(row["source_id"]): int(row["destination_id"]) for row in rows}


def synchronize_links(
    source: ProjectRef,
    destination: ProjectRef,
    pat: str,
    source_items: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    selected_ids = [int(item["id"]) for item in source_items]
    related_ids = {
        target_id for item in source_items for relation in item.get("relations", [])
        if (target_id := related_work_item_id(str(relation.get("url", "")))) is not None
    }
    mappings = destination_mappings(source, destination, selected_ids + sorted(related_ids))
    copied = 0
    errors: list[str] = []
    for item in source_items:
        source_id = int(item["id"])
        destination_id = mappings.get(source_id)
        if not destination_id:
            continue
        candidates = []
        for relation in item.get("relations", []):
            relation_type = str(relation.get("rel", ""))
            target_source_id = related_work_item_id(str(relation.get("url", "")))
            if not relation_type.startswith("System.LinkTypes.") or target_source_id not in mappings:
                continue
            candidates.append((relation_type, mappings[target_source_id]))
        if not candidates:
            continue
        try:
            current = azure_request(
                destination, pat,
                f"_apis/wit/workitems/{destination_id}?$expand=Relations&api-version=7.1",
            )
            existing = {
                (str(link.get("rel", "")), related_work_item_id(str(link.get("url", ""))))
                for link in current.get("relations", [])
            }
            patch = []
            for relation_type, target_destination_id in candidates:
                if (relation_type, target_destination_id) in existing:
                    continue
                patch.append({
                    "op": "add", "path": "/relations/-",
                    "value": {
                        "rel": relation_type,
                        "url": (
                            f"https://dev.azure.com/{urllib.parse.quote(destination.organization)}"
                            f"/{urllib.parse.quote(destination.project)}/_apis/wit/workItems/"
                            f"{target_destination_id}"
                        ),
                        "attributes": {"comment": "Copied by SyncWorkTrack"},
                    },
                })
            if patch:
                azure_request(
                    destination, pat,
                    f"_apis/wit/workitems/{destination_id}?api-version=7.1",
                    method="PATCH", payload=patch,
                    content_type="application/json-patch+json",
                )
                copied += len(patch)
        except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
            errors.append(f"Links for source {source_id}: {exc}")
    return copied, errors


def synchronize_comments(
    source: ProjectRef,
    destination: ProjectRef,
    pat: str,
    source_item: dict[str, Any],
    destination_id: int,
) -> tuple[int, list[str]]:
    source_id = int(source_item["id"])
    copied = 0
    errors: list[str] = []
    try:
        response = azure_request(
            source, pat,
            f"_apis/wit/workItems/{source_id}/comments?$top=200&api-version=7.1-preview.4",
        )
        for comment in response.get("comments", response.get("value", [])):
            comment_id = int(comment["id"])
            with database() as db:
                exists = db.execute(
                    """SELECT 1 FROM synced_comments WHERE source_organization=?
                       AND source_project=? AND source_id=? AND source_comment_id=?
                       AND destination_organization=? AND destination_project=?""",
                    (source.organization, source.project, source_id, comment_id,
                     destination.organization, destination.project),
                ).fetchone()
            if exists:
                continue
            author = comment.get("createdBy", {}).get("displayName", "Source user")
            created = comment.get("createdDate", "")
            text = f"Copied from source comment by {author} ({created})\n\n{comment.get('text', '')}"
            azure_request(
                destination, pat,
                f"_apis/wit/workItems/{destination_id}/comments?format=markdown&api-version=7.1-preview.4",
                method="POST", payload={"text": text},
            )
            with database() as db:
                db.execute(
                    """INSERT OR IGNORE INTO synced_comments VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source.organization, source.project, source_id, comment_id,
                     destination.organization, destination.project, destination_id,
                     datetime.now(timezone.utc).isoformat()),
                )
            copied += 1
    except (ValueError, PermissionError, ConnectionError, RuntimeError, KeyError) as exc:
        errors.append(f"Discussions for source {source_id}: {exc}")
    return copied, errors


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
    except (ValueError, PermissionError, ConnectionError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/connect")
def connect():
    body = request.get_json(silent=True) or {}
    pat = str(body.get("pat", "")).strip()
    remember_pat = body.get("rememberPat") is True
    if not pat and body.get("useStoredPat") is True:
        pat = load_os_credential() or ""
    source_url = str(body.get("source", ""))
    destination_urls = body.get("destinations", [])

    if not pat:
        return jsonify({"ok": False, "message": "Enter a PAT to validate live Azure DevOps access."}), 400
    if not isinstance(destination_urls, list) or not destination_urls:
        return jsonify({"ok": False, "message": "Add at least one destination project."}), 400

    try:
        source_ref = parse_project_url(source_url)
        destination_refs = [parse_project_url(url) for url in destination_urls]
        validate_project_direction(source_ref, destination_refs)
        projects = [("Source", source_ref)]
        projects.extend((f"Destination {i + 1}", ref) for i, ref in enumerate(destination_refs))
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
        if remember_pat:
            save_os_credential(pat)
        return jsonify({"ok": True, "message": f"Validated access to {len(results)} projects.", "projects": results})
    except (ValueError, PermissionError, ConnectionError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/disconnect")
def disconnect():
    PAT_VAULT.remove(session_id())
    if (request.get_json(silent=True) or {}).get("deleteStoredPat") is True:
        delete_os_credential()
        return jsonify({"ok": True, "message": "Session PAT and saved Windows credential were removed."})
    return jsonify({"ok": True, "message": "PAT removed from this app session."})


@app.get("/api/credential-status")
def credential_status():
    return jsonify({"stored": load_os_credential() is not None})


@app.post("/api/preview")
def preview():
    body = request.get_json(silent=True) or {}
    selected_ids = body.get("selectedIds", [])
    selected_items = body.get("selectedItems", [])
    destinations = body.get("destinations", [])
    fields = body.get("fields", [])
    if not selected_ids:
        return jsonify({"ok": False, "message": "Select at least one work item."}), 400
    if not destinations:
        return jsonify({"ok": False, "message": "Add at least one destination project."}), 400

    try:
        source = parse_project_url(str(body.get("source", "")))
        destination_refs = [parse_project_url(str(url)) for url in destinations]
        validate_project_direction(source, destination_refs)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    item_details = {
        int(item.get("id")): {
            "title": str(item.get("title", "Untitled")),
            "type": str(item.get("type", "Unknown")),
        }
        for item in selected_items if item.get("id") is not None
    }
    mapped_pairs = 0
    changes: list[dict[str, Any]] = []
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
            mapping_rows = db.execute(
                f"""SELECT source_id, destination_id FROM work_item_mappings
                    WHERE source_organization=? AND source_project=?
                    AND destination_organization=? AND destination_project=?
                    AND source_id IN ({placeholders})""",
                [source.organization, source.project, destination.organization,
                 destination.project, *selected_ids],
            ).fetchall()
            mapped = {int(item["source_id"]): int(item["destination_id"]) for item in mapping_rows}
            for source_id in selected_ids:
                numeric_id = int(source_id)
                details = item_details.get(numeric_id, {})
                destination_id = mapped.get(numeric_id)
                changes.append({
                    "sourceId": numeric_id,
                    "title": details.get("title", "Untitled"),
                    "type": details.get("type", "Unknown"),
                    "destination": destination.project,
                    "destinationId": destination_id,
                    "action": "Will update" if destination_id is not None else "Will create",
                })
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
            "relationships": 0,
            "fields": len(fields),
        },
        "changes": changes,
        "message": "Live mapping preview completed. No Azure DevOps data was changed.",
    })


@app.post("/api/sync")
def sync():
    body = request.get_json(silent=True) or {}
    try:
        selected_ids = [int(item_id) for item_id in body.get("selectedIds", [])]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Work item IDs must be valid numbers."}), 400
    destinations = body.get("destinations", [])
    selected_fields = [str(field) for field in body.get("fields", [])]
    started = time.monotonic()
    if not selected_ids or not destinations:
        return jsonify({"ok": False, "message": "Work items and destinations are required."}), 400
    if body.get("liveWrites") is not True or body.get("confirmation") != "SYNC":
        return jsonify({
            "ok": False,
            "message": "Enable live writes and confirm the synchronization before running it.",
        }), 400

    run_id = f"SYNC-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        pat = session_pat()
        source = parse_project_url(str(body.get("source", "")))
        destination_refs = [parse_project_url(str(url)) for url in destinations]
        validate_project_direction(source, destination_refs)
        source_items = fetch_work_items(source, pat, selected_ids)
        found_ids = {int(item["id"]) for item in source_items}
        missing_ids = sorted(set(selected_ids) - found_ids)
        if missing_ids:
            raise ValueError(f"Source work items could not be loaded: {', '.join(map(str, missing_ids))}.")
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    with database() as db:
        db.execute(
            """INSERT INTO sync_runs
               (run_id, started_at, mode, source_project, destinations, status)
               VALUES (?, ?, 'live', ?, ?, 'running')""",
            (run_id, started_at, source.project, len(destination_refs)),
        )

    entries: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    for destination in destination_refs:
        destination_totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        destination_warnings: list[str] = []
        comments_copied = 0
        for item in source_items:
            source_id = int(item["id"])
            source_fields = item.get("fields", {})
            title = source_fields.get("System.Title", "Untitled")
            item_type = source_fields.get("System.WorkItemType", "Unknown")
            with database() as db:
                mapping = db.execute(
                    """SELECT destination_id FROM work_item_mappings
                       WHERE source_organization=? AND source_project=? AND source_id=?
                       AND destination_organization=? AND destination_project=?""",
                    (source.organization, source.project, source_id,
                     destination.organization, destination.project),
                ).fetchone()
            destination_id = int(mapping["destination_id"]) if mapping else None
            action = "Update" if destination_id is not None else "Create"
            try:
                patch = work_item_patch(
                    item, selected_fields, include_hyperlinks=destination_id is None,
                    require_title=destination_id is None,
                )
                if destination_id is None:
                    encoded_type = urllib.parse.quote(str(item_type), safe="")
                    response = azure_request(
                        destination, pat,
                        f"_apis/wit/workitems/${encoded_type}?api-version=7.1",
                        method="POST", payload=patch,
                        content_type="application/json-patch+json",
                    )
                    destination_id = int(response["id"])
                    destination_totals["created"] += 1
                    totals["created"] += 1
                else:
                    response = azure_request(
                        destination, pat,
                        f"_apis/wit/workitems/{destination_id}?api-version=7.1",
                        method="PATCH", payload=patch,
                        content_type="application/json-patch+json",
                    )
                    destination_id = int(response.get("id", destination_id))
                    destination_totals["updated"] += 1
                    totals["updated"] += 1
                save_mapping(source, destination, item, destination_id)
                if "Discussions" in selected_fields:
                    comment_count, comment_errors = synchronize_comments(
                        source, destination, pat, item, destination_id
                    )
                    comments_copied += comment_count
                    destination_warnings.extend(comment_errors)
                results.append({
                    "sourceId": source_id, "title": title, "type": item_type,
                    "destination": destination.project, "destinationId": destination_id,
                    "action": "Updated" if action == "Update" else "Created",
                    "status": "Success", "error": "",
                })
            except (ValueError, PermissionError, ConnectionError, RuntimeError, KeyError) as exc:
                destination_totals["failed"] += 1
                totals["failed"] += 1
                results.append({
                    "sourceId": source_id, "title": title, "type": item_type,
                    "destination": destination.project, "destinationId": destination_id,
                    "action": action, "status": "Failed", "error": str(exc),
                })
        links_copied = 0
        if body.get("preserveRelationships") is True:
            links_copied, link_errors = synchronize_links(
                source, destination, pat, source_items
            )
            destination_warnings.extend(link_errors)
        entries.append({
            "destination": destination.project,
            "status": (
                "Completed" if destination_totals["failed"] == 0 and not destination_warnings
                else "Completed with warnings or errors"
            ),
            "links": links_copied,
            "comments": comments_copied,
            "warnings": destination_warnings,
            **destination_totals,
        })

    completed_at = datetime.now(timezone.utc).isoformat()
    run_status = "completed" if totals["failed"] == 0 else "completed_with_errors"
    with database() as db:
        db.execute(
            """UPDATE sync_runs SET completed_at=?, created_count=?, updated_count=?,
               skipped_count=?, failed_count=?, status=? WHERE run_id=?""",
            (completed_at, totals["created"], totals["updated"], totals["skipped"],
             totals["failed"], run_status, run_id),
        )
    return jsonify({
        "ok": True,
        "runId": run_id,
        "duration": f"{max(1, round((time.monotonic() - started) * 1000))} ms",
        "entries": entries,
        "results": results,
        "message": (
            f"Live synchronization finished: {totals['created']} created, "
            f"{totals['updated']} updated, {totals['failed']} failed. Destination states were not changed."
        ),
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
