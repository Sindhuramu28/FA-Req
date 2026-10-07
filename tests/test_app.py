import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from app import app, load_source_work_items, parse_project_url, work_item_patch


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


if __name__ == "__main__":
    unittest.main()
