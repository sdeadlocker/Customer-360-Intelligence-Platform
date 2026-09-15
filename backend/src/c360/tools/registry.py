"""The tool registry, execution context and ``gen_ai.execute_tool`` instrumentation (tasks 6.2-6.4).

A :class:`ToolSpec` binds a name and description to a Pydantic argument model, a Pydantic result
model and a callable that takes ``(ToolContext, args) -> result``. The :class:`ToolRegistry` holds
the specs and is the single place a tool is executed, so every execution goes through one span and
one enforcement path.

The context (task 6.2, 6.5)
---------------------------

A :class:`ToolContext` carries the per-invocation :class:`Principal` and the process-shared
:class:`Services`. It exposes exactly the enforcement the REST routes apply, so a tool cannot skip
it by construction:

* :meth:`ToolContext.authorize` runs the same ``authorize_customer`` gate the routes run, raising
  :class:`EntitlementError` on a non-entitled or absent customer (the model side turns that into a
  refusal — requirement 11.6).
* :meth:`ToolContext.mask` applies the same field-masking serializer the routes apply, so a value a
  role cannot see over REST is equally absent from a tool result.
* graph reads pass ``principal.entitlement`` to the relationship service, which already redacts
  non-entitled nodes to structure-only (task 4.7) — the tool inherits that untouched.

The span (task 6.4)
-------------------

Every execution is a ``gen_ai.execute_tool {name}`` span carrying ``gen_ai.operation.name``,
``gen_ai.tool.name``, ``gen_ai.tool.type`` and ``c360.outcome``. Argument *values* never touch the
span (task 6.4, requirement 18.8) — only the tool name and the outcome, both non-identifying.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from c360.core.telemetry import SpanAttr, get_tracer
from c360.security.authorization import authorize_customer, is_customer_visible
from c360.security.serializer import mask_model

if TYPE_CHECKING:
    from c360.api.services import Services
    from c360.security.model import Principal

_TRACER_NAME = "c360.tools"

#: GenAI semantic-convention values for a tool-execution span (design §13.3).
_GEN_AI_OPERATION = "execute_tool"
_GEN_AI_TOOL_TYPE = "function"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-invocation enforcement context: the principal and the shared services.

    Constructed once per agent turn (or per Q&A request) and passed to every tool call in that turn.
    Holds no mutable state, so it is safe to reuse across the tool calls of a single turn.
    """

    principal: Principal
    services: Services

    def authorize(self, customer_id: str) -> None:
        """Run the same 403/404 gate the REST routes run before any customer read.

        Raises:
            EntitlementError: the customer does not exist or the principal is not entitled to it.
                Collapsed into one error precisely as the REST gate does, so a tool call is no more
                of an existence oracle than an HTTP request (requirement 15.5).
        """
        authorize_customer(self.principal, customer_id, self.services.customer.repository)

    def is_visible(self, customer_id: str) -> bool:
        """Non-raising visibility check, for building the graph redaction predicate (task 4.7)."""
        return is_customer_visible(self.principal, customer_id, self.services.customer.repository)

    def mask(self, model: BaseModel) -> tuple[dict[str, Any], list[str]]:
        """Apply the field policy to ``model``, returning ``(masked_dict, masked_paths)``.

        The identical call :func:`c360.api.masking.masked_envelope` makes for REST, minus the
        envelope: the tool layer keeps the masked dict and the masked-field list so a fact-wrapping
        tool can decide what it is even allowed to turn into a fact.
        """
        return mask_model(model, self.principal.field_policy)


@dataclass(frozen=True, slots=True)
class ToolSpec[ArgsT: BaseModel, ResultT: BaseModel]:
    """One registered tool: its identity, its typed argument/result models, and its implementation.

    ``args_model`` and ``result_model`` are Pydantic models so the schema export (task 6.3) is a
    pure function of the registration, and ``handler`` is the callable the registry invokes inside
    the execution span.
    """

    name: str
    description: str
    args_model: type[ArgsT]
    result_model: type[ResultT]
    handler: Callable[[ToolContext, ArgsT], ResultT]


