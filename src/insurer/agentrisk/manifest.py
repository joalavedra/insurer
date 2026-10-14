from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

EFFECTS = {
    "read",
    "write",
    "money_out",
    "data_export",
    "external_comms",
    "approval",
}
AUTONOMY_LEVELS = {"autonomous", "human_in_loop"}
CONTROL_TYPES = {
    "approval_threshold",
    "amount_limit",
    "recipient_allowlist",
    "input_guardrail",
    "output_guardrail",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: str
    untrusted_input: bool = False
    amount_arg: str | None = None
    max_amount_eur: float | None = None
    recipient_arg: str | None = None


@dataclass(frozen=True)
class ControlSpec:
    id: str
    type: str
    tool: str | None = None
    approval_tool: str | None = None
    threshold_eur: float | None = None
    max_eur: float | None = None
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentManifest:
    agent_id: str
    framework: str
    owner: str
    model: str
    autonomy: str
    data_classes: tuple[str, ...]
    sessions_per_month: int
    tools: tuple[ToolSpec, ...]
    controls: tuple[ControlSpec, ...]

    @property
    def tools_by_name(self) -> dict[str, ToolSpec]:
        return {tool.name: tool for tool in self.tools}


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return value


def _required_string(mapping: dict[str, Any], key: str, name: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}.{key} must be a non-empty string")
    return value.strip()


def _optional_string(mapping: dict[str, Any], key: str, name: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}.{key} must be a non-empty string")
    return value.strip()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if (
        not math.isfinite(number)
        or (positive and number <= 0)
        or (not positive and number < 0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return number


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def parse_manifest(document: Any) -> AgentManifest:
    """Validate a customer-declared agent manifest."""
    raw = _mapping(document, "manifest")
    required = {
        "agent_id",
        "framework",
        "owner",
        "model",
        "autonomy",
        "data_classes",
        "volume",
        "tools",
        "controls",
    }
    missing = sorted(required - raw.keys())
    if missing:
        raise ValueError(f"manifest is missing required fields: {', '.join(missing)}")
    unknown = sorted(raw.keys() - required)
    if unknown:
        raise ValueError(f"manifest has unknown fields: {', '.join(unknown)}")

    agent_id = _required_string(raw, "agent_id", "manifest")
    framework = _required_string(raw, "framework", "manifest")
    owner = _required_string(raw, "owner", "manifest")
    model = _required_string(raw, "model", "manifest")
    autonomy = _required_string(raw, "autonomy", "manifest")
    if autonomy not in AUTONOMY_LEVELS:
        raise ValueError("manifest.autonomy must be 'autonomous' or 'human_in_loop'")

    data_classes = _string_list(raw["data_classes"], "manifest.data_classes")
    volume = _mapping(raw["volume"], "manifest.volume")
    if set(volume) != {"sessions_per_month"}:
        raise ValueError("manifest.volume must contain only sessions_per_month")
    sessions = volume["sessions_per_month"]
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions < 0:
        raise ValueError(
            "manifest.volume.sessions_per_month must be a non-negative integer"
        )

    if not isinstance(raw["tools"], list):
        raise ValueError("manifest.tools must be a list")
    tools: list[ToolSpec] = []
    names: set[str] = set()
    for index, item in enumerate(raw["tools"]):
        name = f"manifest.tools[{index}]"
        entry = _mapping(item, name)
        allowed = {
            "name",
            "effect",
            "untrusted_input",
            "amount_arg",
            "max_amount_eur",
            "recipient_arg",
        }
        extra = sorted(entry.keys() - allowed)
        if extra:
            raise ValueError(f"{name} has unknown fields: {', '.join(extra)}")
        tool_name = _required_string(entry, "name", name)
        if tool_name in names:
            raise ValueError(f"duplicate tool name: {tool_name}")
        names.add(tool_name)
        effect = _required_string(entry, "effect", name)
        if effect not in EFFECTS:
            raise ValueError(f"{name}.effect is not a supported effect")
        untrusted = entry.get("untrusted_input", False)
        if not isinstance(untrusted, bool):
            raise ValueError(f"{name}.untrusted_input must be a boolean")
        amount_arg = _optional_string(entry, "amount_arg", name)
        recipient_arg = _optional_string(entry, "recipient_arg", name)
        max_amount = entry.get("max_amount_eur")
        if max_amount is not None:
            max_amount = _number(max_amount, f"{name}.max_amount_eur", positive=True)
            if amount_arg is None:
                raise ValueError(f"{name}.max_amount_eur requires amount_arg")
        tools.append(
            ToolSpec(
                name=tool_name,
                effect=effect,
                untrusted_input=untrusted,
                amount_arg=amount_arg,
                max_amount_eur=max_amount,
                recipient_arg=recipient_arg,
            )
        )
    tool_map = {tool.name: tool for tool in tools}

    if not isinstance(raw["controls"], list):
        raise ValueError("manifest.controls must be a list")
    controls: list[ControlSpec] = []
    control_ids: set[str] = set()
    for index, item in enumerate(raw["controls"]):
        name = f"manifest.controls[{index}]"
        entry = _mapping(item, name)
        control_id = _required_string(entry, "id", name)
        if control_id in control_ids:
            raise ValueError(f"duplicate control id: {control_id}")
        control_ids.add(control_id)
        control_type = _required_string(entry, "type", name)
        if control_type not in CONTROL_TYPES:
            raise ValueError(f"{name}.type is not a supported control type")

        tool: str | None = None
        approval_tool: str | None = None
        threshold: float | None = None
        max_eur: float | None = None
        allowed_domains: tuple[str, ...] = ()
        if control_type == "approval_threshold":
            expected = {"id", "type", "tool", "approval_tool", "threshold_eur"}
            tool = _required_string(entry, "tool", name)
            approval_tool = _required_string(entry, "approval_tool", name)
            threshold = _number(entry.get("threshold_eur"), f"{name}.threshold_eur")
            if tool not in tool_map:
                raise ValueError(f"{name}.tool references unknown tool {tool!r}")
            if tool_map[tool].amount_arg is None:
                raise ValueError(f"{name}.tool {tool!r} has no amount_arg")
            if approval_tool not in tool_map:
                raise ValueError(
                    f"{name}.approval_tool references unknown tool {approval_tool!r}"
                )
            if tool_map[approval_tool].effect != "approval":
                raise ValueError(
                    f"{name}.approval_tool must reference a tool with effect 'approval'"
                )
        elif control_type == "amount_limit":
            expected = {"id", "type", "tool", "max_eur"}
            tool = _required_string(entry, "tool", name)
            max_eur = _number(entry.get("max_eur"), f"{name}.max_eur", positive=True)
            if tool not in tool_map:
                raise ValueError(f"{name}.tool references unknown tool {tool!r}")
            if tool_map[tool].amount_arg is None:
                raise ValueError(f"{name}.tool {tool!r} has no amount_arg")
        elif control_type == "recipient_allowlist":
            expected = {"id", "type", "tool", "allowed_domains"}
            tool = _required_string(entry, "tool", name)
            allowed_domains = tuple(
                domain.lower()
                for domain in _string_list(
                    entry.get("allowed_domains"), f"{name}.allowed_domains"
                )
            )
            if tool not in tool_map:
                raise ValueError(f"{name}.tool references unknown tool {tool!r}")
            if tool_map[tool].recipient_arg is None:
                raise ValueError(f"{name}.tool {tool!r} has no recipient_arg")
        else:
            expected = {"id", "type"}
        extra = sorted(entry.keys() - expected)
        missing_fields = sorted(expected - entry.keys())
        if missing_fields:
            raise ValueError(
                f"{name} is missing required fields: {', '.join(missing_fields)}"
            )
        if extra:
            raise ValueError(f"{name} has unknown fields: {', '.join(extra)}")
        controls.append(
            ControlSpec(
                id=control_id,
                type=control_type,
                tool=tool,
                approval_tool=approval_tool,
                threshold_eur=threshold,
                max_eur=max_eur,
                allowed_domains=allowed_domains,
            )
        )

    return AgentManifest(
        agent_id=agent_id,
        framework=framework,
        owner=owner,
        model=model,
        autonomy=autonomy,
        data_classes=data_classes,
        sessions_per_month=sessions,
        tools=tuple(tools),
        controls=tuple(controls),
    )


def load_manifest(path: str | Path) -> AgentManifest:
    source = Path(path)
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load manifest {source}: {exc}") from exc
    return parse_manifest(document)
