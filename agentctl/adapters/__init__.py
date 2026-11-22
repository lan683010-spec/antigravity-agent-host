"""Adapter registry for agentctl."""

from __future__ import annotations

from agentctl.adapters.base import BaseAgentAdapter
from agentctl.adapters.claude import ClaudeAdapter
from agentctl.adapters.codex import CodexAdapter
from agentctl.adapters.cursor import CursorAdapter
from agentctl.adapters.custom import load_custom_adapters
from agentctl.adapters.gemini import GeminiAdapter
from agentctl.adapters.mock import MockAdapter

_BUILTIN_REGISTRY: dict[str, type[BaseAgentAdapter]] = {
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "claude": ClaudeAdapter,
    "gemini": GeminiAdapter,
    "mock": MockAdapter,
}


def get_adapter(name: str) -> BaseAgentAdapter:
    key = name.strip().lower()
    cls = _BUILTIN_REGISTRY.get(key)
    if cls is not None:
        return cls()
    custom = load_custom_adapters()
    if key in custom:
        return custom[key]
    supported = sorted(set(_BUILTIN_REGISTRY.keys()) | set(custom.keys()))
    raise ValueError(
        f"Unknown agent '{name}'. Supported agents: {', '.join(supported)}"
    )


def list_adapters(*, include_mock: bool = False) -> list[BaseAgentAdapter]:
    adapters: list[BaseAgentAdapter] = [
        cls()
        for key, cls in _BUILTIN_REGISTRY.items()
        if include_mock or key != "mock"
    ]
    custom = load_custom_adapters()
    for key, adapter in custom.items():
        if key not in _BUILTIN_REGISTRY:
            adapters.append(adapter)
    return adapters
