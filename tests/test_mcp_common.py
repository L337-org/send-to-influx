"""Unit tests for toinflux.mcp_common (the shared MCP handler-lifecycle plumbing:
source resolution, handler construction, and best-effort session close)."""

from unittest.mock import MagicMock, patch

import pytest

from toinflux.exceptions import ConfigError, ToolParamError
from toinflux.mcp_common import close_session, configured_sources, resolve_handler


class TestConfiguredSources:
    def test_uses_sources_list(self):
        assert configured_sources({"sources": ["Hue", "Zappi"]}) == ["hue", "zappi"]

    def test_falls_back_to_default_source(self):
        assert configured_sources({"default_source": "octopus"}) == ["octopus"]

    def test_default_source_is_lowercased(self):
        # A capitalised default_source must normalise, or resolve_handler's
        # source.lower() comparison would never match it.
        assert configured_sources({"default_source": "Octopus"}) == ["octopus"]

    def test_non_string_default_source_dropped(self):
        # YAML coerces `default_source: no` to False; must not end up in the list
        # (it would crash the sorted()/join in resolve_handler's error message).
        assert configured_sources({"sources": [], "default_source": False}) == []

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
        gc.assert_called_once_with("Hue", "cfg.yaml")

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
