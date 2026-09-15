"""The artifact store and the delivery port (task 18.4).

Two concerns, both deliberately behind seams so the build stays offline and no outbound mail is ever
sent:

* :class:`ArtifactStore` writes a generated artifact to the ``data/`` volume and returns its path.
  Artifacts are files, never database rows (task 18.1) — the ``report_run`` row carries only the
  path.
* :class:`Deliverer` is the delivery port. The default :class:`FileDeliverer` "delivers" a digest by
  writing the artifact and a small manifest to ``data/`` — the offline mock the phase gate requires.
  :class:`SmtpDeliverer` is the seam a real deployment fills to send email; it is not wired in the
  build and raises if used without configuration, so the build never makes an outbound connection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from c360.reports.models import GeneratedArtifact, ReportDefinition


def _safe_name(text: str) -> str:
    """A filesystem-safe slug for a filename component (no path separators, bounded length)."""
    keep = [c if c.isalnum() or c in ("-", "_") else "_" for c in text]
    slug = "".join(keep).strip("_")
    return (slug or "report")[:64]


class ArtifactStore:
    """Writes generated artifacts to the ``data/`` volume and returns their paths (task 18.1)."""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, run_id: int, definition: ReportDefinition, artifact: GeneratedArtifact) -> Path:
        """Write the artifact under ``<root>/run-<id>-<type>-<title>.<ext>`` and return its path.

        The filename is derived from the run id (unique) plus a slug of the type and title, so a run
        is locatable on disk without opening the database and two runs never collide.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        name = (
            f"run-{run_id:06d}-{_safe_name(str(definition.report_type))}-"
            f"{_safe_name(definition.title)}.{artifact.file_extension}"
        )
        path = self._root / name
        path.write_bytes(artifact.content)
        return path


class Deliverer(Protocol):
    """The delivery port. A real deployment supplies an SMTP-backed implementation (task 18.4)."""

    def deliver(self, *, artifact_path: Path, definition: ReportDefinition, run_id: int) -> Path:
        """Deliver the artifact for a run. Returns a delivery-receipt path (mock) or manifest."""
        ...


class FileDeliverer:
    """The offline mock deliverer: writes a delivery manifest beside the artifact (task 18.4).

    The default in the build. "Delivering" a digest means recording — next to the artifact on the
    ``data/`` volume — a small JSON manifest naming what was delivered, to whom (the definition
    owner) and where the artifact lives. No mail is sent; the phase gate's "delivered via the mock
    deliverer to ``data/``" is exactly this file appearing.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = root

    def deliver(self, *, artifact_path: Path, definition: ReportDefinition, run_id: int) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run_id,
            "definition_id": definition.definition_id,
            "report_type": str(definition.report_type),
            "title": definition.title,
            "owner_id": definition.owner_id,
            "owner_role": definition.owner_role,
            "artifact": artifact_path.name,
            "delivered_via": "file",
        }
        path = self._root / f"run-{run_id:06d}-manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return path


class SmtpDeliverer:
    """The real-email seam (task 18.4). Not wired in the build; raises if used unconfigured.

    Present so the port has a concrete SMTP implementation to grow into without a code change to the
    scheduler — the scheduler depends only on :class:`Deliverer`. Constructing it without SMTP
    configuration and calling :meth:`deliver` raises, so the build can never accidentally attempt an
    outbound connection: selecting it is an explicit deployment choice.
    """

    __slots__ = ("_host", "_port", "_sender")

    def __init__(self, *, host: str, port: int, sender: str) -> None:
        self._host = host
        self._port = port
        self._sender = sender

    def deliver(self, *, artifact_path: Path, definition: ReportDefinition, run_id: int) -> Path:
        raise NotImplementedError(
            "SmtpDeliverer is the real-email seam and is not enabled in this build; "
            "the offline FileDeliverer is the default (task 18.4)."
        )


__all__ = ["ArtifactStore", "Deliverer", "FileDeliverer", "SmtpDeliverer"]
