"""The versioned prompt registry (task 8.3, design §14.7).

A prompt is a *file*, not an inline string, and it carries its own metadata: the agent it binds to,
its declared version, its field allowlist and its knowledge domains. Two things depend on this being
real rather than decorative:

* ``prompt_version`` flows onto every :class:`~c360.agents.result.AgentResult` and into the agent
  cache key (design §8.7), so a prompt edit invalidates stale narratives and an evaluation can
  attribute a quality change to a specific prompt. That is only meaningful if the version is bound
  to the *content* — hence the content hash, which the loader computes and folds into the effective
  version so an operator who edits a file body but forgets to bump the header still gets a new key.
* the field allowlist and knowledge domains a prompt declares are the *source of truth* the
  :class:`~c360.agents.redaction.PromptRedactor` (task 8.4) and the retrieval step read, so what a
  model is allowed to see is a property of the reviewed, versioned prompt file — not something an
  agent hardcodes and can drift from.

File format
-----------

Each prompt is a UTF-8 ``.md`` file under ``prompts/`` with a delimited front-matter header and a
body::

    ---
    id: financial_health
    version: v1
    owner: platform
    agent: financial_health
    field_allowlist: segment, value_tier, net_worth_cents, total_deposits_cents
    knowledge_domains:
    ---
    You are the financial-health agent. ...

The header is parsed by a tiny purpose-built reader rather than a YAML dependency: values are simple
scalars and comma lists, and keeping the parser in-tree means the prompt format has no third-party
surface. An empty list value (``knowledge_domains:`` with nothing after it) means "no retrieval".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from c360.core.config import Settings


_FRONT_MATTER_DELIMITER: Final = "---"
#: How many hex chars of the content hash are appended to the declared version. Long enough that an
#: accidental collision across the handful of prompt files is not a practical concern.
_HASH_PREFIX_LEN: Final = 12


class PromptError(RuntimeError):
    """Raised when a prompt file is missing, malformed, or bound to the wrong agent."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One loaded prompt: its metadata, its body, and its content-bound effective version.

    ``version`` is ``<declared>+<hash12>`` — the version the file declares, suffixed with the first
    twelve hex chars of the SHA-256 over the whole file. This is what goes on an ``AgentResult`` and
    the cache key, so two files that declare ``v1`` but differ in a single character of the body are
    distinguishable, and re-saving an unchanged file yields the identical version.
    """

    id: str
    agent: str
    owner: str
    declared_version: str
    version: str
    field_allowlist: frozenset[str]
    knowledge_domains: tuple[str, ...]
    body: str
    content_hash: str


class PromptRegistry:
    """Loads and holds the prompt files, one per agent, keyed by agent name (task 8.3).

    Built from a directory at startup by :func:`build_prompt_registry`. Lookups are by agent name
    because the graph wires a node to an agent, and the node asks the registry for *its* prompt; a
    missing prompt for a wired agent is a deployment error and raises rather than silently running
    unversioned.
    """

    __slots__ = ("_by_agent",)

    def __init__(self, prompts: Iterable[Prompt]) -> None:
        by_agent: dict[str, Prompt] = {}
        for prompt in prompts:
            if prompt.agent in by_agent:
                raise PromptError(f"two prompt files bind to agent {prompt.agent!r}")
            by_agent[prompt.agent] = prompt
        self._by_agent = by_agent

    def for_agent(self, agent: str) -> Prompt:
        """The prompt bound to ``agent``; raises :class:`PromptError` if none is registered."""
        try:
            return self._by_agent[agent]
        except KeyError:
            raise PromptError(f"no prompt registered for agent {agent!r}") from None

    def agents(self) -> tuple[str, ...]:
        """Every agent that has a registered prompt, sorted."""
        return tuple(sorted(self._by_agent))

    def get(self, agent: str) -> Prompt | None:
        """The prompt for ``agent`` or ``None`` — the non-raising lookup, for optional paths."""
        return self._by_agent.get(agent)


def parse_prompt(text: str, *, source: str) -> Prompt:
    """Parse one prompt file's text into a :class:`Prompt` (task 8.3).

    ``source`` names the file in error messages only. The content hash is computed over the *whole*
    text, header included, so any change — body or metadata — yields a new effective version.
    """
    header, body = _split_front_matter(text, source=source)
    fields = _parse_header(header, source=source)

    for required in ("id", "agent", "version"):
        if required not in fields:
            raise PromptError(f"{source}: front matter missing required key {required!r}")

    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    declared = fields["version"]
    return Prompt(
        id=fields["id"],
        agent=fields["agent"],
        owner=fields.get("owner", "unknown"),
        declared_version=declared,
        version=f"{declared}+{content_hash[:_HASH_PREFIX_LEN]}",
        field_allowlist=frozenset(_parse_list(fields.get("field_allowlist", ""))),
        knowledge_domains=tuple(_parse_list(fields.get("knowledge_domains", ""))),
        body=body.strip(),
        content_hash=content_hash,
    )


def build_prompt_registry(settings: Settings) -> PromptRegistry:
    """Load every ``*.md`` prompt under the configured registry directory (task 8.3).

    Raises:
        PromptError: the directory does not exist, or a file in it is malformed. A missing registry
            is a deployment error, not a degraded state — an agent must never run unversioned.
    """
    directory = settings.prompt_registry_dir
    if not directory.is_dir():
        raise PromptError(f"prompt registry directory not found: {directory}")
    # `README.md` is documentation, not a prompt, and is skipped by name. Every other `*.md` file is
    # a prompt and *must* parse — a malformed one raises rather than being silently skipped, so a
    # broken prompt can never quietly drop an agent to running unversioned.
    prompts = [
        parse_prompt(path.read_text(encoding="utf-8"), source=path.name)
        for path in sorted(directory.glob("*.md"))
        if path.name.lower() != "readme.md"
    ]
    if not prompts:
        raise PromptError(f"no prompt files found under {directory}")
    return PromptRegistry(prompts)


# ---------------------------------------------------------------- header parsing


def _split_front_matter(text: str, *, source: str) -> tuple[str, str]:
    """Return ``(header, body)`` from a ``---``-delimited file, or raise if the fence is missing."""
    stripped = text.lstrip("\ufeff")  # tolerate a UTF-8 BOM
    if not stripped.startswith(_FRONT_MATTER_DELIMITER):
        raise PromptError(f"{source}: file must begin with a '---' front-matter fence")
    rest = stripped[len(_FRONT_MATTER_DELIMITER) :]
    end = rest.find(f"\n{_FRONT_MATTER_DELIMITER}")
    if end == -1:
        raise PromptError(f"{source}: front-matter fence is not closed with '---'")
    header = rest[:end]
    body = rest[end + len(_FRONT_MATTER_DELIMITER) + 1 :]
    return header, body


def _parse_header(header: str, *, source: str) -> Mapping[str, str]:
    """Parse ``key: value`` lines into a mapping. Blank lines and comments (``#``) are ignored."""
    fields: dict[str, str] = {}
    for raw in header.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise PromptError(f"{source}: malformed header line (no ':'): {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _parse_list(value: str) -> list[str]:
    """Split a comma-separated header value into a clean list; an empty value is an empty list."""
    return [item.strip() for item in value.split(",") if item.strip()]


__all__ = [
    "Prompt",
    "PromptError",
    "PromptRegistry",
    "build_prompt_registry",
    "parse_prompt",
]
