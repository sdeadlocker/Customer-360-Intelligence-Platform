"""Phase 6 — the typed tool registry (design §8.2, tasks 6.1-6.5).

Every agent and the Q&A ReAct loop reach customer data through the tools in this package rather than
through the services directly, so a single layer carries three guarantees the model side depends on:

* **Fact provenance (task 6.1).** Every value a tool returns is wrapped as a :class:`Fact` carrying
  ``entity_type``, ``entity_id``, ``field`` and ``as_of``, collected into a :class:`FactTable` with
  stable numbered ids. That is what a downstream claim validator (Phase 8) resolves a numeric claim
  against, and what a citation in a narrative points at.

* **The same enforcement as REST (tasks 6.2, 6.5).** Each tool calls the *same* application service
  the REST API calls, runs the *same* authorization gate, applies the *same* field-masking
  serializer, and inherits the *same* graph redaction. A rule implemented once in the service or the
  security layer is therefore enforced identically whether reached over HTTP or a tool call.

* **Argument-free tracing (task 6.4).** Every execution is a ``gen_ai.execute_tool`` span carrying
  the tool name, duration and outcome, and never the argument values.
"""

from c360.tools.facts import Fact, FactRef, FactTable
from c360.tools.registry import ToolContext, ToolRegistry, ToolSpec, build_tool_registry
from c360.tools.schemas import tool_schema, tool_schemas

__all__ = [
    "Fact",
    "FactRef",
    "FactTable",
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "build_tool_registry",
    "tool_schema",
    "tool_schemas",
]
