"""Unit tests for toinflux.mcp_common (the shared MCP handler-lifecycle plumbing:
source resolution, handler construction, and best-effort session close)."""

import json
from unittest.mock import MagicMock, patch

import anyio
import pytest

from toinflux.exceptions import ConfigError, SourceConnectionError, ToInfluxError, ToolParamError
from toinflux.influx import InfluxWriteError
from toinflux.mcp_common import close_session, configured_sources, register_tool, resolve_handler


class TestConfiguredSources:
    def test_uses_sources_list(self):
        assert configured_sources({"sources": ["Hue", "Zappi"]}) == ["hue", "zappi"]

    def test_empty_or_absent_sources_returns_empty(self):
        # No fallback since default_source was removed - "nothing configured" means
        # the MCP tools expose nothing, matching what the collectors run.
        assert configured_sources({"sources": []}) == []
        assert configured_sources({}) == []

    def test_leftover_default_source_key_is_inert(self):
        # default_source was removed with no deprecation window - a leftover key from
        # before this change must be silently ignored, not fall back to it.
        assert configured_sources({"default_source": "octopus"}) == []

    def test_non_string_entries_filtered_from_list(self):
        assert configured_sources({"sources": ["Hue", 5, None, "zappi"]}) == ["hue", "zappi"]


class TestResolveHandler:
    @pytest.mark.parametrize("bad", [None, "", "   ", 5, ["hue"]])
    def test_non_string_or_empty_source_rejected(self, bad):
        # A clean tool error, not an AttributeError from .lower() on a non-string.
        with pytest.raises(ToolParamError, match="non-empty string"):
            resolve_handler(bad, {"sources": ["hue"]}, None)

    def test_unknown_source_rejected(self):
        with pytest.raises(ToolParamError, match="unknown source"):
            resolve_handler("nosuch", {"sources": ["hue"]}, None)

    def test_case_insensitive_match_passes_original_name_to_factory(self):
        # The configured-source check is case-insensitive, but the original name is
        # handed to get_class (itself case-insensitive), not a lowercased one.
        with patch("toinflux.mcp_common.get_class", return_value="HANDLER") as gc:
            assert resolve_handler("Hue", {"sources": ["hue"]}, "cfg.yaml") == "HANDLER"
        gc.assert_called_once_with("Hue", "cfg.yaml", instance=None)

    def test_unknown_instance_is_refused_immediately_and_closes_the_session(self):
        """An unconfigured bridge must be refused here, not deep inside schema building.

        Construction does not touch the instance, so without this the bogus value is
        accepted and only surfaces later as a raw ConfigError - bypassing the
        ToolParamError wrapping that tells a caller mistake from a transport failure, and
        leaking the handler's session because the caller never receives the handler.
        """
        from unittest.mock import MagicMock

        settings = {
            "sources": ["hue"],
            "influx": {"url": "http://x", "user": "u", "password": "p"},
            "hue": {"db": "hue_db", "interval": 300, "host": "real.example.com", "user": "tok"},
        }
        handler = MagicMock()
        handler.mcp_tag_filters.side_effect = ConfigError("no Hue bridge configured at 'bogus'")
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            with pytest.raises(ToolParamError, match="not usable"):
                resolve_handler("hue", settings, None, instance="bogus")
        handler.session.close.assert_called_once()

    def test_no_instance_skips_the_resolution_probe(self):
        """A single-target source has nothing to resolve, so it must not be probed."""
        from unittest.mock import MagicMock

        handler = MagicMock()
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            assert resolve_handler("hue", {"sources": ["hue"]}, None) is handler
        handler.mcp_tag_filters.assert_not_called()

    def test_instance_is_refused_for_a_single_target_source(self):
        """Accepting it would silently return an unscoped read the caller thinks is narrowed.

        A single-target source never consults its instance - ``mcp_tag_filters()`` does not
        look at it - so the resolution probe below cannot catch this: there is nothing to
        resolve and nothing raises. The value was therefore accepted, the read ran across
        everything, and the answer came back looking scoped.

        Guarded here rather than in each tool because this is the one function every read
        and write tool constructs through, so a future tool that grows an instance-shaped
        parameter inherits the refusal instead of having to remember it.
        """
        with patch("toinflux.mcp_common.get_class") as gc:
            with pytest.raises(ToolParamError, match="single target"):
                resolve_handler("octopus", {"sources": ["octopus"]}, None, instance="bogus")
        # Refused before construction, so there is no session to leak.
        gc.assert_not_called()

    def test_instance_is_still_accepted_for_an_instanced_source(self):
        """The guard must not block the case it exists to protect."""
        handler = MagicMock()
        handler.mcp_tag_filters.return_value = {"host": "a.example.com"}
        with patch("toinflux.mcp_common.get_class", return_value=handler):
            assert resolve_handler("hue", {"sources": ["hue"]}, None, instance="a.example.com") is handler

    def test_unusable_source_wrapped_as_tool_param_error(self):
        # A ConfigError from the factory becomes a (non-retryable) ToolParamError.
        with patch("toinflux.mcp_common.get_class", side_effect=ConfigError("boom")):
            with pytest.raises(ToolParamError, match="not usable: boom"):
                resolve_handler("hue", {"sources": ["hue"]}, None)


