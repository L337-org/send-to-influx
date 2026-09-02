"""Unit tests for toinflux.mcp_resources (read resources: the documentation
reference, and per-source schema and current-state resources)."""

from unittest.mock import MagicMock, patch

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, UnexpectedResourceError

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.mcp_resources import register_resources


def _server():
    return MCPServer(name="test")


def _settings():
    return {
        "sources": ["zappi", "speedtest"],
        "influx": {"url": "http://x", "user": "u", "password": "p"},
        # A serial, because zappi is instanced: one worker per configured
        # device, so a block with no device expands to nothing and the source drops out.
        "zappi": {"db": "zappi_db", "serial": "12345"},
        "speedtest": {"db": "speedtest_db"},
    }


def _live_handler():
    handler = MagicMock()
    handler.source = "zappi"
    handler.MCP_LIVE_STATE = True
    handler.MCP_DESCRIPTION = "Zappi desc"
    handler.MCP_MEASUREMENT = "myenergi"
    handler.MCP_FIELD_METADATA = {"sta": {"codes": {3: "charging"}}}
    handler.mcp_field_metadata.return_value = handler.MCP_FIELD_METADATA
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


class TestResourceFailures:
    """What a failed resource read tells the client, which until now was nothing.

    Every test above this reads a resource that works. A read that fails has three
    outcomes worth telling apart - the source is unknown, InfluxDB is unreachable, or
    this server has a bug - and the SDK decides which the client sees purely from the
    exception's type: a `ResourceError` keeps its message and is logged at INFO, and
    anything else becomes `UnexpectedResourceError("Error reading resource <uri>")`,
    logged at ERROR with a traceback. `ToolParamError` is a `ValueError` and
    `SourceConnectionError` a plain `Exception`, so all three outcomes read identically
    to a client: "Error reading resource state://zappi" and nothing more.

    Not a regression, unlike the tool half: mcp 2.0.0 flattened even a deliberate
    `ResourceError` to that same generic message, so there was nothing a resource could
    have said. 2.1.0 is what made this fixable, and these are the tests that were
    missing when it was.
    """

    def test_transport_failure_says_what_failed(self):
        server = _server()
        register_resources(server, _settings(), None)
        with patch(
            "toinflux.mcp_resources.current_state_result",
            side_effect=SourceConnectionError("InfluxDB read failed (latest point): timed out"),
        ):
            with pytest.raises(ResourceError) as excinfo:
                anyio.run(server.read_resource, "state://zappi")
        assert "InfluxDB read failed (latest point): timed out" in str(excinfo.value)

    def test_caller_mistake_says_what_was_wrong(self):
        server = _server()
        register_resources(server, _settings(), None)
        with patch(
            "toinflux.mcp_resources.list_fields_result",
            side_effect=ToolParamError("source 'zappi' is not usable: no serial configured"),
        ):
            with pytest.raises(ResourceError) as excinfo:
                anyio.run(server.read_resource, "schema://zappi")
        assert "not usable: no serial configured" in str(excinfo.value)

    def test_an_anticipated_failure_is_not_classed_as_a_crash(self):
        # The type is the mechanism, so assert it rather than only the message: an
        # UnexpectedResourceError is logged at ERROR with a traceback, and a plain
        # ResourceError at INFO. Getting this wrong fills the journal with crashes
        # every time InfluxDB blinks, and nothing in the payload would show it.
        server = _server()
        register_resources(server, _settings(), None)
        with patch("toinflux.mcp_resources.current_state_result", side_effect=SourceConnectionError("down")):
            with pytest.raises(ResourceError) as excinfo:
                anyio.run(server.read_resource, "state://zappi")
        assert not isinstance(excinfo.value, UnexpectedResourceError)

    def test_a_bug_stays_a_crash_with_its_text_withheld(self):
        # The other half of the contract. A bug in this server must not be dressed up
        # as a deliberate resource error, or it is neither logged with its traceback
        # nor kept off the wire - which is what the SDK's rule exists to guarantee.
        server = _server()
        register_resources(server, _settings(), None)
        with patch(
            "toinflux.mcp_resources.current_state_result",
            side_effect=AttributeError("'NoneType' object has no attribute 'lower'"),
        ):
            with pytest.raises(UnexpectedResourceError) as excinfo:
                anyio.run(server.read_resource, "state://zappi")
        assert "NoneType" not in str(excinfo.value)

    def test_the_documentation_resource_is_covered_too(self):
        # docs:// is registered in register_resources() itself rather than in
        # _register_source_resources(), so it is the one that can be missed.
        server = _server()
        register_resources(server, _settings(), None)
        with patch("toinflux.mcp_resources.build_documentation", side_effect=ToolParamError("no sources configured")):
            with pytest.raises(ResourceError) as excinfo:
                anyio.run(server.read_resource, "docs://reference")
        assert "no sources configured" in str(excinfo.value)

    def test_nothing_registers_a_resource_around_the_registrar(self):
        """The source guard, mirroring the one on the tool registrar.

        A resource registered with a bare `@server.resource(` still works and still
        passes every behaviour test above - it just goes back to saying nothing about
        why it failed, which no payload assertion can see. Checking the source is the
        only guard that fails on the machine where the mistake is made.
        """
        import pathlib

        text = (pathlib.Path(__file__).resolve().parent.parent / "toinflux/mcp_resources.py").read_text(
            encoding="utf-8"
        )
        assert "@server.resource(" not in text, (
            "toinflux/mcp_resources.py registers a resource directly with @server.resource - use "
            "@_register_resource(server, ...) so an anticipated failure keeps its message"
        )
