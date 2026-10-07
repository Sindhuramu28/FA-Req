# SyncWorkTrack

A Python/Flask desktop app for one-way Azure DevOps work-item synchronization from one source project to multiple destination projects.

## Included

- Session-only PAT entry and live project-access validation
- One source and addable destination projects
- Work-item selection for Epics, Features, Requirements, Tasks, Test Cases, and other destination-supported types
- Field controls with State explicitly excluded
- Safe preview followed by an explicit live-write switch and confirmation
- Creates selected items in every destination and updates them on later runs
- Exact Type + Title match discovery for linking pre-existing destination items without creating duplicates
- Local source-to-destination ID mappings in `%LOCALAPPDATA%\FA-Sync-Console\fa-sync.db`
- Optional PAT storage in Windows Credential Manager; the PAT is never written to app files
- Automatic `FA-Synced` destination tag, reconciled mapped work-item links, synchronized hyperlinks and attachments, and optional discussions
- Optional daily synchronization through Windows Task Scheduler, using a PAT stored only in Windows Credential Manager
- Per-item destination IDs and error reports
- Persistent per-item synchronization logs with one-click CSV export for the most recent run
- Source filtering by FA tag or work-item type, including Epic, Feature, Requirement, Task, and Test Case

Destination workflow state is never created from or updated to match the source. The destination project must support the source work-item type; otherwise that item is reported as failed without stopping the remaining items.

Test Case work items support the selected common fields and Microsoft test steps. Test Plans, Test Suites, configurations, shared-step membership, and automated-test associations are outside the current work-item synchronization scope.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

## Windows executable

The packaged `SyncWorkTrack.exe` opens as a desktop application. It does not show a console, browser address, or local IP. Closing the desktop window stops the application and clears the session PAT unless the user explicitly saved it in Windows Credential Manager.

`testsyncapp.exe` is a separate UI prototype. It displays disabled, type-specific field choices when Epic, Feature, Requirement, Task, or Test Case is selected. These prototype choices are intentionally visual-only and are never included in Azure synchronization requests. The regular `SyncWorkTrack.exe` does not display this section.

## PAT handling

Use a PAT with **Work Items: Read & write** access to the source and every destination project. The PAT is posted to the Python backend for validation and retained only in process memory under a random session identifier. If **Remember securely** is selected, it is stored in Windows Credential Manager. It is never written to project files, logs, cookies, browser storage, or environment files.

## Live synchronization safety

Preview is always read-only. A live run requires enabling **Activate** and accepting a separate confirmation. Selected fields are synchronized; Title remains mandatory when a new destination item is created. The `FA-Synced` tracking tag is always retained. Selected hyperlinks, attachments, and mapped Azure work-item links are reconciled on later runs. State is never included in Azure create or update requests.

When **Attachments** is selected, files attached to the source work item are downloaded and uploaded to each destination work item. SyncWorkTrack records each transferred file locally so later runs do not upload duplicates. If a source attachment is removed, only the corresponding attachment previously created and tracked by SyncWorkTrack is removed; destination-owned files are left untouched. Inline images embedded inside rich-text fields are not currently rewritten to their new destination attachment URLs.

## Daily scheduling

Save the PAT in Windows Credential Manager, select the source items and destinations, choose a daily time, and select **Save schedule**. SyncWorkTrack registers a Windows Task Scheduler task that launches the same executable in background mode. The schedule configuration is stored under `%LOCALAPPDATA%\FA-Sync-Console`; it contains project URLs, selected work-item IDs, selected fields, and the run time, but never the PAT. Manual synchronization remains available at any time.

Mapped Parent/Child, Affects/Affected By, and other Azure `System.LinkTypes.*` relationships are reconciled when **Copy mapped work-item links** is selected. SyncWorkTrack adds missing mapped links and removes obsolete links only when those links were previously created and tracked by SyncWorkTrack. Destination-owned links are left untouched.

## Existing destination items

Select source items and choose **Match existing**. SyncWorkTrack searches each destination for exact work-item Type + Title matches and requires the user to confirm the destination ID. **Link and sync** stores the mapping and applies selected source fields on the next synchronization. **Link only** stores the current source revision as the baseline, so only later source changes synchronize. Neither option changes the destination State.
