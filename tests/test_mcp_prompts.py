"""Unit tests for toinflux.mcp_prompts (the MCP task prompts: registration gating
on write-enablement, and rendered content)."""

from unittest.mock import patch

import anyio
from mcp.server.fastmcp import FastMCP

from toinflux.mcp_prompts import register_prompts


def _server():
    return FastMCP(name="test")


def _prompt_names(server):
    return {p.name for p in anyio.run(server.list_prompts)}


def _render(server, name, args):
    result = anyio.run(server.get_prompt, name, args)
    return " ".join(m.content.text for m in result.messages)


def _register(server, writable=()):
    with patch("toinflux.mcp_prompts.writable_enabled_sources", return_value=list(writable)):
        register_prompts(server, {"sources": ["hue"]}, None)
    return server


class TestRegisterPrompts:
    def test_read_prompts_always_registered(self):
        names = _prompt_names(_register(_server()))
        assert "home_status" in names and "usage_trends" in names

    def test_control_prompt_absent_without_writes(self):
        assert "control_device" not in _prompt_names(_register(_server()))

    def test_control_prompt_present_when_writes_enabled(self):
        assert "control_device" in _prompt_names(_register(_server(), writable=["hue"]))


class TestPromptContent:
    def test_home_status_without_focus(self):
        text = _render(_register(_server()), "home_status", {})
        assert "get_current_state" in text
        assert "Focus on" not in text

    def test_home_status_with_focus_embeds_it(self):
        text = _render(_register(_server()), "home_status", {"focus": "the front door"})
        assert "Focus on: the front door" in text

    def test_usage_trends_embeds_question(self):
        text = _render(_register(_server()), "usage_trends", {"question": "electricity this month"})
        assert "electricity this month" in text
        assert "query_history" in text

    def test_control_device_flow(self):
        server = _register(_server(), writable=["hue"])
        text = _render(server, "control_device", {"request": "turn on the kitchen light"})
        assert "turn on the kitchen light" in text
        assert "list_writable_devices" in text and "set_device_state" in text
