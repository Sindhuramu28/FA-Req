# FA Sync Console

A Python/Flask desktop app for one-way Azure DevOps work-item synchronization from one source project to multiple destination projects.

## Included

- Session-only PAT entry and live project-access validation
- One source and addable destination projects
- Work-item selection for Epics, Features, Requirements, Tasks, and other destination-supported types
- Field controls with State explicitly excluded
- Safe preview followed by an explicit live-write switch and confirmation
- Creates selected items in every destination and updates them on later runs
- Local source-to-destination ID mappings in `%LOCALAPPDATA%\FA-Sync-Console\fa-sync.db`
- Per-item destination IDs and error reports

Destination workflow state is never created from or updated to match the source. The destination project must support the source work-item type; otherwise that item is reported as failed without stopping the remaining items.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Windows executable

The packaged `FA-Sync-Console.exe` opens as a desktop application. It does not show a console, browser address, or local IP. Closing the desktop window stops the application and clears the session PAT.

## PAT handling

Use a PAT with **Work Items: Read & write** access to the source and every destination project. The PAT is posted to the Python backend for validation and retained only in process memory under a random session identifier. It is never written to project files, logs, cookies, or browser storage. Restarting the app clears all retained PATs.

## Live synchronization safety

Preview is always read-only. A live run requires enabling **Enable live writes** and accepting a separate confirmation. Title is always synchronized. Description, tags, and hyperlinks follow the selected field controls. Hyperlinks are copied during creation; later updates do not duplicate them. State is never included in Azure create or update requests.