class ToolRegistry:
    """Holds the registered tools and executes them inside a ``gen_ai.execute_tool`` span (6.2-6.4).

    The single execution path means no tool is ever run without the span and without validating its
    arguments through the declared Pydantic model — a tool call with malformed arguments fails
    validation before the handler runs, rather than inside it.
    """

    __slots__ = ("_tools",)

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec[Any, Any]] = {}

    def register(self, spec: ToolSpec[Any, Any]) -> None:
        """Register ``spec``. A duplicate name is a wiring error and raises."""
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec[Any, Any]:
        """The spec for ``name``, or a :class:`KeyError` naming the unknown tool."""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def names(self) -> tuple[str, ...]:
        """Every registered tool name, sorted, so a schema export is deterministically ordered."""
        return tuple(sorted(self._tools))

    def specs(self) -> tuple[ToolSpec[Any, Any], ...]:
        """Every registered spec, in name order."""
        return tuple(self._tools[name] for name in self.names())

    def execute(
        self,
        name: str,
        context: ToolContext,
        arguments: Mapping[str, Any] | BaseModel,
    ) -> BaseModel:
        """Validate ``arguments``, run the tool inside its span, and return the typed result.

        ``arguments`` may be a raw mapping (as it arrives from a model's tool call) or an
        already-built args model. Either way it is validated against the declared ``args_model``
        before the handler sees it. The span records the tool name and outcome only — never the
        arguments (task 6.4).
        """
        spec = self.get(name)
        args = self._coerce_args(spec, arguments)
        tracer = get_tracer(_TRACER_NAME)
        with tracer.start_as_current_span(f"{_GEN_AI_OPERATION} {name}") as span:
            span.set_attribute("gen_ai.operation.name", _GEN_AI_OPERATION)
            span.set_attribute("gen_ai.tool.name", name)
            span.set_attribute("gen_ai.tool.type", _GEN_AI_TOOL_TYPE)
            span.set_attribute(SpanAttr.TOOL, name)
            try:
                result: BaseModel = spec.handler(context, args)
            except Exception:
                span.set_attribute(SpanAttr.OUTCOME, "error")
                raise
            span.set_attribute(SpanAttr.OUTCOME, "ok")
            return result

    @staticmethod
    def _coerce_args(
        spec: ToolSpec[Any, Any], arguments: Mapping[str, Any] | BaseModel
    ) -> BaseModel:
        args_model: type[BaseModel] = spec.args_model
        if isinstance(arguments, args_model):
            return arguments
        if isinstance(arguments, BaseModel):
            # A different model was passed; round-trip through its dump so the declared model still
            # validates it rather than being bypassed.
            return args_model.model_validate(arguments.model_dump())
        return args_model.model_validate(dict(arguments))


def build_tool_registry() -> ToolRegistry:
    """Construct the registry with every customer tool registered (task 6.2).

    Imported lazily inside the function so :mod:`c360.tools.registry` has no import-time dependency
    on the concrete tool modules — the tools depend on this module for :class:`ToolSpec`, and a
    top-level import back would be a cycle.
    """
    from c360.tools.customer_tools import CUSTOMER_TOOL_SPECS  # noqa: PLC0415 - avoids import cycle
    from c360.tools.knowledge_tool import KNOWLEDGE_TOOL_SPEC  # noqa: PLC0415 - avoids import cycle

    registry = ToolRegistry()
    for spec in CUSTOMER_TOOL_SPECS:
        registry.register(spec)
    # knowledge_search joins the same registry as the customer tools (design §9.6), inheriting the
    # execution span, argument validation and the shared ToolContext principal.
    registry.register(KNOWLEDGE_TOOL_SPEC)
    return registry


__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "build_tool_registry",
]
