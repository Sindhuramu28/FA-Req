import unittest
from unittest.mock import patch

from app import app, load_source_work_items, parse_project_url


class AppTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

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

    def test_demo_items(self):
        response = self.client.get("/api/demo-items")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["items"]), 6)

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
