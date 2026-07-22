"""Unit tests for toinflux.mcp_resources (read resources: the documentation
reference, and per-source schema and current-state resources)."""

from unittest.mock import MagicMock, patch

import anyio
from mcp.server.fastmcp import FastMCP

from toinflux.mcp_resources import register_resources


def _server():
    return FastMCP(name="test")


def _settings():
    return {
        "sources": ["zappi", "speedtest"],
        "influx": {"url": "http://x", "user": "u", "password": "p"},
        "zappi": {"db": "zappi_db"},
        "speedtest": {"db": "speedtest_db"},
    }


def _live_handler():
    handler = MagicMock()
    handler.source = "zappi"
    handler.MCP_LIVE_STATE = True
    handler.MCP_DESCRIPTION = "Zappi desc"
    handler.MCP_MEASUREMENT = "myenergi"
    handler.MCP_FIELD_METADATA = {"sta": {"codes": {3: "charging"}}}
    handler.get_data.return_value = {"sta": 3}
    handler.session = MagicMock()
    return handler


class TestRegisterResources:
    def test_registers_docs_and_per_source_resources(self):
        server = _server()
        register_resources(server, _settings(), None)
        uris = {str(r.uri) for r in anyio.run(server.list_resources)}
        assert {
            "docs://reference",
            "schema://zappi",
            "state://zappi",
            "schema://speedtest",
            "state://speedtest",
        } <= uris

    def test_state_resource_reads_current_state(self):
        server = _server()
        register_resources(server, _settings(), None)
        with patch("toinflux.mcp_common.get_class", return_value=_live_handler()):
            contents = anyio.run(server.read_resource, "state://zappi")
        text = contents[0].content
        assert "charging" in text and "live" in text

    def test_documentation_resource_reads_markdown(self):
        server = _server()
        register_resources(server, _settings(), None)
        with patch("toinflux.mcp_common.get_class", return_value=_live_handler()):
            contents = anyio.run(server.read_resource, "docs://reference")
        assert "data reference" in contents[0].content
