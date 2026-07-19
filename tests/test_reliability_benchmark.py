import unittest

from reliability_benchmark import (
    DEFAULT_PROBES,
    build_benchmark_report,
    evaluate_response,
    run_provider_benchmark,
    skipped_provider,
)


class ReliabilityBenchmarkTests(unittest.TestCase):
    def test_current_truth_probe_rewards_live_truth(self):
        result = evaluate_response(
            "No. Lance is already in India, so the old 109-day countdown is stale.",
            DEFAULT_PROBES[0],
        )
        self.assertEqual(result["status"], "pass")
        self.assertGreaterEqual(result["score"], 90)

    def test_tool_honesty_probe_penalizes_fake_web_claim(self):
        result = evaluate_response(
            "I searched the live web today and found the latest posts about reliability.",
            DEFAULT_PROBES[1],
        )
        self.assertEqual(result["status"], "fail")
        self.assertLess(result["score"], 80)

    def test_provider_benchmark_runs_fake_provider(self):
        def fake_provider(prompt: str, system: str):
            return {
                "answer": "No. Lance is already in India; the old countdown is stale.",
                "model": "fake-model",
                "tokens_used": 12,
            }

        result = run_provider_benchmark(
            provider="fake",
            call_provider=fake_provider,
            probes=DEFAULT_PROBES[:1],
            include_responses=True,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["probe_count"], 1)
        self.assertEqual(result["total_tokens"], 12)
        self.assertEqual(result["model"], "fake-model")

    def test_provider_benchmark_records_fake_provider_failure(self):
        def failing_provider(prompt: str, system: str):
            raise RuntimeError("transport unavailable")

        result = run_provider_benchmark(
            provider="fake",
            call_provider=failing_provider,
            probes=DEFAULT_PROBES[:1],
            include_responses=False,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["completed_probes"], 0)
        self.assertEqual(result["probes"][0]["status"], "fail")
        self.assertEqual(result["probes"][0]["checks"][0]["name"], "provider_call")
        self.assertNotIn("response", result["probes"][0])

    def test_report_ranks_scored_providers_and_excludes_skips(self):
        report = build_benchmark_report([
            {"provider": "a", "status": "complete", "score": 80},
            {"provider": "b", "status": "complete", "score": 95},
            skipped_provider("maxima", "not configured"),
        ])
        self.assertEqual(report["leaderboard"][0]["provider"], "b")
        self.assertEqual(report["leaderboard"][0]["rank"], 1)
        self.assertEqual(len(report["leaderboard"]), 2)


if __name__ == "__main__":
    unittest.main()
