# FA Sync Console

A Python/Flask demonstration app for configuring one-way Azure DevOps FA work-item synchronization from one source project to multiple destination projects.

## Included in the demo

- Session-only PAT entry and live project-access validation
- One source and addable destination projects
- FA work-item selection for Epics, Features, Requirements, and Test Cases
- Field controls with State explicitly excluded
- Relationship and schedule settings
- Safe preview and dry-run reports

The demo does not create or update Azure DevOps work items. Production write operations should be enabled only after field mapping, identity mapping, conflict handling, deletion behavior, and relationship policies are approved.

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

The PAT is posted to the Python backend for validation and retained only in process memory under a random session identifier. It is never written to project files, logs, cookies, or browser storage. Restarting the app clears all retained PATs.
