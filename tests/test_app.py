import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from app import (
    app, azure_attachment_download, expand_child_hierarchy, expand_work_item_selection,
    load_source_work_items, parse_project_url,
    synchronize_attachments, synchronize_hyperlinks, synchronize_links, work_item_patch,
)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_database_path = app_module.DATABASE_PATH
        app_module.DATABASE_PATH = os.path.join(self.temp_directory.name, "test.db")
        app_module.initialize_database()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self):
        app_module.DATABASE_PATH = self.original_database_path
        self.temp_directory.cleanup()

    def test_project_url_parser(self):
        ref = parse_project_url("https://dev.azure.com/PG-PSDC/TestProject1_Base")
        self.assertEqual(ref.organization, "PG-PSDC")
        self.assertEqual(ref.project, "TestProject1_Base")

    def test_project_url_parser_decodes_names(self):
        ref = parse_project_url("https://dev.azure.com/example/My%20Project")
        self.assertEqual(ref.project, "My Project")
        self.assertEqual(ref.url, "https://dev.azure.com/example/My%20Project")

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_standard_app_has_requested_work_item_filters(self):
        response = self.client.get("/")
        self.assertIn(b'value="type:Task"', response.data)
        self.assertIn(b'value="type:Test Case"', response.data)
        self.assertNotIn(b'value="type:Bug"', response.data)
        self.assertNotIn(b'value="type:Issue"', response.data)

    def test_preview_requires_items(self):
        response = self.client.post("/api/preview", json={"selectedIds": [], "destinations": ["x"]})
        self.assertEqual(response.status_code, 400)

    def test_preview_returns_individual_change_rows(self):
        response = self.client.post("/api/preview", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [12],
            "selectedItems": [{"id": 12, "title": "Test task", "type": "Task"}],
            "destinations": ["https://dev.azure.com/example/destination"],
            "fields": ["Title"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["changes"][0]["sourceId"], 12)
        self.assertEqual(response.json["changes"][0]["title"], "Test task")
        self.assertEqual(response.json["changes"][0]["action"], "To create")

    def add_mapping(self, source_id=12, destination_id=99, revision=4):
        with app_module.database() as db:
            db.execute(
                """INSERT INTO work_item_mappings
                   (source_organization, source_project, source_id,
                    destination_organization, destination_project, destination_id,
                    work_item_type, last_source_revision, first_synced_at,
                    last_synced_at, status)
                   VALUES ('example', 'source', ?, 'example', 'destination', ?,
                           'Task', ?, 'now', 'now', 'active')""",
                (source_id, destination_id, revision),
            )

    def test_preview_marks_newer_revision_as_changes_to_sync(self):
        self.add_mapping(revision=4)
        response = self.client.post("/api/preview", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [12],
            "selectedItems": [{"id": 12, "title": "Changed task", "type": "Task", "rev": 5}],
            "destinations": ["https://dev.azure.com/example/destination"],
            "fields": ["Title"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["changes"][0]["action"], "Changes to sync")
        self.assertEqual(response.json["summary"]["updates"], 1)

    def test_preview_marks_same_revision_as_up_to_date(self):
        self.add_mapping(revision=4)
        response = self.client.post("/api/preview", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [12],
            "selectedItems": [{"id": 12, "title": "Unchanged task", "type": "Task", "rev": 4}],
            "destinations": ["https://dev.azure.com/example/destination"],
            "fields": ["Title"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["changes"][0]["action"], "Up to date")
        self.assertEqual(response.json["summary"]["upToDate"], 1)

    @patch("app.fetch_work_items")
    @patch("app.azure_request")
    @patch("app.session_pat", return_value="read-pat")
    def test_match_existing_finds_exact_type_and_title_only(
        self, session_pat, azure_request, fetch_work_items
    ):
        source_item = {
            "id": 12, "rev": 5,
            "fields": {"System.Title": "Existing feature", "System.WorkItemType": "Feature"},
            "relations": [],
        }
        destination_item = {
            "id": 99, "rev": 3,
            "fields": {
                "System.Title": "Existing feature", "System.WorkItemType": "Feature",
                "System.State": "Proposed",
            },
            "relations": [],
        }
        fetch_work_items.side_effect = [[source_item], [destination_item]]
        azure_request.return_value = {"workItems": [{"id": 99}]}
        response = self.client.post("/api/matches", json={
            "source": "https://dev.azure.com/example/source",
            "destinations": ["https://dev.azure.com/example/destination"],
            "selectedIds": [12],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["rows"][0]["candidates"][0]["id"], 99)
        self.assertEqual(response.json["rows"][0]["candidates"][0]["state"], "Proposed")

    @patch("app.fetch_work_items")
    @patch("app.session_pat", return_value="read-pat")
    def test_link_and_sync_stores_mapping_as_changes_to_sync(self, session_pat, fetch_work_items):
        source_item = {
            "id": 12, "rev": 5,
            "fields": {"System.Title": "Existing feature", "System.WorkItemType": "Feature"},
            "relations": [],
        }
        destination_item = {
            "id": 99, "rev": 3,
            "fields": {"System.Title": "Existing feature", "System.WorkItemType": "Feature"},
            "relations": [],
        }
        fetch_work_items.side_effect = [[source_item], [destination_item]]
        response = self.client.post("/api/mappings/link", json={
            "source": "https://dev.azure.com/example/source",
            "choices": [{
                "sourceId": 12,
                "destinationUrl": "https://dev.azure.com/example/destination",
                "destinationId": 99,
                "mode": "sync",
            }],
        })
        self.assertEqual(response.status_code, 200)
        preview = self.client.post("/api/preview", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [12],
            "selectedItems": [{"id": 12, "title": "Existing feature", "type": "Feature", "rev": 5}],
            "destinations": ["https://dev.azure.com/example/destination"],
            "fields": ["Title"],
        })
        self.assertEqual(preview.json["changes"][0]["destinationId"], 99)
        self.assertEqual(preview.json["changes"][0]["action"], "Changes to sync")

    @patch("app.fetch_work_items")
    @patch("app.session_pat", return_value="read-pat")
    def test_link_only_marks_existing_item_up_to_date(self, session_pat, fetch_work_items):
        item = {
            "id": 12, "rev": 5,
            "fields": {"System.Title": "Existing task", "System.WorkItemType": "Task"},
            "relations": [],
        }
        destination_item = {
            "id": 99, "rev": 2,
            "fields": {"System.Title": "Existing task", "System.WorkItemType": "Task"},
            "relations": [],
        }
        fetch_work_items.side_effect = [[item], [destination_item]]
        response = self.client.post("/api/mappings/link", json={
            "source": "https://dev.azure.com/example/source",
            "choices": [{
                "sourceId": 12,
                "destinationUrl": "https://dev.azure.com/example/destination",
                "destinationId": 99,
                "mode": "aligned",
            }],
        })
        self.assertEqual(response.status_code, 200)
        with app_module.database() as db:
            mapping = db.execute("SELECT last_source_revision FROM work_item_mappings").fetchone()
        self.assertEqual(mapping["last_source_revision"], 5)

    def test_work_item_patch_never_copies_state(self):
        patch_document = work_item_patch({
            "fields": {
                "System.Title": "FA_test1",
                "System.Description": "Description",
                "System.State": "Closed",
                "System.Tags": "FA",
            },
            "relations": [],
        }, ["Title", "Description", "Tags"])
        paths = [operation["path"] for operation in patch_document]
        self.assertIn("/fields/System.Title", paths)
        self.assertIn("/fields/System.Description", paths)
        self.assertIn("/fields/System.Tags", paths)
        self.assertNotIn("/fields/System.State", paths)
        tag_operation = next(op for op in patch_document if op["path"] == "/fields/System.Tags")
        self.assertIn("FA-Synced", tag_operation["value"])

    def test_custom_impact_assessment_field_is_copied(self):
        patch_document = work_item_patch({
            "fields": {
                "System.Title": "Feature",
                "Custom.ImpactAssessment": "High impact",
            },
            "relations": [],
        }, ["Impact assessment"])
        operation = next(
            op for op in patch_document
            if op["path"] == "/fields/Custom.ImpactAssessment"
        )
        self.assertEqual(operation["value"], "High impact")

    def test_test_case_steps_are_copied_without_state(self):
        patch_document = work_item_patch({
            "fields": {
                "System.Title": "Verify protection",
                "System.State": "Closed",
                "Microsoft.VSTS.TCM.Steps": "<steps id=\"0\"><step /></steps>",
            },
            "relations": [],
        }, ["Title", "Test steps"])
        values = {operation["path"]: operation["value"] for operation in patch_document}
        self.assertIn("/fields/Microsoft.VSTS.TCM.Steps", values)
        self.assertNotIn("/fields/System.State", values)

    def test_title_can_be_excluded_from_updates(self):
        patch_document = work_item_patch({
            "fields": {"System.Title": "Do not copy"}, "relations": []
        }, [], require_title=False)
        self.assertNotIn("/fields/System.Title", [op["path"] for op in patch_document])

    @patch("app.load_os_credential", return_value="saved-pat")
    def test_credential_status_reports_saved_pat(self, load_credential):
        response = self.client.get("/api/credential-status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["stored"])

    @patch("app.save_schedule_configuration")
    def test_daily_schedule_endpoint(self, save_configuration):
        save_configuration.return_value = {
            "scheduleTime": "07:00", "selectedIds": [12],
            "destinations": ["https://dev.azure.com/example/destination"],
        }
        response = self.client.post("/api/schedule", json={"scheduleTime": "07:00"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["enabled"])
        self.assertEqual(response.json["time"], "07:00")

    def test_live_sync_requires_explicit_confirmation(self):
        response = self.client.post("/api/sync", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [12],
            "destinations": ["https://dev.azure.com/example/destination"],
            "liveWrites": True,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm", response.json["message"].lower())

    @patch("app.save_mapping")
    @patch("app.azure_request")
    @patch("app.fetch_work_items")
    @patch("app.session_pat", return_value="write-pat")
    def test_live_sync_creates_item_without_copying_state(
        self, session_pat, fetch_work_items, azure_request, save_mapping
    ):
        fetch_work_items.return_value = [{
            "id": 12,
            "rev": 4,
            "fields": {
                "System.WorkItemType": "Task",
                "System.Title": "FA_test1",
                "System.Description": "Test",
                "System.State": "Closed",
            },
            "relations": [],
        }]
        azure_request.return_value = {"id": 99}
        response = self.client.post("/api/sync", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [12],
            "destinations": ["https://dev.azure.com/example/destination"],
            "fields": ["Title", "Description"],
            "liveWrites": True,
            "confirmation": "SYNC",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["results"][0]["destinationId"], 99)
        request_patch = azure_request.call_args.kwargs["payload"]
        self.assertNotIn("/fields/System.State", [op["path"] for op in request_patch])
        self.assertEqual(azure_request.call_args.kwargs["content_type"], "application/json-patch+json")
        save_mapping.assert_called_once()

    def test_live_items_require_validated_session(self):
        response = self.client.post("/api/work-items", json={
            "source": "https://dev.azure.com/example/source",
            "marker": "tag",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Validate", response.json["message"])

    @patch("app.azure_request")
    def test_relationship_sync_adds_and_tracks_mapped_parent_child_link(self, azure_request):
        self.add_mapping(source_id=1, destination_id=101)
        self.add_mapping(source_id=2, destination_id=102)
        azure_request.side_effect = [{"relations": []}, {"id": 101}]
        source = parse_project_url("https://dev.azure.com/example/source")
        destination = parse_project_url("https://dev.azure.com/example/destination")
        added, removed, errors = synchronize_links(source, destination, "pat", [{
            "id": 1,
            "relations": [{
                "rel": "System.LinkTypes.Hierarchy-Forward",
                "url": "https://dev.azure.com/example/_apis/wit/workItems/2",
            }],
        }])
        self.assertEqual((added, removed, errors), (1, 0, []))
        patch_document = azure_request.call_args_list[1].kwargs["payload"]
        self.assertEqual(patch_document[0]["value"]["rel"], "System.LinkTypes.Hierarchy-Forward")
        with app_module.database() as db:
            tracked = db.execute("SELECT COUNT(*) AS count FROM synced_relations").fetchone()
        self.assertEqual(tracked["count"], 1)

    @patch("app.azure_request")
    def test_relationship_sync_removes_only_obsolete_tracked_link(self, azure_request):
        self.add_mapping(source_id=1, destination_id=101)
        self.add_mapping(source_id=2, destination_id=102)
        with app_module.database() as db:
            db.execute(
                """INSERT INTO synced_relations VALUES
                   ('example', 'source', 1, 2, 'System.LinkTypes.Hierarchy-Forward',
                    'example', 'destination', 101, 102, 'now')"""
            )
        azure_request.side_effect = [{"relations": [{
            "rel": "System.LinkTypes.Hierarchy-Forward",
            "url": "https://dev.azure.com/example/_apis/wit/workItems/102",
        }]}, {"id": 101}]
        source = parse_project_url("https://dev.azure.com/example/source")
        destination = parse_project_url("https://dev.azure.com/example/destination")
        added, removed, errors = synchronize_links(
            source, destination, "pat", [{"id": 1, "relations": []}]
        )
        self.assertEqual((added, removed, errors), (0, 1, []))
        patch_document = azure_request.call_args_list[1].kwargs["payload"]
        self.assertEqual(patch_document, [{"op": "remove", "path": "/relations/0"}])
        with app_module.database() as db:
            tracked = db.execute("SELECT COUNT(*) AS count FROM synced_relations").fetchone()
        self.assertEqual(tracked["count"], 0)

    @patch("app.azure_request")
    def test_hyperlink_sync_adds_new_and_removes_only_managed_obsolete_links(self, azure_request):
        azure_request.side_effect = [{"relations": [
            {
                "rel": "Hyperlink", "url": "https://obsolete.example",
                "attributes": {"comment": "Copied by SyncWorkTrack"},
            },
            {
                "rel": "Hyperlink", "url": "https://destination-owned.example",
                "attributes": {"comment": "Added manually"},
            },
        ]}, {"id": 101}]
        destination = parse_project_url("https://dev.azure.com/example/destination")
        added, removed, errors = synchronize_hyperlinks(destination, "pat", {
            "id": 1,
            "relations": [{"rel": "Hyperlink", "url": "https://new.example"}],
        }, 101)
        self.assertEqual((added, removed, errors), (1, 1, []))
        patch_document = azure_request.call_args_list[1].kwargs["payload"]
        self.assertEqual(patch_document[0], {"op": "remove", "path": "/relations/0"})
        self.assertEqual(patch_document[1]["value"]["url"], "https://new.example")

    @patch("app.azure_attachment_upload", return_value="https://dest.example/attachment/1")
    @patch("app.azure_attachment_download", return_value=b"file contents")
    @patch("app.azure_request")
    def test_attachment_sync_uploads_adds_and_tracks_file(
        self, azure_request, attachment_download, attachment_upload
    ):
        azure_request.side_effect = [{"relations": []}, {"id": 101}]
        source = parse_project_url("https://dev.azure.com/example/source")
        destination = parse_project_url("https://dev.azure.com/example/destination")
        added, removed, errors = synchronize_attachments(
            source, destination, "pat", {
                "id": 1,
                "relations": [{
                    "rel": "AttachedFile",
                    "url": "https://dev.azure.com/example/_apis/wit/attachments/source-1",
                    "attributes": {"name": "report.pdf"},
                }],
            }, 101,
        )
        self.assertEqual((added, removed, errors), (1, 0, []))
        attachment_download.assert_called_once()
        attachment_upload.assert_called_once_with(
            destination, "pat", "report.pdf", b"file contents"
        )
        patch_document = azure_request.call_args_list[1].kwargs["payload"]
        self.assertEqual(patch_document[0]["value"]["rel"], "AttachedFile")
        self.assertEqual(
            patch_document[0]["value"]["url"], "https://dest.example/attachment/1"
        )
        with app_module.database() as db:
            tracked = db.execute("SELECT COUNT(*) AS count FROM synced_attachments").fetchone()
        self.assertEqual(tracked["count"], 1)

    @patch("app.azure_request")
    def test_attachment_sync_removes_only_obsolete_tracked_file(self, azure_request):
        with app_module.database() as db:
            db.execute(
                """INSERT INTO synced_attachments VALUES
                   ('example', 'source', 1, 'https://source/old', 'old.pdf',
                    'example', 'destination', 101, 'https://destination/tracked', 'now')"""
            )
        azure_request.side_effect = [{"relations": [
            {"rel": "AttachedFile", "url": "https://destination/tracked"},
            {"rel": "AttachedFile", "url": "https://destination/manual"},
        ]}, {"id": 101}]
        source = parse_project_url("https://dev.azure.com/example/source")
        destination = parse_project_url("https://dev.azure.com/example/destination")
        added, removed, errors = synchronize_attachments(
            source, destination, "pat", {"id": 1, "relations": []}, 101
        )
        self.assertEqual((added, removed, errors), (0, 1, []))
        patch_document = azure_request.call_args_list[1].kwargs["payload"]
        self.assertEqual(patch_document, [{"op": "remove", "path": "/relations/0"}])
        with app_module.database() as db:
            tracked = db.execute("SELECT COUNT(*) AS count FROM synced_attachments").fetchone()
        self.assertEqual(tracked["count"], 0)

    def test_attachment_download_rejects_untrusted_url(self):
        with self.assertRaises(ValueError):
            azure_attachment_download("https://malicious.example/file", "pat")

    @patch("app.azure_request")
    def test_manual_loading_does_not_restrict_work_item_types(self, azure_request):
        azure_request.side_effect = [
            {"workItems": [{"id": 12}]},
            {"value": [{
                "id": 12,
                "rev": 3,
                "fields": {
                    "System.WorkItemType": "Task",
                    "System.Title": "Existing task",
                    "System.State": "Active",
                },
                "relations": [],
            }]},
        ]
        ref = parse_project_url("https://dev.azure.com/example/source")
        items = load_source_work_items(ref, "test-pat", "manual")
        query = azure_request.call_args_list[0].kwargs["payload"]["query"]
        self.assertNotIn("System.WorkItemType", query)
        self.assertIn("[System.TeamProject] = 'source'", query)
        self.assertFalse(azure_request.call_args_list[0].kwargs["project_scoped"])
        self.assertEqual(items[0]["type"], "Task")

    @patch("app.azure_request")
    def test_work_item_type_filter_is_added_to_live_query(self, azure_request):
        azure_request.return_value = {"workItems": []}
        ref = parse_project_url("https://dev.azure.com/example/source")
        self.assertEqual(load_source_work_items(ref, "test-pat", "type:Test Case"), [])
        query = azure_request.call_args.kwargs["payload"]["query"]
        self.assertIn("[System.WorkItemType] = 'Test Case'", query)

    @patch("app.fetch_work_items")
    def test_child_hierarchy_expands_recursively(self, fetch_work_items):
        fetch_work_items.side_effect = [
            [{
                "id": 1,
                "fields": {
                    "System.TeamProject": "source",
                    "System.WorkItemType": "Epic",
                },
                "relations": [{
                    "rel": "System.LinkTypes.Hierarchy-Forward",
                    "url": "https://dev.azure.com/example/_apis/wit/workItems/2",
                }],
            }],
            [{
                "id": 2,
                "fields": {
                    "System.TeamProject": "source",
                    "System.WorkItemType": "Requirement",
                },
                "relations": [{
                    "rel": "System.LinkTypes.Hierarchy-Forward",
                    "url": "https://dev.azure.com/example/_apis/wit/workItems/3",
                }],
            }],
            [{
                "id": 3,
                "fields": {
                    "System.TeamProject": "source",
                    "System.WorkItemType": "Task",
                },
                "relations": [],
            }],
        ]
        source = parse_project_url("https://dev.azure.com/example/source")
        hierarchy = expand_child_hierarchy(source, "pat", [1])
        self.assertEqual([item["id"] for item in hierarchy], [1, 2, 3])

    @patch("app.expand_work_item_selection")
    @patch("app.session_pat", return_value="read-pat")
    def test_preview_labels_automatically_included_child(self, session_pat, expand_hierarchy):
        items = [
            {
                "id": 1, "rev": 1,
                "fields": {"System.Title": "Epic", "System.WorkItemType": "Epic"},
            },
            {
                "id": 2, "rev": 1,
                "fields": {"System.Title": "Child", "System.WorkItemType": "Requirement"},
            },
        ]
        expand_hierarchy.return_value = (items, {2}, set())
        response = self.client.post("/api/preview", json={
            "source": "https://dev.azure.com/example/source",
            "selectedIds": [1],
            "selectedItems": [{"id": 1, "title": "Epic", "type": "Epic", "rev": 1}],
            "destinations": ["https://dev.azure.com/example/destination"],
            "fields": ["Title"],
            "includeChildren": True,
        })
        self.assertEqual(response.status_code, 200)
        child = next(row for row in response.json["changes"] if row["sourceId"] == 2)
        self.assertEqual(child["status"], "Included child")

    @patch("app.fetch_work_items")
    def test_affected_item_is_included_for_one_level(self, fetch_work_items):
        fetch_work_items.side_effect = [
            [{
                "id": 10,
                "fields": {
                    "System.TeamProject": "source",
                    "System.WorkItemType": "Feature",
                },
                "relations": [{
                    "rel": "Custom.LinkTypes.Affects-Forward",
                    "url": "https://dev.azure.com/example/_apis/wit/workItems/20",
                    "attributes": {"name": "Affects"},
                }],
            }],
            [{
                "id": 20,
                "fields": {
                    "System.TeamProject": "source",
                    "System.WorkItemType": "Requirement",
                },
                "relations": [{
                    "rel": "Custom.LinkTypes.Affects-Forward",
                    "url": "https://dev.azure.com/example/_apis/wit/workItems/30",
                    "attributes": {"name": "Affects"},
                }],
            }],
        ]
        source = parse_project_url("https://dev.azure.com/example/source")
        items, child_ids, affected_ids = expand_work_item_selection(
            source, "pat", [10], include_children=False, include_affected=True
        )
        self.assertEqual([item["id"] for item in items], [10, 20])
        self.assertEqual(child_ids, set())
        self.assertEqual(affected_ids, {20})
        self.assertEqual(fetch_work_items.call_count, 2)

    @patch("app.expand_child_hierarchy")
    @patch("app.fetch_work_items")
    def test_children_of_directly_affected_item_are_included(
        self, fetch_work_items, expand_child_hierarchy
    ):
        feature = {
            "id": 10,
            "fields": {
                "System.TeamProject": "source",
                "System.WorkItemType": "Feature",
            },
            "relations": [{
                "rel": "Custom.LinkTypes.Affects-Forward",
                "url": "https://dev.azure.com/example/_apis/wit/workItems/20",
                "attributes": {"name": "Affects"},
            }],
        }
        requirement = {
            "id": 20,
            "fields": {
                "System.TeamProject": "source",
                "System.WorkItemType": "Requirement",
            },
            "relations": [{
                "rel": "System.LinkTypes.Hierarchy-Forward",
                "url": "https://dev.azure.com/example/_apis/wit/workItems/30",
            }],
        }
        task = {
            "id": 30,
            "fields": {
                "System.TeamProject": "source",
                "System.WorkItemType": "Task",
            },
            "relations": [],
        }
        expand_child_hierarchy.side_effect = [[feature], [requirement, task]]
        fetch_work_items.return_value = [requirement]
        source = parse_project_url("https://dev.azure.com/example/source")

        items, child_ids, affected_ids = expand_work_item_selection(
            source, "pat", [10], include_children=True, include_affected=True
        )

        self.assertEqual([item["id"] for item in items], [10, 20, 30])
        self.assertEqual(child_ids, {30})
        self.assertEqual(affected_ids, {20})
        self.assertEqual(expand_child_hierarchy.call_count, 2)

    def test_export_latest_sync_log_contains_item_change_rows(self):
        with app_module.database() as db:
            db.execute(
                """INSERT INTO sync_runs
                   (run_id, started_at, completed_at, mode, source_project,
                    destinations, created_count, updated_count, skipped_count,
                    failed_count, status)
                   VALUES ('SYNC-TEST', 'start', 'finish', 'live', 'source',
                           1, 1, 0, 0, 0, 'completed')"""
            )
            db.execute(
                """INSERT INTO sync_run_items
                   (run_id, source_id, title, work_item_type, destination,
                    destination_id, action, status, error)
                   VALUES ('SYNC-TEST', 12, 'Feature title', 'Feature',
                           'destination', 99, 'Created', 'Success', '')"""
            )
        response = self.client.get("/api/export/latest")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        exported = response.data.decode("utf-8-sig")
        self.assertIn("SYNC-TEST", exported)
        self.assertIn("Feature title", exported)
        self.assertIn("destination", exported)
        export_folder = os.path.join(self.temp_directory.name, "Exported Logs")
        with patch("app.export_log_directory", return_value=export_folder):
            saved = self.client.post("/api/export/latest/save", json={})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(os.path.isfile(saved.json["path"]))
        self.assertIn(export_folder, saved.json["path"])

    def test_export_latest_sync_log_reports_when_no_run_exists(self):
        response = self.client.get("/api/export/latest")
        self.assertEqual(response.status_code, 404)
        self.assertIn("No completed", response.json["message"])


if __name__ == "__main__":
    unittest.main()
