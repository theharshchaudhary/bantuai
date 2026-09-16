"""Tool registration, JSON Schema generation, and permission gating.

Schemas are derived from type hints and the docstring, so a tool's signature is
its contract — the schema and the code cannot drift apart.

Permission tiers are enforced *here*, before the function is called. Not in the
system prompt: a prompt is a suggestion, a code path is not.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import typing
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, get_args, get_origin

from ..providers.base import ToolCall, ToolSpec

log = logging.getLogger("bantu.tools")


class Tier(str, Enum):
    AUTO = "auto"          # read-only or trivially reversible; runs silently
    CONFIRM = "confirm"    # changes state; needs explicit approval first
    BLOCKED = "blocked"    # never runs, whatever the model or user asks


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to."""


class Rejected(Exception):
    """The user declined a confirmation."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    tier: Tier
    category: str = "general"

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


# --- schema generation ------------------------------------------------------

_SIMPLE: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _docstring_parts(fn: Callable) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into a summary and per-argument help."""
    doc = inspect.getdoc(fn) or ""
    m = re.split(r"\n\s*(?:Args|Arguments|Params|Parameters):\s*\n", doc, maxsplit=1)
    summary = m[0].strip()
    args: dict[str, str] = {}
    if len(m) > 1:
        body = re.split(r"\n\s*(?:Returns|Raises|Example[s]?):\s*\n", m[1])[0]
        cur = None
        for line in body.split("\n"):
            hit = re.match(r"\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)", line)
            if hit:
                cur = hit.group(1)
                args[cur] = hit.group(2).strip()
            elif cur and line.strip():
                args[cur] += " " + line.strip()
    return summary, args


def _json_type(ann: Any, help_text: str = "", optional: bool = False) -> dict[str, Any]:
    """Map a type annotation onto a JSON Schema fragment.

    Optional parameters are typed as [X, "null"]. Models routinely pass null
    for an argument they mean to omit, and Groq rejects that server-side
    against a plain type. Verified: the type-list form is the only dialect
    both Groq and Gemini accept - "nullable": true fails on Groq.
    """
    if ann in _SIMPLE:
        out: dict[str, Any] = {"type": _SIMPLE[ann]}
    else:
        origin, args = get_origin(ann), get_args(ann)
        if origin is Literal:
            # All Literal members share a type in practice; infer from the first.
            out = {"type": _SIMPLE.get(type(args[0]), "string"), "enum": list(args)}
        elif origin in (list, set, tuple):
            item = args[0] if args else str
            out = {"type": "array", "items": _json_type(item)}
        elif origin is dict:
            out = {"type": "object"}
        elif origin is typing.Union:
            # Optional[X] is Union[X, None]: describe X, nullability is handled
            # by leaving the field out of `required`.
            inner = [a for a in args if a is not type(None)]
            out = _json_type(inner[0]) if len(inner) == 1 else {"type": "string"}
        else:
            out = {"type": "string"}
    if optional and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    if help_text:
        out["description"] = help_text
    return out


def build_schema(fn: Callable) -> tuple[str, dict[str, Any]]:
    """Derive (description, JSON Schema) from a function's signature and docstring."""
    summary, arg_help = _docstring_parts(fn)
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=False)

    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        if pname in ("self", "cls") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        is_optional = p.default is not inspect.Parameter.empty
        props[pname] = _json_type(hints.get(pname, str), arg_help.get(pname, ""), is_optional)
        if not is_optional:
            required.append(pname)

    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return summary, schema


# --- registry ---------------------------------------------------------------