class TestCloseSession:
    def test_closes_the_session(self):
        session = MagicMock()
        close_session(session)
        session.close.assert_called_once_with()

    def test_swallows_close_error(self):
        session = MagicMock()
        session.close.side_effect = RuntimeError("nope")
        close_session(session)  # must not raise
        session.close.assert_called_once_with()


class TestRegisterToolErrorTranslation:
    """What a tool's failure message says to the model, which the SDK decides by type.

    mcp 2.1.0 stopped putting a non-``ToolError`` exception's text on the wire, so a
    ``ToolParamError`` reading "unknown field 'evil' for source 'zappi'; choose one of:
    ..." arrived as ``Error executing tool query_history`` and nothing else - the model
    told only that it failed, and never what to send instead. It was logged as a server
    crash with a traceback too. ``register_tool()`` translates the two anticipated
    failures into the SDK's ``ToolError`` to restore both; these tests are what catches
    the next SDK release moving that line again, since nothing else asserts on the type.
    """

    @staticmethod
    def _server():
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name="test")

    def test_tool_param_error_keeps_its_message(self):
        server = self._server()

        @register_tool(server, title="T")
        async def boom(field: str) -> dict:
            """Doc."""
            raise ToolParamError(f"unknown field {field!r}; choose one of: gen, imp")

        with pytest.raises(Exception) as excinfo:
            anyio.run(server.call_tool, "boom", {"field": "evil"})
        assert "unknown field 'evil'; choose one of: gen, imp" in str(excinfo.value)

    def test_source_connection_error_keeps_its_message(self):
        server = self._server()

        @register_tool(server, title="T")
        async def boom() -> dict:
            """Doc."""
            raise SourceConnectionError("InfluxDB read failed (field history): timed out")

        with pytest.raises(Exception) as excinfo:
            anyio.run(server.call_tool, "boom", {})
        assert "InfluxDB read failed (field history): timed out" in str(excinfo.value)

    def test_an_anticipated_failure_is_not_classed_as_a_crash(self):
        # The type is the whole mechanism: the SDK logs an UnexpectedToolError at ERROR
        # with a traceback and withholds its text, and a plain ToolError at INFO with the
        # message kept. Asserting only on the message would still pass if a later SDK
        # started reporting crashes verbosely, and the log level would silently be wrong.
        from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

        server = self._server()

        @register_tool(server, title="T")
        async def boom() -> dict:
            """Doc."""
            raise ToolParamError("bad field")

        with pytest.raises(ToolError) as excinfo:
            anyio.run(server.call_tool, "boom", {})
        assert not isinstance(excinfo.value, UnexpectedToolError)

    def test_an_unanticipated_failure_stays_a_crash_with_its_text_withheld(self):
        # The other half of the contract, and the reason the translation is a listed pair
        # rather than a bare `except Exception`: a bug in this server must not be dressed
        # up as a deliberate tool error, because then it is neither logged with its
        # traceback nor kept off the wire.
        from mcp.server.mcpserver.exceptions import UnexpectedToolError

        server = self._server()

        @register_tool(server, title="T")
        async def boom() -> dict:
            """Doc."""
            raise AttributeError("'NoneType' object has no attribute 'lower'")

        with pytest.raises(UnexpectedToolError) as excinfo:
            anyio.run(server.call_tool, "boom", {})
        assert "NoneType" not in str(excinfo.value)

    def test_a_sync_tool_is_not_turned_into_a_coroutine_function(self):
        """The wrapper must match the tool's own sync/async-ness.

        The SDK asks ``is_async_callable(fn)`` and awaits accordingly, so an async
        wrapper around a sync tool would be awaited and its body would run on the event
        loop - exactly the stall the read tools use ``anyio.to_thread.run_sync`` to
        avoid, and silent, since the call would still return the right answer.
        """
        import inspect

        server = self._server()

        @register_tool(server, title="T")
        def sync_tool() -> dict:
            """Doc."""
            return {"ok": True}

        assert not inspect.iscoroutinefunction(sync_tool)
        # And it still runs: the SDK calls a sync tool without awaiting it.
        result = anyio.run(server.call_tool, "sync_tool", {})
        assert json.loads(result.content[0].text) == {"ok": True}

    def test_the_advertised_schema_still_comes_from_the_tool_signature(self):
        # functools.wraps is what keeps this true (inspect.signature follows __wrapped__,
        # and the SDK builds the input schema from it) - a hand-rolled wrapper would
        # advertise (*args, **kwargs) and every tool would take no arguments.
        server = self._server()

        @register_tool(server, title="T")
        async def documented(source: str, detail: bool = False) -> dict:
            """First line.

            Indented continuation."""
            return {}

        tool = anyio.run(server.list_tools)[0]
        assert sorted(tool.input_schema["properties"]) == ["detail", "source"]
        assert tool.input_schema["required"] == ["source"]
        # And the dedent register_tool exists for still happens.
        assert tool.description == "First line.\n\nIndented continuation."


