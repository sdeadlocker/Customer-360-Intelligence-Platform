"""Tool-schema export for Bedrock tool-calling (task 6.3, requirement 11.1).

A model that calls tools needs a machine-readable description of each tool: its name, what it does,
and the JSON schema of its arguments. This module derives that from the same Pydantic argument
models the tools already declare, so the schema a model sees and the validation a call is subjected
to are one source of truth — a drift between "what the model was told it could pass" and "what the
registry will accept" is impossible by construction.

Why not import LangChain here
-----------------------------

The generated shape is the OpenAI/Bedrock *function tool* object —
``{"type": "function", "function": {"name", "description", "parameters"}}`` — which is exactly what
``langchain_aws.ChatBedrockConverse.bind_tools`` (and ``convert_to_openai_tool``) consume. Producing
it from ``model_json_schema()`` directly keeps the tool layer free of a LangChain import: LangChain
arrives in Phase 8, and binding the Phase 6 registry to it now would make the deterministic tool
layer depend on the AI stack it is meant to sit beneath. Phase 8 passes :func:`tool_schemas` (or the
per-tool :func:`tool_schema`) straight to ``bind_tools`` with no adapter.

The schema is inlined and cleaned so it survives the round trip through a provider that does not
follow ``$ref`` / ``$defs`` — nested arg models and enums are expanded in place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from c360.tools.registry import ToolRegistry, ToolSpec


def tool_schema(spec: ToolSpec[Any, Any]) -> dict[str, Any]:
    """The Bedrock/OpenAI function-tool object for one tool.

    ``parameters`` is the JSON schema of the tool's argument model, with ``$defs`` inlined so a
    consumer that does not resolve references still sees a complete schema.
    """
    parameters = _inline_defs(spec.args_model.model_json_schema())
    parameters.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": parameters,
        },
    }


def tool_schemas(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Every registered tool's schema, in the registry's deterministic name order.

    This is the list Phase 8 hands to ``bind_tools``; the ordering is stable so a prompt that embeds
    the tool list has a stable content hash for the prompt registry (task 8.3).
    """
    return [tool_schema(spec) for spec in registry.specs()]


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with every ``$ref`` into its ``$defs`` replaced by the definition inline.

    Pydantic factors nested models and enums into ``$defs`` and references them with ``$ref``.
    Several tool-calling providers accept only self-contained parameter schemas, so the references
    are expanded here and the now-empty ``$defs`` block removed. A ref that cannot be resolved is
    left untouched rather than raising — a schema is advisory to the model, and a malformed one is a
    smaller problem than a crash on export.
    """
    defs = schema.get("$defs", {})
    resolved = _resolve(schema, defs)
    if isinstance(resolved, dict):
        resolved.pop("$defs", None)
        return resolved
    return schema


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    """Recursively replace ``{"$ref": "#/$defs/Name"}`` nodes with a copy of the definition."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            target = defs.get(name)
            if target is not None:
                # Merge any sibling keywords (e.g. a `default`) over the inlined definition.
                merged = {**_resolve(target, defs)}
                for key, value in node.items():
                    if key != "$ref":
                        merged[key] = _resolve(value, defs)
                return merged
        return {key: _resolve(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(item, defs) for item in node]
    return node


__all__ = ["tool_schema", "tool_schemas"]
