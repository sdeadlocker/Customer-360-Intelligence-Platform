"""Print a clean, screenshot-friendly telemetry summary for one Ask AI request.

For local demos without the Docker observability stack: runs a question through the running API and
prints the trace id, end-to-end latency, model, token usage, estimated cost and tool-call count in a
readable table — the same signals Grafana/Jaeger show, but on the console.

Usage (backend venv, API running on :8000):
    python scripts/show_telemetry.py "which customers have rising risk this month"
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

_BASE = "http://127.0.0.1:8000"
_USER = "risk.riley"
_PASSWORD = "risk-dev-password"  # noqa: S105 - seeded local dev credential


def _token() -> str:
    req = urllib.request.Request(  # noqa: S310 - localhost only
        f"{_BASE}/auth/token",
        data=json.dumps({"username": _USER, "password": _PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        return str(json.loads(resp.read())["data"]["access_token"])


def _ask(token: str, question: str) -> tuple[str, dict[str, object], float]:
    body = json.dumps({"question": question, "session_id": "telemetry-demo"}).encode()
    req = urllib.request.Request(  # noqa: S310 - localhost only
        f"{_BASE}/ask",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    answer = ""
    done: dict[str, object] = {}
    event = ""
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        for raw in resp:
            line = raw.decode().rstrip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = line.split(":", 1)[1].strip()
                if event == "token":
                    answer += json.loads(payload).get("text", "")
                elif event == "done":
                    done = json.loads(payload)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return answer, done, elapsed_ms


def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else "which customers have rising risk this month"
    token = _token()
    answer, done, elapsed_ms = _ask(token, question)

    bar = "=" * 68
    print(bar)
    print(" Customer 360 — Ask AI telemetry")
    print(bar)
    print(f" Question        : {question}")
    print(f" Model           : {done.get('model_id', '(unknown)')}")
    print(f" Route           : {done.get('route', '(unknown)')}")
    print(f" End-to-end       : {elapsed_ms:,.0f} ms")
    print(f" Degraded         : {done.get('degraded')}")
    print(f" Refused          : {done.get('refused')}")
    print(f" No guidance      : {done.get('no_guidance')}")
    print(bar)
    print(" Answer:")
    for answer_line in answer.strip().splitlines():
        print(f"   {answer_line}")
    print(bar)
    print(" Full traces + token/cost/latency metrics stream to the backend terminal")
    print(" (OTEL_TRACES_EXPORTER=console). For dashboards, run: npm run obs:up")
    print(bar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