class TestEveryDeliberateFailureIsTranslated:
    """The classes the translation covers, and why it covers a base rather than a list.

    `ANTICIPATED_FAILURES` used to name two types. `ConfigError` was not one of them, so an
    unconfigured device answered `Error executing tool list_fields` and nothing else, while the
    server logged it at ERROR as though the server were broken - the exact failure the translation
    was written to prevent, left live by the enumeration itself.

    Parameterised over the project's exception classes rather than over a hand-written list of the
    ones that happen to be reachable today: a new one is covered by inheriting, and if someone adds
    a project exception outside the hierarchy, `test_every_project_exception_is_a_toinflux_error`
    below fails rather than this quietly missing it.
    """

    @staticmethod
    def _server():
        from mcp.server.mcpserver import MCPServer

        return MCPServer(name="test")

    @pytest.mark.parametrize(
        "exception",
        [ConfigError, SourceConnectionError, ToolParamError, InfluxWriteError],
        ids=lambda cls: cls.__name__,
    )
    def test_its_message_reaches_the_caller(self, exception):
        from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

        server = self._server()

        @register_tool(server, title="T")
        async def boom() -> dict:
            """Doc."""
            raise exception("the operator can act on this")

        with pytest.raises(ToolError) as excinfo:
            anyio.run(server.call_tool, "boom", {})
        assert not isinstance(excinfo.value, UnexpectedToolError), f"{exception.__name__} is reported as a crash"
        assert "the operator can act on this" in str(excinfo.value)

    def test_every_project_exception_is_a_toinflux_error(self):
        """The hierarchy is what the translation relies on, so a stray sibling must fail here.

        `CredentialCliError` is deliberately excluded: it lives in the credential CLI, is raised
        before any tool is registered, and never reaches a client.
        """
        import inspect
        import pkgutil
        import importlib

        import toinflux

        outside = []
        for module in pkgutil.iter_modules(toinflux.__path__):
            if module.name == "credential_cli":
                continue
            loaded = importlib.import_module(f"toinflux.{module.name}")
            for name, obj in vars(loaded).items():
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, Exception)
                    and obj.__module__.startswith("toinflux.")
                    and not issubclass(obj, ToInfluxError)
                ):
                    outside.append(f"{obj.__module__}.{name}")
        assert not outside, (
            f"project exceptions outside the ToInfluxError hierarchy: {sorted(set(outside))} - they "
            f"will reach a client as a bare crash with their message withheld"
        )
