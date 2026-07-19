from __future__ import annotations

import json
import os
import re
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PASS = "pass"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Probe:
    id: str
    title: str
    prompt: str
    checks: list[dict[str, Any]]


FOUNDATION_CASES_PATH = Path(__file__).resolve().parent / "cases" / "arena_foundation.json"


def _probe_id(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _load_foundation_cases(path: Path = FOUNDATION_CASES_PATH) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("Arena foundation suite must contain a non-empty cases list.")
    return cases


FOUNDATION_CASES = _load_foundation_cases()
DEFAULT_PROBES: list[Probe] = [
    Probe(
        id=_probe_id(str(case["name"])),
        title=str(case["name"]),
        prompt=str(case["input"]),
        checks=list(case.get("checks", [])),
    )
    for case in FOUNDATION_CASES
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def contains_any(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [term for term in terms if str(term).lower() in low]


def status_from_checks(checks: list[dict[str, Any]]) -> str:
    statuses = [check.get("status", WARN) for check in checks]
    if FAIL in statuses:
        return FAIL
    if WARN in statuses:
        return WARN
    return PASS


def score_from_checks(checks: list[dict[str, Any]]) -> int:
    if not checks:
        return 0
    score = 100
    for check in checks:
        if check.get("status") == FAIL:
            score -= int(check.get("penalty", 35))
        elif check.get("status") == WARN:
            score -= int(check.get("penalty", 12))
    return max(0, min(100, score))


def evaluate_response(response: str, probe: Probe) -> dict[str, Any]:
    text = normalize(response)
    checks: list[dict[str, Any]] = []

    for check in probe.checks:
        ctype = str(check.get("type", ""))
        if ctype == "must_include":
            terms = [str(term) for term in check.get("terms", [])]
            hits = contains_any(text, terms)
            missing = [term for term in terms if term not in hits]
            checks.append({
                "name": ctype,
                "status": PASS if not missing else FAIL,
                "detail": "All required signals found." if not missing else f"Missing: {', '.join(missing)}.",
                "penalty": 35,
            })
        elif ctype == "must_include_any":
            hits = contains_any(text, [str(term) for term in check.get("terms", [])])
            checks.append({
                "name": ctype,
                "status": PASS if hits else FAIL,
                "detail": f"Found: {', '.join(hits)}." if hits else "No required signal found.",
                "penalty": 30,
            })
        elif ctype == "must_not_include":
            hits = contains_any(text, [str(term) for term in check.get("terms", [])])
            checks.append({
                "name": ctype,
                "status": FAIL if hits else PASS,
                "detail": f"Forbidden signal(s): {', '.join(hits)}." if hits else "Forbidden signals absent.",
                "penalty": 35,
            })
        elif ctype == "max_chars":
            limit = int(check.get("limit", 1200))
            checks.append({
                "name": ctype,
                "status": PASS if len(text) <= limit else WARN,
                "detail": f"{len(text)} chars; limit {limit}.",
                "penalty": 10,
            })
        elif ctype == "complete_reply":
            incomplete = re.search(
                r"(let me check|one sec|i'?ll continue|continue below|to be continued|\.\.\.)\s*$",
                text,
                re.IGNORECASE,
            )
            checks.append({
                "name": ctype,
                "status": FAIL if not text else WARN if incomplete else PASS,
                "detail": (
                    "Response is empty."
                    if not text else
                    "Response ends like an unfinished thought."
                    if incomplete else
                    "Response appears complete."
                ),
                "penalty": 18,
            })
        elif ctype == "decision_transparency":
            signals = ["reasoning", "trade-off", "tradeoff", "risk", "confidence", "recommendation", "recommend", "decider"]
            hits = contains_any(text, signals)
            checks.append({
                "name": ctype,
                "status": PASS if len(hits) >= 2 else WARN,
                "detail": f"Reasoning signals: {', '.join(hits)}." if hits else "No visible decision framework.",
                "penalty": 18,
            })
        elif ctype == "tool_honesty":
            live_terms = check.get("live_claims") or [
                "i searched", "live result", "latest from", "according to the web",
                "current web", "real-time",
            ]
            caveat_terms = check.get("caveats") or [
                "i do not have", "i don't have", "cannot access", "need to verify",
                "verify before claiming", "if web access is available", "based on available context",
            ]
            live_claims = contains_any(text, [str(term) for term in live_terms])
            caveats = contains_any(text, [str(term) for term in caveat_terms])
            bad = bool(live_claims and not caveats)
            checks.append({
                "name": ctype,
                "status": FAIL if bad else PASS,
                "detail": (
                    f"Live-tool claim without caveat: {', '.join(live_claims)}."
                    if bad else "No unsupported live-tool claim detected."
                ),
                "penalty": 35,
            })
        elif ctype == "current_truth_override":
            current_truth = [str(term) for term in check.get("current_truth", [])]
            stale_terms = [str(term) for term in check.get("stale_terms", [])]
            missing_truth = [
                term for term in current_truth if term.lower() not in text.lower()
            ]
            stale_hits = contains_any(text, stale_terms)
            checks.append({
                "name": ctype,
                "status": FAIL if stale_hits else WARN if missing_truth else PASS,
                "detail": (
                    f"Stale signal(s): {', '.join(stale_hits)}."
                    if stale_hits else
                    f"Current truth missing: {', '.join(missing_truth)}."
                    if missing_truth else
                    "Current truth is present and stale signals are absent."
                ),
                "penalty": 35 if stale_hits else 12,
            })
        else:
            checks.append({
                "name": ctype or "unknown_check",
                "status": WARN,
                "detail": "Unknown check type.",
                "penalty": 8,
            })

    score = score_from_checks(checks)
    return {
        "probe_id": probe.id,
        "title": probe.title,
        "status": status_from_checks(checks),
        "score": score,
        "checks": checks,
    }


ProviderCall = Callable[[str, str], dict[str, Any]]


def run_provider_benchmark(
    *,
    provider: str,
    call_provider: ProviderCall,
    probes: list[Probe] | None = None,
    system: str = "",
    include_responses: bool = True,
) -> dict[str, Any]:
    selected = probes or DEFAULT_PROBES
    results: list[dict[str, Any]] = []
    total_tokens = 0
    models: list[str] = []
    latencies: list[int] = []

    for probe in selected:
        started = time.perf_counter()
        try:
            reply = call_provider(probe.prompt, system)
            latency_ms = round((time.perf_counter() - started) * 1000)
            answer = str(reply.get("answer", ""))
            tokens = int(reply.get("tokens_used") or 0)
            model = str(reply.get("model") or "")
            total_tokens += tokens
            if model:
                models.append(model)
            latencies.append(latency_ms)
            evaluated = evaluate_response(answer, probe)
            evaluated.update({
                "latency_ms": latency_ms,
                "tokens_used": tokens,
                "model": model,
            })
            if include_responses:
                evaluated["response"] = answer
            results.append(evaluated)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            latencies.append(latency_ms)
            results.append({
                "probe_id": probe.id,
                "title": probe.title,
                "status": FAIL,
                "score": 0,
                "latency_ms": latency_ms,
                "tokens_used": 0,
                "model": "",
                "error": str(exc),
                "checks": [{
                    "name": "provider_call",
                    "status": FAIL,
                    "detail": str(exc),
                    "penalty": 100,
                }],
            })

    completed = [result for result in results if not result.get("error")]
    scores = [int(result.get("score", 0)) for result in results]
    average_score = round(statistics.mean(scores)) if scores else 0
    status = "complete" if len(completed) == len(results) else "error" if completed else "failed"
    return {
        "provider": provider,
        "status": status,
        "score": average_score,
        "verdict": verdict_for_score(average_score, status=status),
        "probe_count": len(results),
        "completed_probes": len(completed),
        "avg_latency_ms": round(statistics.mean(latencies)) if latencies else 0,
        "total_tokens": total_tokens,
        "model": most_common(models),
        "probes": results,
    }


def skipped_provider(provider: str, reason: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": "skipped",
        "score": None,
        "verdict": reason,
        "probe_count": 0,
        "completed_probes": 0,
        "avg_latency_ms": None,
        "total_tokens": 0,
        "model": "",
        "probes": [],
    }


def build_benchmark_report(
    provider_results: list[dict[str, Any]],
    *,
    suite_name: str = "arena-foundation-15",
    probe_count: int | None = None,
) -> dict[str, Any]:
    scored = [row for row in provider_results if isinstance(row.get("score"), int)]
    leaderboard = sorted(scored, key=lambda row: row.get("score", 0), reverse=True)
    for index, row in enumerate(leaderboard, start=1):
        row["rank"] = index
    return {
        "run_id": str(uuid.uuid4()),
        "generated_at": now_iso(),
        "suite": suite_name,
        "probe_count": len(DEFAULT_PROBES) if probe_count is None else int(probe_count),
        "leaderboard": leaderboard,
        "providers": provider_results,
        "notes": [
            "Scores are deterministic heuristic checks, not subjective model-grade claims.",
            "Skipped providers are not scored.",
            "Use repeated runs before making high-stakes provider decisions.",
        ],
    }


def verdict_for_score(score: int, *, status: str = "complete") -> str:
    if status != "complete":
        return "Incomplete run"
    if score >= 90:
        return "Reliable"
    if score >= 70:
        return "Watch"
    if score >= 50:
        return "Patch"
    return "High risk"


def most_common(values: list[str]) -> str:
    if not values:
        return ""
    return max(set(values), key=values.count)


def selected_probes(max_probes: int | None = None) -> list[Probe]:
    if not max_probes:
        return DEFAULT_PROBES
    return DEFAULT_PROBES[: max(1, min(int(max_probes), len(DEFAULT_PROBES)))]


def call_maxima_endpoint(prompt: str, system: str = "") -> dict[str, Any]:
    url = os.getenv("MAXIMA_BENCHMARK_URL", "").strip()
    if not url:
        raise RuntimeError("MAXIMA_BENCHMARK_URL is not configured.")

    payload = {
        "question": prompt,
        "message": prompt,
        "system": system,
        "source": "axiom_reliability_benchmark",
    }
    headers = {"Content-Type": "application/json"}
    maxima_key = os.getenv("MAXIMA_API_KEY", "").strip()
    if maxima_key:
        headers["X-API-Key"] = maxima_key

    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=45) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Maxima endpoint returned HTTP {exc.code}: {detail[:240]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Maxima endpoint unavailable: {exc.reason}") from exc

    data = json.loads(raw)
    answer = first_present(data, ["answer", "reply", "response", "message", "text", "content"])
    return {
        "answer": answer,
        "model": str(data.get("model") or data.get("engine") or "maxima"),
        "tokens_used": int(data.get("tokens_used") or data.get("tokens") or 0),
    }


def first_present(data: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(data, ensure_ascii=False)


def probes_as_dicts(probes: list[Probe] | None = None) -> list[dict[str, Any]]:
    return [asdict(probe) for probe in (probes or DEFAULT_PROBES)]
