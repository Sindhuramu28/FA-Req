from __future__ import annotations

import base64
import csv
import ctypes
from ctypes import wintypes
import json
import io
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from flask import Flask, Response, jsonify, render_template, request, session


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
SCHEDULE_TASK_NAME = "SyncWorkTrack Daily Synchronization"


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
SCHEDULE_CONFIG_PATH = os.path.join(data_directory(), "schedule.json")
SCHEDULE_LOG_PATH = os.path.join(data_directory(), "schedule.log")


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
            CREATE TABLE IF NOT EXISTS sync_run_items (
                run_id TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                work_item_type TEXT NOT NULL,
                destination TEXT NOT NULL,
                destination_id INTEGER,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (run_id) REFERENCES sync_runs(run_id)
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
            CREATE TABLE IF NOT EXISTS synced_relations (
                source_organization TEXT NOT NULL,
                source_project TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                source_target_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                destination_organization TEXT NOT NULL,
                destination_project TEXT NOT NULL,
                destination_id INTEGER NOT NULL,
                destination_target_id INTEGER NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (
                    source_organization, source_project, source_id,
                    source_target_id, relation_type,
                    destination_organization, destination_project
                )
            );
            CREATE TABLE IF NOT EXISTS synced_attachments (
                source_organization TEXT NOT NULL,
                source_project TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                source_attachment_url TEXT NOT NULL,
                source_file_name TEXT NOT NULL,
                destination_organization TEXT NOT NULL,
                destination_project TEXT NOT NULL,
                destination_id INTEGER NOT NULL,
                destination_attachment_url TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (
                    source_organization, source_project, source_id,
                    source_attachment_url,
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


def azure_attachment_download(url: str, pat: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not (
        host == "dev.azure.com" or host.endswith(".visualstudio.com")
    ):
        raise ValueError("The source attachment URL is not a supported Azure DevOps address.")
    token = base64.b64encode(f":{pat}".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {token}", "User-Agent": "SyncWorkTrack/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PermissionError("The PAT cannot read a source work-item attachment.") from exc
        raise RuntimeError(f"Azure DevOps returned HTTP {exc.code} while downloading an attachment.") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError("Azure DevOps could not be reached while downloading an attachment.") from exc


def azure_attachment_upload(
    destination: ProjectRef, pat: str, file_name: str, content: bytes
) -> str:
    safe_name = os.path.basename(file_name.replace("\\", "/")) or "attachment.bin"
    token = base64.b64encode(f":{pat}".encode()).decode()
    url = (
        f"https://dev.azure.com/{urllib.parse.quote(destination.organization)}/"
        f"{urllib.parse.quote(destination.project)}/_apis/wit/attachments"
        f"?fileName={urllib.parse.quote(safe_name)}&api-version=7.1"
    )
    req = urllib.request.Request(
        url,
        data=content,
        method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
            "User-Agent": "SyncWorkTrack/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode())
        attachment_url = str(result.get("url", ""))
        if not attachment_url:
            raise RuntimeError("Azure DevOps did not return the uploaded attachment URL.")
        return attachment_url
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("message", "")
        except Exception:
            pass
        if exc.code in (401, 403):
            raise PermissionError("The PAT cannot upload an attachment to the destination project.") from exc
        raise RuntimeError(detail or f"Azure DevOps returned HTTP {exc.code} while uploading an attachment.") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError("Azure DevOps could not be reached while uploading an attachment.") from exc


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
    elif marker.startswith("type:"):
        work_item_type = marker.removeprefix("type:")
        allowed_types = {
            "Epic", "Feature", "Requirement", "Task", "Test Case",
        }
        if work_item_type not in allowed_types:
            raise ValueError("Choose a supported work-item type filter.")
        conditions.append(f"[System.WorkItemType] = '{wiql_escape(work_item_type)}'")
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


def expand_child_hierarchy(
    ref: ProjectRef, pat: str, root_ids: list[int], limit: int = 500
) -> list[dict[str, Any]]:
    """Load selected roots and supported descendants linked as Azure children."""
    allowed_types = {"Epic", "Feature", "Requirement", "Task", "Test Case"}
    pending = list(dict.fromkeys(int(item_id) for item_id in root_ids))
    discovered: dict[int, dict[str, Any]] = {}
    while pending:
        batch = pending[:200]
        del pending[:200]
        items = fetch_work_items(ref, pat, batch)
        returned_ids = {int(item["id"]) for item in items}
        missing_roots = [item_id for item_id in batch if item_id not in returned_ids]
        if missing_roots and any(item_id in root_ids for item_id in missing_roots):
            raise ValueError(
                "Source work items could not be loaded: "
                + ", ".join(str(item_id) for item_id in missing_roots)
                + "."
            )
        for item in items:
            item_id = int(item["id"])
            fields = item.get("fields", {})
            project = str(fields.get("System.TeamProject", ""))
            item_type = str(fields.get("System.WorkItemType", ""))
            if project and project.casefold() != ref.project.casefold():
                continue
            if item_type not in allowed_types:
                continue
            if item_id in discovered:
                continue
            discovered[item_id] = item
            if len(discovered) > limit:
                raise ValueError(
                    f"The selected hierarchy exceeds the {limit}-item safety limit. "
                    "Select a smaller branch."
                )
            for relation in item.get("relations", []):
                if relation.get("rel") != "System.LinkTypes.Hierarchy-Forward":
                    continue
                child_id = related_work_item_id(str(relation.get("url", "")))
                if child_id is not None and child_id not in discovered and child_id not in pending:
                    pending.append(child_id)
    return list(discovered.values())


def find_exact_destination_matches(
    source: ProjectRef,
    destinations: list[ProjectRef],
    pat: str,
    source_ids: list[int],
) -> list[dict[str, Any]]:
    source_items = fetch_work_items(source, pat, source_ids)
    source_by_id = {int(item["id"]): item for item in source_items}
    rows: list[dict[str, Any]] = []
    for destination in destinations:
        with database() as db:
            placeholders = ",".join("?" for _ in source_ids)
            mapping_rows = db.execute(
                f"""SELECT source_id, destination_id FROM work_item_mappings
                    WHERE source_organization=? AND source_project=?
                    AND destination_organization=? AND destination_project=?
                    AND source_id IN ({placeholders})""",
                (source.organization, source.project, destination.organization,
                 destination.project, *source_ids),
            ).fetchall()
            assigned_rows = db.execute(
                """SELECT destination_id FROM work_item_mappings
                   WHERE source_organization=? AND source_project=?
                   AND destination_organization=? AND destination_project=?""",
                (source.organization, source.project, destination.organization,
                 destination.project),
            ).fetchall()
        mapped = {int(row["source_id"]): int(row["destination_id"]) for row in mapping_rows}
        assigned_ids = {int(row["destination_id"]) for row in assigned_rows}
        unmatched = [item for item_id, item in source_by_id.items() if item_id not in mapped]
        candidate_items: list[dict[str, Any]] = []
        if unmatched:
            pairs = {
                (
                    str(item.get("fields", {}).get("System.WorkItemType", "")),
                    str(item.get("fields", {}).get("System.Title", "")),
                )
                for item in unmatched
            }
            clauses = [
                "([System.WorkItemType] = '" + wiql_escape(item_type) + "' AND "
                "[System.Title] = '" + wiql_escape(title) + "')"
                for item_type, title in pairs if item_type and title
            ]
            if clauses:
                query = (
                    "SELECT [System.Id] FROM WorkItems WHERE "
                    f"[System.TeamProject] = '{wiql_escape(destination.project)}' AND ("
                    + " OR ".join(clauses) + ") ORDER BY [System.Id]"
                )
                result = azure_request(
                    destination, pat,
                    "_apis/wit/wiql?$top=1000&api-version=7.1",
                    method="POST", payload={"query": query}, project_scoped=False,
                )
                candidate_ids = [
                    int(item["id"]) for item in result.get("workItems", [])
                    if int(item["id"]) not in assigned_ids
                ]
                if candidate_ids:
                    candidate_items = fetch_work_items(destination, pat, candidate_ids)
        for source_id in source_ids:
            source_item = source_by_id.get(source_id)
            if not source_item:
                continue
            fields = source_item.get("fields", {})
            item_type = str(fields.get("System.WorkItemType", "Unknown"))
            title = str(fields.get("System.Title", "Untitled"))
            matches = [
                {
                    "id": int(candidate["id"]),
                    "title": str(candidate.get("fields", {}).get("System.Title", "Untitled")),
                    "type": str(candidate.get("fields", {}).get("System.WorkItemType", "Unknown")),
                    "state": str(candidate.get("fields", {}).get("System.State", "")),
                }
                for candidate in candidate_items
                if str(candidate.get("fields", {}).get("System.WorkItemType", "")).casefold()
                == item_type.casefold()
                and str(candidate.get("fields", {}).get("System.Title", "")).casefold()
                == title.casefold()
            ]
            rows.append({
                "sourceId": source_id,
                "title": title,
                "type": item_type,
                "destination": destination.project,
                "destinationUrl": destination.url,
                "mappedDestinationId": mapped.get(source_id),
                "candidates": matches,
            })
    return rows


FIELD_MAP = {
    "Title": "System.Title",
    "Description": "System.Description",
    "Tags": "System.Tags",
    "Test steps": "Microsoft.VSTS.TCM.Steps",
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
    *,
    mark_current: bool = True,
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
             fields.get("System.WorkItemType", "Unknown"),
             source_item.get("rev") if mark_current else None,
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
) -> tuple[int, int, list[str]]:
    selected_ids = [int(item["id"]) for item in source_items]
    related_ids = {
        target_id for item in source_items for relation in item.get("relations", [])
        if (target_id := related_work_item_id(str(relation.get("url", "")))) is not None
    }
    mappings = destination_mappings(source, destination, selected_ids + sorted(related_ids))
    added = 0
    removed = 0
    errors: list[str] = []
    for item in source_items:
        source_id = int(item["id"])
        destination_id = mappings.get(source_id)
        if not destination_id:
            continue
        desired: dict[tuple[str, int], int] = {}
        for relation in item.get("relations", []):
            relation_type = str(relation.get("rel", ""))
            target_source_id = related_work_item_id(str(relation.get("url", "")))
            if not relation_type.startswith("System.LinkTypes.") or target_source_id not in mappings:
                continue
            desired[(relation_type, mappings[target_source_id])] = target_source_id
        try:
            with database() as db:
                tracked_rows = db.execute(
                    """SELECT source_target_id, relation_type, destination_target_id
                       FROM synced_relations WHERE source_organization=?
                       AND source_project=? AND source_id=?
                       AND destination_organization=? AND destination_project=?""",
                    (source.organization, source.project, source_id,
                     destination.organization, destination.project),
                ).fetchall()
            if not desired and not tracked_rows:
                continue
            current = azure_request(
                destination, pat,
                f"_apis/wit/workitems/{destination_id}?$expand=Relations&api-version=7.1",
            )
            current_relations = current.get("relations", [])
            existing = {
                (str(link.get("rel", "")), related_work_item_id(str(link.get("url", "")))): index
                for index, link in enumerate(current_relations)
            }
            tracked = {
                (str(row["relation_type"]), int(row["destination_target_id"])):
                    int(row["source_target_id"])
                for row in tracked_rows
            }
            stale = [key for key in tracked if key not in desired]
            remove_indexes = sorted(
                (existing[key] for key in stale if key in existing), reverse=True
            )
            patch = [
                {"op": "remove", "path": f"/relations/{index}"}
                for index in remove_indexes
            ]
            additions: list[tuple[str, int, int]] = []
            for (relation_type, target_destination_id), target_source_id in desired.items():
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
                additions.append((relation_type, target_source_id, target_destination_id))
            if patch:
                azure_request(
                    destination, pat,
                    f"_apis/wit/workitems/{destination_id}?api-version=7.1",
                    method="PATCH", payload=patch,
                    content_type="application/json-patch+json",
                )
            with database() as db:
                for relation_type, target_destination_id in stale:
                    db.execute(
                        """DELETE FROM synced_relations WHERE source_organization=?
                           AND source_project=? AND source_id=? AND relation_type=?
                           AND destination_organization=? AND destination_project=?
                           AND destination_target_id=?""",
                        (source.organization, source.project, source_id, relation_type,
                         destination.organization, destination.project,
                         target_destination_id),
                    )
                now = datetime.now(timezone.utc).isoformat()
                for relation_type, target_source_id, target_destination_id in additions:
                    db.execute(
                        """INSERT OR REPLACE INTO synced_relations (
                           source_organization, source_project, source_id,
                           source_target_id, relation_type, destination_organization,
                           destination_project, destination_id,
                           destination_target_id, synced_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (source.organization, source.project, source_id, target_source_id,
                         relation_type, destination.organization, destination.project,
                         destination_id, target_destination_id, now),
                    )
            added += len(additions)
            removed += len(remove_indexes)
        except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
            errors.append(f"Links for source {source_id}: {exc}")
    return added, removed, errors


def synchronize_hyperlinks(
    destination: ProjectRef,
    pat: str,
    source_item: dict[str, Any],
    destination_id: int,
) -> tuple[int, int, list[str]]:
    desired_urls = {
        str(relation.get("url")) for relation in source_item.get("relations", [])
        if relation.get("rel") == "Hyperlink" and relation.get("url")
    }
    try:
        current = azure_request(
            destination, pat,
            f"_apis/wit/workitems/{destination_id}?$expand=Relations&api-version=7.1",
        )
        relations = current.get("relations", [])
        existing_urls = {
            str(relation.get("url")) for relation in relations
            if relation.get("rel") == "Hyperlink" and relation.get("url")
        }
        managed = {
            index: str(relation.get("url"))
            for index, relation in enumerate(relations)
            if relation.get("rel") == "Hyperlink"
            and relation.get("attributes", {}).get("comment") == "Copied by SyncWorkTrack"
        }
        remove_indexes = sorted(
            (index for index, url in managed.items() if url not in desired_urls),
            reverse=True,
        )
        add_urls = sorted(desired_urls - existing_urls)
        patch = [
            {"op": "remove", "path": f"/relations/{index}"}
            for index in remove_indexes
        ]
        patch.extend({
            "op": "add", "path": "/relations/-",
            "value": {
                "rel": "Hyperlink", "url": url,
                "attributes": {"comment": "Copied by SyncWorkTrack"},
            },
        } for url in add_urls)
        if patch:
            azure_request(
                destination, pat,
                f"_apis/wit/workitems/{destination_id}?api-version=7.1",
                method="PATCH", payload=patch,
                content_type="application/json-patch+json",
            )
        return len(add_urls), len(remove_indexes), []
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return 0, 0, [f"Hyperlinks for source {source_item['id']}: {exc}"]


def synchronize_attachments(
    source: ProjectRef,
    destination: ProjectRef,
    pat: str,
    source_item: dict[str, Any],
    destination_id: int,
) -> tuple[int, int, list[str]]:
    source_id = int(source_item["id"])
    desired = {
        str(relation.get("url")): (
            os.path.basename(
                str(relation.get("attributes", {}).get("name", "attachment.bin"))
                .replace("\\", "/")
            ) or "attachment.bin"
        )
        for relation in source_item.get("relations", [])
        if relation.get("rel") == "AttachedFile" and relation.get("url")
    }
    try:
        with database() as db:
            tracked_rows = db.execute(
                """SELECT source_attachment_url, source_file_name,
                          destination_attachment_url
                   FROM synced_attachments WHERE source_organization=?
                   AND source_project=? AND source_id=?
                   AND destination_organization=? AND destination_project=?""",
                (source.organization, source.project, source_id,
                 destination.organization, destination.project),
            ).fetchall()
        tracked = {
            str(row["source_attachment_url"]): {
                "name": str(row["source_file_name"]),
                "destinationUrl": str(row["destination_attachment_url"]),
            }
            for row in tracked_rows
        }
        if not desired and not tracked:
            return 0, 0, []
        current = azure_request(
            destination, pat,
            f"_apis/wit/workitems/{destination_id}?$expand=Relations&api-version=7.1",
        )
        relations = current.get("relations", [])
        existing = {
            str(relation.get("url")): index
            for index, relation in enumerate(relations)
            if relation.get("rel") == "AttachedFile" and relation.get("url")
        }
        stale_source_urls = [url for url in tracked if url not in desired]
        remove_indexes = sorted(
            (
                existing[tracked[url]["destinationUrl"]]
                for url in stale_source_urls
                if tracked[url]["destinationUrl"] in existing
            ),
            reverse=True,
        )
        patch = [
            {"op": "remove", "path": f"/relations/{index}"}
            for index in remove_indexes
        ]
        new_mappings: list[tuple[str, str, str]] = []
        added = 0
        for source_url, file_name in desired.items():
            destination_url = tracked.get(source_url, {}).get("destinationUrl")
            if destination_url is None:
                content = azure_attachment_download(source_url, pat)
                destination_url = azure_attachment_upload(
                    destination, pat, file_name, content
                )
                new_mappings.append((source_url, file_name, destination_url))
            if destination_url not in existing:
                patch.append({
                    "op": "add", "path": "/relations/-",
                    "value": {
                        "rel": "AttachedFile",
                        "url": destination_url,
                        "attributes": {
                            "name": file_name,
                            "comment": "Copied by SyncWorkTrack",
                        },
                    },
                })
                added += 1
        if patch:
            azure_request(
                destination, pat,
                f"_apis/wit/workitems/{destination_id}?api-version=7.1",
                method="PATCH", payload=patch,
                content_type="application/json-patch+json",
            )
        with database() as db:
            for source_url in stale_source_urls:
                db.execute(
                    """DELETE FROM synced_attachments WHERE source_organization=?
                       AND source_project=? AND source_id=?
                       AND source_attachment_url=? AND destination_organization=?
                       AND destination_project=?""",
                    (source.organization, source.project, source_id, source_url,
                     destination.organization, destination.project),
                )
            now = datetime.now(timezone.utc).isoformat()
            for source_url, file_name, destination_url in new_mappings:
                db.execute(
                    """INSERT OR REPLACE INTO synced_attachments (
                       source_organization, source_project, source_id,
                       source_attachment_url, source_file_name,
                       destination_organization, destination_project,
                       destination_id, destination_attachment_url, synced_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source.organization, source.project, source_id, source_url,
                     file_name, destination.organization, destination.project,
                     destination_id, destination_url, now),
                )
        return added, len(remove_indexes), []
    except (ValueError, PermissionError, ConnectionError, RuntimeError, OSError) as exc:
        return 0, 0, [f"Attachments for source {source_id}: {exc}"]


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


def load_schedule_configuration() -> dict[str, Any] | None:
    try:
        with open(SCHEDULE_CONFIG_PATH, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def scheduled_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --scheduled-run'
    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop.py")
    return f'"{sys.executable}" "{launcher}" --scheduled-run'


def install_daily_task(run_time: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Daily scheduling is available only on Windows.")
    completed = subprocess.run(
        [
            "schtasks.exe", "/Create", "/TN", SCHEDULE_TASK_NAME,
            "/TR", scheduled_command(), "/SC", "DAILY", "/ST", run_time, "/F",
        ],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(detail or "Windows could not create the daily synchronization task.")


def remove_daily_task() -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", SCHEDULE_TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        os.remove(SCHEDULE_CONFIG_PATH)
    except FileNotFoundError:
        pass


def next_daily_run(run_time: str, now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    hour, minute = (int(part) for part in run_time.split(":"))
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def save_schedule_configuration(body: dict[str, Any]) -> dict[str, Any]:
    run_time = str(body.get("scheduleTime", "07:00"))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", run_time):
        raise ValueError("Choose a valid daily synchronization time.")
    selected_ids = [int(item_id) for item_id in body.get("selectedIds", [])]
    destinations = [str(value).strip() for value in body.get("destinations", []) if str(value).strip()]
    source = parse_project_url(str(body.get("source", "")))
    destination_refs = [parse_project_url(url) for url in destinations]
    validate_project_direction(source, destination_refs)
    if not selected_ids:
        raise ValueError("Select at least one work item before saving the schedule.")
    if not destination_refs:
        raise ValueError("Add at least one destination before saving the schedule.")
    if not load_os_credential():
        raise ValueError("Save the PAT in Windows Credential Manager before enabling a schedule.")
    config = {
        "source": source.url,
        "destinations": [item.url for item in destination_refs],
        "selectedIds": selected_ids,
        "fields": [str(value) for value in body.get("fields", [])],
        "preserveRelationships": body.get("preserveRelationships") is True,
        "includeChildren": body.get("includeChildren") is True,
        "scheduleTime": run_time,
        "savedAt": datetime.now(timezone.utc).isoformat(),
    }
    install_daily_task(run_time)
    temporary_path = f"{SCHEDULE_CONFIG_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    os.replace(temporary_path, SCHEDULE_CONFIG_PATH)
    return config


def append_schedule_log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(SCHEDULE_LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def run_saved_schedule() -> bool:
    config = load_schedule_configuration()
    pat = load_os_credential()
    if not config or not pat:
        append_schedule_log("Scheduled synchronization skipped: configuration or saved PAT missing.")
        return False
    payload = dict(config)
    payload.update({"liveWrites": True, "confirmation": "SYNC", "_scheduled": True})
    with app.test_client() as client:
        with client.session_transaction() as current_session:
            vault_id = secrets.token_urlsafe(24)
            current_session["vault_id"] = vault_id
        PAT_VAULT.put(vault_id, pat)
        try:
            response = client.post("/api/sync", json=payload)
            data = response.get_json(silent=True) or {}
            append_schedule_log(
                f"Scheduled synchronization HTTP {response.status_code}: "
                f"{data.get('message', 'No result message')}"
            )
            return response.status_code == 200 and data.get("ok") is True
        finally:
            PAT_VAULT.remove(vault_id)


@app.after_request
def secure_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
def index():
    prototype = os.environ.get("SYNCWORKTRACK_FIELD_PROTOTYPE") == "1"
    return render_template(
        "index.html",
        test_field_prototype=prototype,
        app_name="TestSyncApp" if prototype else "SyncWorkTrack",
    )


@app.post("/api/work-items")
def work_items():
    body = request.get_json(silent=True) or {}
    try:
        ref = parse_project_url(str(body.get("source", "")))
        marker = str(body.get("marker", "tag"))
        if marker not in {"tag", "manual"} and not marker.startswith("type:"):
            return jsonify({
                "ok": False,
                "message": "Choose All work items, Source tag: FA, or a work-item type.",
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


@app.get("/api/schedule")
def schedule_status():
    config = load_schedule_configuration()
    if not config:
        return jsonify({"enabled": False, "storedCredential": load_os_credential() is not None})
    with database() as db:
        last_run = db.execute(
            """SELECT completed_at, status FROM sync_runs WHERE mode='scheduled'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
    return jsonify({
        "enabled": True,
        "time": config.get("scheduleTime", "07:00"),
        "selectedItems": len(config.get("selectedIds", [])),
        "destinations": len(config.get("destinations", [])),
        "nextRun": next_daily_run(str(config.get("scheduleTime", "07:00"))),
        "lastRun": dict(last_run) if last_run else None,
        "storedCredential": load_os_credential() is not None,
    })


@app.post("/api/schedule")
def save_schedule():
    try:
        config = save_schedule_configuration(request.get_json(silent=True) or {})
        return jsonify({
            "ok": True,
            "enabled": True,
            "time": config["scheduleTime"],
            "nextRun": next_daily_run(config["scheduleTime"]),
            "message": f"Daily synchronization scheduled for {config['scheduleTime']}.",
        })
    except (ValueError, RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.delete("/api/schedule")
def delete_schedule():
    remove_daily_task()
    return jsonify({"ok": True, "enabled": False, "message": "Daily synchronization removed."})


@app.post("/api/matches")
def existing_matches():
    body = request.get_json(silent=True) or {}
    try:
        selected_ids = [int(item_id) for item_id in body.get("selectedIds", [])]
        if not selected_ids:
            raise ValueError("Select at least one source work item.")
        pat = session_pat()
        source = parse_project_url(str(body.get("source", "")))
        destinations = [
            parse_project_url(str(url)) for url in body.get("destinations", [])
            if str(url).strip()
        ]
        if not destinations:
            raise ValueError("Add at least one destination project.")
        validate_project_direction(source, destinations)
        if body.get("includeChildren") is True:
            hierarchy = expand_child_hierarchy(source, pat, selected_ids)
            selected_ids = [int(item["id"]) for item in hierarchy]
        rows = find_exact_destination_matches(source, destinations, pat, selected_ids)
        possible = sum(1 for row in rows if row["candidates"])
        mapped = sum(1 for row in rows if row["mappedDestinationId"] is not None)
        return jsonify({
            "ok": True,
            "rows": rows,
            "message": f"Found possible matches for {possible} item-destination pairs; {mapped} are already linked.",
        })
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/mappings/link")
def link_existing_items():
    body = request.get_json(silent=True) or {}
    choices = body.get("choices", [])
    if not choices:
        return jsonify({"ok": False, "message": "Choose at least one destination match."}), 400
    try:
        pat = session_pat()
        source = parse_project_url(str(body.get("source", "")))
        source_ids = sorted({int(choice["sourceId"]) for choice in choices})
        source_items = fetch_work_items(source, pat, source_ids)
        source_by_id = {int(item["id"]): item for item in source_items}
        linked = 0
        for choice in choices:
            source_id = int(choice["sourceId"])
            destination_id = int(choice["destinationId"])
            mode = str(choice.get("mode", "sync"))
            if mode not in {"sync", "aligned"}:
                raise ValueError("Choose Link and sync or Link only.")
            source_item = source_by_id.get(source_id)
            if not source_item:
                raise ValueError(f"Source work item {source_id} could not be loaded.")
            destination = parse_project_url(str(choice.get("destinationUrl", "")))
            validate_project_direction(source, [destination])
            destination_items = fetch_work_items(destination, pat, [destination_id])
            if not destination_items:
                raise ValueError(
                    f"Destination work item {destination_id} could not be loaded from {destination.project}."
                )
            destination_item = destination_items[0]
            source_fields = source_item.get("fields", {})
            destination_fields = destination_item.get("fields", {})
            same_title = str(source_fields.get("System.Title", "")).casefold() == str(
                destination_fields.get("System.Title", "")
            ).casefold()
            same_type = str(source_fields.get("System.WorkItemType", "")).casefold() == str(
                destination_fields.get("System.WorkItemType", "")
            ).casefold()
            if not same_title or not same_type:
                raise ValueError(
                    f"Destination work item {destination_id} no longer exactly matches the source title and type."
                )
            with database() as db:
                assigned = db.execute(
                    """SELECT source_id FROM work_item_mappings
                       WHERE source_organization=? AND source_project=?
                       AND destination_organization=? AND destination_project=?
                       AND destination_id=? AND source_id<>?""",
                    (source.organization, source.project, destination.organization,
                     destination.project, destination_id, source_id),
                ).fetchone()
            if assigned:
                raise ValueError(
                    f"Destination work item {destination_id} is already linked to source {assigned['source_id']}."
                )
            save_mapping(
                source, destination, source_item, destination_id,
                mark_current=mode == "aligned",
            )
            linked += 1
        return jsonify({
            "ok": True,
            "linked": linked,
            "message": f"Linked {linked} existing destination item mappings.",
        })
    except (KeyError, TypeError, ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


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
    root_ids = {int(item_id) for item_id in selected_ids}
    if body.get("includeChildren") is True:
        try:
            hierarchy = expand_child_hierarchy(source, session_pat(), list(root_ids))
        except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        selected_ids = [int(item["id"]) for item in hierarchy]
        selected_items = [
            {
                "id": item["id"],
                "title": item.get("fields", {}).get("System.Title", "Untitled"),
                "type": item.get("fields", {}).get("System.WorkItemType", "Unknown"),
                "rev": item.get("rev"),
            }
            for item in hierarchy
        ]
    item_details = {
        int(item.get("id")): {
            "title": str(item.get("title", "Untitled")),
            "type": str(item.get("type", "Unknown")),
            "rev": item.get("rev"),
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
                f"""SELECT source_id, destination_id, last_source_revision FROM work_item_mappings
                    WHERE source_organization=? AND source_project=?
                    AND destination_organization=? AND destination_project=?
                    AND source_id IN ({placeholders})""",
                [source.organization, source.project, destination.organization,
                 destination.project, *selected_ids],
            ).fetchall()
            mapped = {
                int(item["source_id"]): {
                    "destinationId": int(item["destination_id"]),
                    "lastRevision": item["last_source_revision"],
                }
                for item in mapping_rows
            }
            for source_id in selected_ids:
                numeric_id = int(source_id)
                details = item_details.get(numeric_id, {})
                mapping = mapped.get(numeric_id)
                destination_id = mapping["destinationId"] if mapping else None
                current_revision = details.get("rev")
                last_revision = mapping["lastRevision"] if mapping else None
                if mapping is None:
                    action = "To create"
                elif last_revision is None or current_revision is None or int(current_revision) > int(last_revision):
                    action = "Changes to sync"
                else:
                    action = "Up to date"
                changes.append({
                    "sourceId": numeric_id,
                    "title": details.get("title", "Untitled"),
                    "type": details.get("type", "Unknown"),
                    "destination": destination.project,
                    "destinationId": destination_id,
                    "action": action,
                    "status": "Included child" if numeric_id not in root_ids else "Planned",
                })
    total_pairs = len(selected_ids) * len(destination_refs)
    create_count = total_pairs - mapped_pairs
    update_count = sum(1 for change in changes if change["action"] == "Changes to sync")
    up_to_date_count = sum(1 for change in changes if change["action"] == "Up to date")
    return jsonify({
        "ok": True,
        "summary": {
            "items": len(selected_ids),
            "destinations": len(destinations),
            "creates": create_count,
            "updates": update_count,
            "upToDate": up_to_date_count,
            "relationships": 0,
            "fields": len(fields),
        },
        "changes": changes,
        "message": "Live mapping preview completed. No Azure DevOps data was changed.",
    })


@app.post("/api/sync")
def sync():
    body = request.get_json(silent=True) or {}
    run_mode = "scheduled" if body.get("_scheduled") is True else "live"
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
        requested_ids = list(selected_ids)
        source_items = (
            expand_child_hierarchy(source, pat, requested_ids)
            if body.get("includeChildren") is True
            else fetch_work_items(source, pat, requested_ids)
        )
        selected_ids = [int(item["id"]) for item in source_items]
        found_ids = {int(item["id"]) for item in source_items}
        missing_ids = sorted(set(requested_ids) - found_ids)
        if missing_ids:
            raise ValueError(f"Source work items could not be loaded: {', '.join(map(str, missing_ids))}.")
    except (ValueError, PermissionError, ConnectionError, RuntimeError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400

    with database() as db:
        db.execute(
            """INSERT INTO sync_runs
               (run_id, started_at, mode, source_project, destinations, status)
               VALUES (?, ?, ?, ?, ?, 'running')""",
            (run_id, started_at, run_mode, source.project, len(destination_refs)),
        )

    entries: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    for destination in destination_refs:
        destination_totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        destination_warnings: list[str] = []
        comments_copied = 0
        hyperlinks_added = 0
        hyperlinks_removed = 0
        attachments_added = 0
        attachments_removed = 0
        for item in source_items:
            source_id = int(item["id"])
            source_fields = item.get("fields", {})
            title = source_fields.get("System.Title", "Untitled")
            item_type = source_fields.get("System.WorkItemType", "Unknown")
            with database() as db:
                mapping = db.execute(
                    """SELECT destination_id, last_source_revision FROM work_item_mappings
                       WHERE source_organization=? AND source_project=? AND source_id=?
                       AND destination_organization=? AND destination_project=?""",
                    (source.organization, source.project, source_id,
                     destination.organization, destination.project),
                ).fetchone()
            destination_id = int(mapping["destination_id"]) if mapping else None
            action = "Update" if destination_id is not None else "Create"
            if (
                mapping and mapping["last_source_revision"] is not None
                and item.get("rev") is not None
                and int(item["rev"]) <= int(mapping["last_source_revision"])
            ):
                item_warnings: list[str] = []
                if "Attachments" in selected_fields:
                    added_count, removed_count, attachment_errors = synchronize_attachments(
                        source, destination, pat, item, destination_id
                    )
                    attachments_added += added_count
                    attachments_removed += removed_count
                    destination_warnings.extend(attachment_errors)
                    item_warnings.extend(attachment_errors)
                destination_totals["skipped"] += 1
                totals["skipped"] += 1
                results.append({
                    "sourceId": source_id, "title": title, "type": item_type,
                    "destination": destination.project, "destinationId": destination_id,
                    "action": "Up to date", "status": (
                        "Skipped with warnings" if item_warnings else "Skipped"
                    ), "error": " | ".join(item_warnings),
                })
                continue
            item_warnings: list[str] = []
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
                if "Hyperlinks" in selected_fields and action == "Update":
                    added_count, removed_count, hyperlink_errors = synchronize_hyperlinks(
                        destination, pat, item, destination_id
                    )
                    hyperlinks_added += added_count
                    hyperlinks_removed += removed_count
                    destination_warnings.extend(hyperlink_errors)
                    item_warnings.extend(hyperlink_errors)
                if "Discussions" in selected_fields:
                    comment_count, comment_errors = synchronize_comments(
                        source, destination, pat, item, destination_id
                    )
                    comments_copied += comment_count
                    destination_warnings.extend(comment_errors)
                    item_warnings.extend(comment_errors)
                if "Attachments" in selected_fields:
                    added_count, removed_count, attachment_errors = synchronize_attachments(
                        source, destination, pat, item, destination_id
                    )
                    attachments_added += added_count
                    attachments_removed += removed_count
                    destination_warnings.extend(attachment_errors)
                    item_warnings.extend(attachment_errors)
                results.append({
                    "sourceId": source_id, "title": title, "type": item_type,
                    "destination": destination.project, "destinationId": destination_id,
                    "action": "Updated" if action == "Update" else "Created",
                    "status": "Success with warnings" if item_warnings else "Success",
                    "error": " | ".join(item_warnings),
                })
            except (ValueError, PermissionError, ConnectionError, RuntimeError, KeyError) as exc:
                destination_totals["failed"] += 1
                totals["failed"] += 1
                results.append({
                    "sourceId": source_id, "title": title, "type": item_type,
                    "destination": destination.project, "destinationId": destination_id,
                    "action": action, "status": "Failed", "error": str(exc),
                })
        links_added = 0
        links_removed = 0
        if body.get("preserveRelationships") is True:
            links_added, links_removed, link_errors = synchronize_links(
                source, destination, pat, source_items
            )
            destination_warnings.extend(link_errors)
        entries.append({
            "destination": destination.project,
            "status": (
                "Completed" if destination_totals["failed"] == 0 and not destination_warnings
                else "Completed with warnings or errors"
            ),
            "links": links_added,
            "linksRemoved": links_removed,
            "comments": comments_copied,
            "hyperlinksAdded": hyperlinks_added,
            "hyperlinksRemoved": hyperlinks_removed,
            "attachmentsAdded": attachments_added,
            "attachmentsRemoved": attachments_removed,
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
        db.executemany(
            """INSERT INTO sync_run_items
               (run_id, source_id, title, work_item_type, destination,
                destination_id, action, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    run_id, row["sourceId"], row["title"], row["type"],
                    row["destination"], row.get("destinationId"), row["action"],
                    row["status"], row.get("error", ""),
                )
                for row in results
            ],
        )
    return jsonify({
        "ok": True,
        "runId": run_id,
        "duration": f"{max(1, round((time.monotonic() - started) * 1000))} ms",
        "entries": entries,
        "results": results,
        "message": (
            f"Live synchronization finished: {totals['created']} created, "
            f"{totals['updated']} updated, {totals['skipped']} already up to date, "
            f"{totals['failed']} failed. Destination states were not changed."
        ),
    })


def latest_sync_csv() -> tuple[str, str]:
    with database() as db:
        run = db.execute(
            """SELECT run_id, started_at, completed_at, mode, source_project,
                      destinations, created_count, updated_count, skipped_count,
                      failed_count, status
               FROM sync_runs WHERE completed_at IS NOT NULL
               ORDER BY completed_at DESC LIMIT 1"""
        ).fetchone()
        if run is None:
            raise ValueError("No completed synchronization log is available yet.")
        rows = db.execute(
            """SELECT source_id, title, work_item_type, destination,
                      destination_id, action, status, error
               FROM sync_run_items WHERE run_id=?
               ORDER BY destination, source_id""",
            (run["run_id"],),
        ).fetchall()
    def spreadsheet_safe(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["SyncWorkTrack recent synchronization log"])
    writer.writerow(["Run ID", run["run_id"]])
    writer.writerow(["Started UTC", run["started_at"]])
    writer.writerow(["Completed UTC", run["completed_at"]])
    writer.writerow(["Mode", run["mode"]])
    writer.writerow(["Source project", run["source_project"]])
    writer.writerow(["Destinations", run["destinations"]])
    writer.writerow(["Created", run["created_count"]])
    writer.writerow(["Updated", run["updated_count"]])
    writer.writerow(["Up to date", run["skipped_count"]])
    writer.writerow(["Failed", run["failed_count"]])
    writer.writerow([])
    writer.writerow([
        "Source ID", "Title", "Type", "Destination", "Destination ID",
        "Action", "Status", "Error",
    ])
    for row in rows:
        writer.writerow([spreadsheet_safe(value) for value in [
            row["source_id"], row["title"], row["work_item_type"],
            row["destination"], row["destination_id"] or "", row["action"],
            row["status"], row["error"],
        ]])
    filename = f"SyncWorkTrack-{run['run_id']}.csv"
    return filename, "\ufeff" + output.getvalue()


@app.get("/api/export/latest")
def export_latest_sync_log():
    try:
        filename, content = latest_sync_csv()
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 404
    return Response(
        content,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export/latest/save")
def save_latest_sync_log():
    try:
        filename, content = latest_sync_csv()
        export_folder = os.path.join(
            os.path.expanduser("~"), "Downloads", "SyncWorkTrack Exported Logs"
        )
        os.makedirs(export_folder, exist_ok=True)
        path = os.path.join(export_folder, filename)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return jsonify({
            "ok": True,
            "path": path,
            "filename": filename,
            "message": f"Synchronization log saved to {path}",
        })
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 404
    except OSError as exc:
        return jsonify({
            "ok": False,
            "message": f"The synchronization log could not be saved: {exc}",
        }), 500


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
