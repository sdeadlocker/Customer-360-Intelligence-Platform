"""Export the OpenAPI specification to ``frontend/openapi.json``.

The frontend generates its API types from a *committed* spec file and fails the build when the
committed types drift from it (``frontend/scripts/check-codegen-drift.mjs``). Until now there was no
script to produce that file, so re-exporting meant starting the API and fetching ``/openapi.json``
by hand — a manual step easy to forget, which is exactly how a backend route ends up invisible to
the typed client.

This closes the loop:

    node scripts/backend.mjs scripts/export_openapi.py   # or: npm run openapi:export
    npm --prefix frontend run codegen

No server and no database are needed. ``create_app`` builds the service container lazily, so the
route table — which is all the spec describes — is available from the app object alone.

Formatting matches the committed file exactly: two-space indent, FastAPI's own key ordering, ASCII
escapes for non-ASCII characters (the docstrings are full of ``§``, and the committed file carries
them as ``\\u00a7``), and a trailing newline. Byte-identical output when nothing changed is the
point — otherwise every export rewrites the whole file and buries the one route that actually moved.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Final

#: Repository root, two levels above this file (``backend/scripts/export_openapi.py``).
_ROOT: Final = Path(__file__).resolve().parents[2]
_TARGET: Final = _ROOT / "frontend" / "openapi.json"

#: The spec depends only on the route table, so the app is built in the cheapest configuration that
#: constructs cleanly: no telemetry exporters, no real model provider, no identity provider round
#: trips. These are process-local and never written to a file.
_EXPORT_ENV: Final[dict[str, str]] = {
    "ENVIRONMENT": "local",
    "LLM_PROVIDER": "mock",
    "AUTH_PROVIDER": "local",
    "OTEL_ENABLED": "false",
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
}


def build_spec() -> dict[str, Any]:
    """Construct the app and return its OpenAPI document."""
    for key, value in _EXPORT_ENV.items():
        os.environ.setdefault(key, value)

    # Imported after the environment is set so `Settings` reads the export configuration.
    from c360.core.config import reset_settings_cache  # noqa: PLC0415
    from c360.main import create_app  # noqa: PLC0415

    reset_settings_cache()
    spec: dict[str, Any] = create_app().openapi()
    return spec


def main() -> int:
    spec = build_spec()
    rendered = json.dumps(spec, indent=2) + "\n"
    previous = _TARGET.read_text(encoding="utf-8") if _TARGET.is_file() else ""
    if rendered == previous:
        print(f"openapi.json is already up to date ({len(spec['paths'])} paths)")
        return 0
    _TARGET.write_text(rendered, encoding="utf-8")
    print(f"wrote {_TARGET.relative_to(_ROOT)} ({len(spec['paths'])} paths)")
    print("next: npm --prefix frontend run codegen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
