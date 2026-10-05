import unittest

from app import app, parse_project_url


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


if __name__ == "__main__":
    unittest.main()
