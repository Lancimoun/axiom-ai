import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

os.environ.setdefault("SERVICE_API_KEY", "test-service-key")

from fastapi import HTTPException
from fastapi.testclient import TestClient

import main


def parse_sse(body: str):
    events = []
    for block in body.strip().split("\n\n"):
        event_type = "message"
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        if data_lines:
            events.append((event_type, json.loads("\n".join(data_lines))))
    return events


class FakeOpenAIStream:
    def __init__(self, chunks, *, final_error=None):
        self._chunks = iter(chunks)
        self._final_error = final_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            text = next(self._chunks)
        except StopIteration:
            if self._final_error is not None:
                error = self._final_error
                self._final_error = None
                raise error
            raise StopAsyncIteration
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
        )


class FakeOpenAICompletions:
    def __init__(self, stream):
        self.stream = stream
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream


class FakeAsyncOpenAI:
    def __init__(self, stream):
        self.completions = FakeOpenAICompletions(stream)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeGeminiStream:
    def __init__(self, chunks, *, tokens=0, final_error=None):
        self._chunks = iter(chunks)
        self._final_error = final_error
        self.usage_metadata = SimpleNamespace(total_token_count=tokens)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            text = next(self._chunks)
        except StopIteration:
            if self._final_error is not None:
                error = self._final_error
                self._final_error = None
                raise error
            raise StopAsyncIteration
        return SimpleNamespace(text=text)


class FakeGeminiModels:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAsyncGeminiModels:
    def __init__(self, stream):
        self.stream = stream
        self.calls = []

    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        return self.stream


class FakeGeminiClient:
    def __init__(self, *, response=None, error=None, stream=None):
        self.models = FakeGeminiModels(response=response, error=error)
        self.async_models = FakeAsyncGeminiModels(stream)
        self.aio = SimpleNamespace(models=self.async_models)


class RaisingCreate:
    def __init__(self, error):
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise self.error


def fake_chat_client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=create))