#: Signature of the confirmation callback: (tool, arguments) -> allowed?
ConfirmFn = Callable[[Tool, dict[str, Any]], bool]


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    #: Approvals granted for the current task only; cleared by `new_task()`.
    _session_allow: set[str] = field(default_factory=set)

    def register(
        self,
        fn: Callable | None = None,
        *,
        tier: Tier = Tier.CONFIRM,
        name: str | None = None,
        category: str = "general",
    ):
        """Register a tool. Usable bare or with arguments.

        The tier defaults to CONFIRM deliberately: a tool author who forgets to
        think about safety gets the cautious behaviour, not the dangerous one.
        """

        def wrap(f: Callable) -> Callable:
            desc, schema = build_schema(f)
            if not desc:
                raise ValueError(f"tool {f.__name__} needs a docstring - the model reads it")
            t = Tool(name or f.__name__, desc, schema, f, tier, category)
            if t.name in self.tools:
                raise ValueError(f"duplicate tool name: {t.name}")
            self.tools[t.name] = t
            return f

        return wrap(fn) if fn else wrap

    # --- lookup -------------------------------------------------------------

    def specs(self, include_blocked: bool = False) -> list[ToolSpec]:
        """What the model is told it can do. Blocked tools are not advertised."""
        return [
            t.spec()
            for t in self.tools.values()
            if include_blocked or t.tier is not Tier.BLOCKED
        ]

    def __len__(self) -> int:
        return len(self.tools)

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def new_task(self) -> None:
        """Drop per-task approvals. Call at the start of each user request."""
        self._session_allow.clear()

    def allow_for_task(self, name: str) -> None:
        self._session_allow.add(name)

    # --- execution ----------------------------------------------------------

    def execute(
        self,
        call: ToolCall,
        confirm: ConfirmFn | None = None,
    ) -> str:
        """Run a tool call and return a string the model can read.

        Failures come back as text rather than raising: the model should get the
        chance to recover, and a crashed assistant is worse than a corrected one.
        """
        tool = self.tools.get(call.name)
        if tool is None:
            return f"Error: no tool named {call.name!r}. Available: {', '.join(sorted(self.tools))}"

        if tool.tier is Tier.BLOCKED:
            log.warning("blocked tool refused: %s", tool.name)
            return f"Error: {tool.name} is blocked and cannot be run."

        if tool.tier is Tier.CONFIRM and tool.name not in self._session_allow:
            if confirm is None:
                return (
                    f"Error: {tool.name} needs confirmation but nothing can ask the user "
                    f"right now, so it was not run."
                )
            try:
                if not confirm(tool, call.arguments):
                    return f"The user declined to run {tool.name}. Do not retry it; try another way."
            except Rejected:
                return f"The user declined to run {tool.name}. Do not retry it; try another way."

        try:
            bound = self._coerce(tool, call.arguments)
        except (TypeError, ValueError) as e:
            return f"Error: bad arguments for {tool.name}: {e}"

        try:
            result = tool.fn(**bound)
        except ToolError as e:
            return f"Error: {e}"
        except Exception as e:  # a bug in one tool must not kill the agent
            log.exception("tool %s raised", tool.name)
            return f"Error: {tool.name} failed: {type(e).__name__}: {e}"

        return self._stringify(result)

    @staticmethod
    def _coerce(tool: Tool, args: dict[str, Any]) -> dict[str, Any]:
        """Keep only known parameters, and fix the usual model type slips."""
        sig = inspect.signature(tool.fn)
        hints = typing.get_type_hints(tool.fn, include_extras=False)
        out: dict[str, Any] = {}
        for k, v in (args or {}).items():
            if k not in sig.parameters:
                continue  # models occasionally invent arguments; drop them quietly
            if v is None:
                if sig.parameters[k].default is not inspect.Parameter.empty:
                    continue  # an explicit null means "omitted"; let the default apply
                raise TypeError(f"{k} is required but was given null")
            want = hints.get(k)
            if want in (int, float) and isinstance(v, str):
                try:
                    v = want(v)
                except ValueError:
                    pass
            elif want is bool and isinstance(v, str):
                v = v.strip().lower() in ("true", "1", "yes")
            out[k] = v
        missing = [
            n
            for n, p in sig.parameters.items()
            if p.default is inspect.Parameter.empty
            and n not in out
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        if missing:
            raise TypeError(f"missing required argument(s): {', '.join(missing)}")
        return out

    @staticmethod
    def _stringify(result: Any, limit: int = 6000) -> str:
        """Render a tool result for the model, truncated to protect the budget."""
        if result is None:
            return "Done."
        if isinstance(result, str):
            text = result
        elif isinstance(result, (dict, list)):
            try:
                text = json.dumps(result, indent=2, default=str)
            except (TypeError, ValueError):
                text = str(result)
        else:
            text = str(result)
        if len(text) > limit:
            text = text[:limit] + f"\n... [truncated, {len(text)} chars total]"
        return text
