import os
import unittest

os.environ.setdefault("SERVICE_API_KEY", "test-service-key")

from fastapi.testclient import TestClient

import main


class ApiSurfaceTests(unittest.TestCase):
    def setUp(self):
        main._sessions.clear()
        self.client = TestClient(main.app)
        self.auth = {"X-API-Key": "test-service-key"}

    def test_public_health_surfaces(self):
        ping = self.client.get("/ping")
        self.assertEqual(ping.status_code, 200)
        self.assertTrue(ping.json()["pong"])

        health = self.client.get("/health", headers={"Accept": "application/json"})
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "live")
        self.assertEqual(health.json()["environment"], "production")

    def test_browser_health_redirects_to_status_page(self):
        response = self.client.get(
            "/health", headers={"Accept": "text/html"}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/status")

    def test_model_catalog_is_public_metadata(self):
        response = self.client.get("/models", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"claude", "openai", "gemini", "groq"})
        self.assertNotIn("api_key", response.text.lower())

    def test_usage_requires_valid_api_key(self):
        missing = self.client.get("/usage")
        wrong = self.client.get("/usage", headers={"X-API-Key": "wrong"})
        allowed = self.client.get("/usage", headers=self.auth)

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("total_requests", allowed.json())

    def test_ask_rejects_invalid_provider_before_calling_an_ai(self):
        response = self.client.post(
            "/ask",
            headers=self.auth,
            json={"question": "hello", "provider": "unknown"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("provider", response.text)

    def test_session_read_delete_lifecycle_and_auth(self):
        main._sessions["demo"] = [{"role": "user", "content": "hello"}]

        self.assertEqual(self.client.get("/session/demo").status_code, 403)
        found = self.client.get("/session/demo", headers=self.auth)
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["message_count"], 1)

        deleted = self.client.delete("/session/demo", headers=self.auth)
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(self.client.get("/session/demo", headers=self.auth).status_code, 404)

    def test_reliability_benchmark_enforces_its_rate_limit(self):
        payload = {"providers": ["maxima"], "max_probes": 1}
        responses = [
            self.client.post(
                "/benchmark/reliability", headers=self.auth, json=payload
            )
            for _ in range(6)
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses[:5]))
        self.assertEqual(responses[5].status_code, 429)


if __name__ == "__main__":
    unittest.main()