class ApiSurfaceTests(unittest.TestCase):
    def setUp(self):
        main._sessions.clear()
        main._usage["total_requests"] = 0
        main._usage["total_tokens"] = 0
        for provider in main._usage["by_provider"]:
            main._usage["by_provider"][provider] = 0
        for endpoint in main._usage["by_endpoint"]:
            main._usage["by_endpoint"][endpoint] = 0
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
        self.assertEqual(health.json()["providers"]["gemini"]["sdk"], "google-genai")

    def test_gemini_uses_supported_google_genai_dependency_only(self):
        root = Path(main.__file__).resolve().parent
        requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        source = (root / "main.py").read_text(encoding="utf-8")

        self.assertIn("google-genai", requirements)
        self.assertNotIn("google-generativeai", requirements)
        self.assertNotIn("google.generativeai", source)

    def test_browser_health_redirects_to_status_page(self):
        response = self.client.get(
            "/health", headers={"Accept": "text/html"}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/status")

    def test_landing_page_exposes_tested_failure_contracts_safely(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="failure-lab"', response.text)
        self.assertIn("Contract replay — no provider call", response.text)
        self.assertIn("30 local tests", response.text)
        self.assertIn("never emits a false terminal", response.text)
        self.assertIn("prefers-reduced-motion", response.text)
        self.assertIn("const reduceMotion", response.text)
        self.assertIn("three.module.js", response.text)
        self.assertNotIn("three.min.js", response.text)
        self.assertIn("gpt-5.5", response.text.lower())
        self.assertIn("gemini 3.5 pro", response.text.lower())
        self.assertNotIn("All Systems Online", response.text)
        self.assertNotIn("GPT&#8209;4o", response.text)

    def test_model_catalog_is_public_metadata(self):
        response = self.client.get("/models", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"claude", "openai", "gemini", "groq"})
        self.assertEqual(response.json()["gemini"]["sdk"], "google-genai")
        self.assertNotIn("api_key", response.text.lower())

    def test_benchmark_catalog_exposes_fifteen_local_probes(self):
        response = self.client.get("/benchmark/probes")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["suite"], "arena-foundation-15")
        self.assertEqual(response.json()["probe_count"], 15)
        self.assertEqual(len(response.json()["probes"]), 15)

        request = main.ReliabilityBenchmarkRequest(
            providers=["maxima"], max_probes=15
        )
        self.assertEqual(request.max_probes, 15)
        with self.assertRaises(ValueError):
            main.ReliabilityBenchmarkRequest(providers=["maxima"], max_probes=16)

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

    def test_ask_reports_unconfigured_provider_without_recording_usage(self):
        with patch.object(main, "claude_client", None):
            response = self.client.post(
                "/ask",
                headers=self.auth,
                json={"question": "hello", "provider": "claude"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.json()["detail"].lower())
        self.assertEqual(main._usage["total_requests"], 0)
        self.assertEqual(main._usage["total_tokens"], 0)

    def test_sync_provider_failures_are_sanitized_across_all_adapters(self):
        secret = "SECRET-UPSTREAM-DETAIL"

        claude_create = RaisingCreate(RuntimeError(secret))
        with patch.object(
            main,
            "claude_client",
            SimpleNamespace(messages=claude_create),
        ):
            claude = self.client.post(
                "/ask",
                headers=self.auth,
                json={"question": "hello", "provider": "claude"},
            )

        openai_create = RaisingCreate(RuntimeError(secret))
        with patch.object(main, "openai_client", fake_chat_client(openai_create)):
            openai_response = self.client.post(
                "/ask",
                headers=self.auth,
                json={"question": "hello", "provider": "openai"},
            )

        gemini_client = FakeGeminiClient(error=RuntimeError(secret))
        with (
            patch.object(main, "_gemini_available", True),
            patch.object(main, "_gemini_client", gemini_client),
        ):
            gemini = self.client.post(
                "/ask",
                headers=self.auth,
                json={"question": "hello", "provider": "gemini"},
            )

        groq_create = RaisingCreate(RuntimeError(secret))
        with (
            patch.object(main, "_groq_available", True),
            patch.object(main, "_groq_client", fake_chat_client(groq_create)),
        ):
            groq = self.client.post(
                "/ask",
                headers=self.auth,
                json={"question": "hello", "provider": "groq"},
            )

        responses = {
            "Claude": claude,
            "OpenAI": openai_response,
            "Gemini": gemini,
            "Groq": groq,
        }
        for provider, response in responses.items():
            with self.subTest(provider=provider):
                self.assertEqual(response.status_code, 502)
                self.assertEqual(
                    response.json()["detail"], f"{provider} API request failed."
                )
                self.assertNotIn(secret, response.text)

    def test_gemini_sync_preserves_multi_turn_roles_and_usage(self):
        gemini_client = FakeGeminiClient(
            response=SimpleNamespace(
                text="The supported SDK answered.",
                usage_metadata=SimpleNamespace(total_token_count=37),
            )
        )
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up"},
        ]

        with (
            patch.object(main, "_gemini_available", True),
            patch.object(main, "_gemini_client", gemini_client),
        ):
            answer, tokens = main._ask_gemini(
                messages, "gemini-3.5-flash", "System contract"
            )

        self.assertEqual(answer, "The supported SDK answered.")
        self.assertEqual(tokens, 37)
        self.assertEqual(len(gemini_client.models.calls), 1)
        call = gemini_client.models.calls[0]
        self.assertEqual(call["model"], "gemini-3.5-flash")
        self.assertEqual(
            [content.role for content in call["contents"]],
            ["user", "model", "user"],
        )
        self.assertEqual(
            [content.parts[0].text for content in call["contents"]],
            ["First question", "First answer", "Follow-up"],
        )
        self.assertEqual(call["config"].system_instruction, "System contract")
        self.assertEqual(call["config"].max_output_tokens, 1536)

    def test_openai_rate_limit_has_one_application_attempt(self):
        request = httpx.Request(
            "POST", "https://api.openai.com/v1/chat/completions"
        )
        upstream_response = httpx.Response(429, request=request)
        rate_error = main.openai.RateLimitError(
            "rate limited", response=upstream_response, body=None
        )
        create = RaisingCreate(rate_error)

        with (
            patch.object(main, "openai_client", fake_chat_client(create)),
            patch.object(main.time, "sleep") as sleep,
        ):
            response = self.client.post(
                "/ask",
                headers=self.auth,
                json={"question": "hello", "provider": "openai"},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(create.calls), 1)
        sleep.assert_not_called()

    def test_chat_provider_failure_does_not_commit_half_turn(self):
        original = [{"role": "assistant", "content": "ready"}]
        main._sessions["demo"] = [dict(message) for message in original]

        with patch.object(
            main,
            "_route",
            side_effect=HTTPException(status_code=503, detail="Provider unavailable."),
        ):
            response = self.client.post(
                "/chat",
                headers=self.auth,
                json={
                    "message": "this should not persist",
                    "session_id": "demo",
                    "provider": "claude",
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(main._sessions["demo"], original)
        self.assertEqual(main._usage["total_requests"], 0)

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

    def test_stream_rejects_known_unconfigured_provider_before_sse_starts(self):
        with patch.object(main, "async_claude", None):
            response = self.client.post(
                "/stream",
                headers=self.auth,
                json={"question": "hello", "provider": "claude"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Claude not configured. Add ANTHROPIC_API_KEY to environment.",
        )
        self.assertEqual(main._usage["total_requests"], 0)

    def test_stream_emits_ordered_tokens_and_one_terminal_done_event(self):
        fake_client = FakeAsyncOpenAI(FakeOpenAIStream(["Hello", " world"]))
        with patch.object(main, "async_openai", fake_client):
            response = self.client.post(
                "/stream",
                headers=self.auth,
                json={"question": "hello", "provider": "openai"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(
            parse_sse(response.text),
            [
                ("message", {"token": "Hello"}),
                ("message", {"token": " world"}),
                ("message", {"done": True, "model": "gpt-5.4-mini"}),
            ],
        )
        self.assertEqual(len(fake_client.completions.calls), 1)
        self.assertTrue(fake_client.completions.calls[0]["stream"])

    def test_stream_sanitizes_partial_provider_failure_and_never_emits_done(self):
        fake_client = FakeAsyncOpenAI(
            FakeOpenAIStream(
                ["partial"],
                final_error=RuntimeError("provider exploded with SECRET-DETAIL"),
            )
        )
        with patch.object(main, "async_openai", fake_client):
            response = self.client.post(
                "/stream",
                headers=self.auth,
                json={"question": "hello", "provider": "openai"},
            )

        events = parse_sse(response.text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events[0], ("message", {"token": "partial"}))
        self.assertEqual(
            events[1],
            (
                "error",
                {
                    "error": "OpenAI stream failed.",
                    "code": "upstream_failure",
                    "retryable": False,
                },
            ),
        )
        self.assertEqual(len(events), 2)
        self.assertNotIn("done", response.text)
        self.assertNotIn("SECRET-DETAIL", response.text)

    def test_gemini_stream_emits_tokens_usage_and_one_terminal_done_event(self):
        gemini_client = FakeGeminiClient(
            stream=FakeGeminiStream(["Gemini", " stream"], tokens=23)
        )
        with (
            patch.object(main, "_gemini_available", True),
            patch.object(main, "_gemini_client", gemini_client),
        ):
            response = self.client.post(
                "/stream",
                headers=self.auth,
                json={"question": "hello", "provider": "gemini"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            parse_sse(response.text),
            [
                ("message", {"token": "Gemini"}),
                ("message", {"token": " stream"}),
                (
                    "message",
                    {
                        "done": True,
                        "tokens_used": 23,
                        "model": "gemini-3.5-flash",
                    },
                ),
            ],
        )
        self.assertEqual(len(gemini_client.async_models.calls), 1)
        call = gemini_client.async_models.calls[0]
        self.assertEqual(call["contents"], "hello")
        self.assertEqual(call["config"].max_output_tokens, 800)

    def test_gemini_stream_sanitizes_partial_failure_without_done(self):
        gemini_client = FakeGeminiClient(
            stream=FakeGeminiStream(
                ["partial"],
                final_error=RuntimeError("SECRET-GEMINI-DETAIL"),
            )
        )
        with (
            patch.object(main, "_gemini_available", True),
            patch.object(main, "_gemini_client", gemini_client),
        ):
            response = self.client.post(
                "/stream",
                headers=self.auth,
                json={"question": "hello", "provider": "gemini"},
            )

        events = parse_sse(response.text)
        self.assertEqual(events[0], ("message", {"token": "partial"}))
        self.assertEqual(
            events[1],
            (
                "error",
                {
                    "error": "Gemini stream failed.",
                    "code": "upstream_failure",
                    "retryable": False,
                },
            ),
        )
        self.assertEqual(len(events), 2)
        self.assertNotIn("done", response.text)
        self.assertNotIn("SECRET-GEMINI-DETAIL", response.text)


if __name__ == "__main__":
    unittest.main()
