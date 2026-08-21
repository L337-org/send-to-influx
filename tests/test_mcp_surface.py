"""Guards on the MCP server's *advertised surface* - the titles and descriptions a
client loads on every session and a model reads to choose a tool.

The mechanical half of the AI-consumer standard was already enforced per module:
``tests/test_mcp_read.py`` and ``tests/test_mcp_write.py`` fail on a tool with no
title or no safety hint. This module covers the half that had no guard - the prose,
and the size of the surface as a whole - and it spans read tools, write tools,
prompts and resources, so it lives in its own module rather than in any one of
theirs.

What is checked, and why each is a guard rather than a preference:

* Every tool, prompt and resource carries a description, and a title distinct from
  its name. Both fields are optional in the MCP schema (verified against the
  2026-07-28 specification's Tool/Prompt/Resource data types), so nothing but a
  test stops a new registration shipping without them - which is exactly how the
  three resources shipped from 5.0 to 5.3 advertising a URI, a name and nothing
  else.
* Every tool description names the sibling tools a caller might otherwise reach
  for, from the ``SIBLINGS`` table below. The table must list every registered tool,
  so a *new* tool fails this module until someone has decided which of its
  neighbours it must be told apart from. That decision is the point; the assertion
  only makes it unskippable.
* No description names a tool that does not exist. A rename otherwise leaves a
  stale pointer that reads as authoritative and sends the model to a tool it cannot
  call.
* Every tool description says something about how it fails. A model recovers from a
  refusal it was warned about and cannot from one it was not, and the annotation
  fields carry none of this - the specification tells clients to treat annotations
  as untrusted hints, so prose is the only place it can live.
* The total size stays within a recorded budget. Every byte here is paid for on
  every session that loads the surface, so growth is a deliberate act with a reason
  in the commit message, not a drift nobody measured.

Measured surface (this module's fixture: two sources, both write-enabled, so all
nine tools, three prompts and five resources register):

===========  =============  ============
category     before (5.3)   after (5.4)
===========  =============  ============
tools                9,937        11,253
prompts                225           523
resources                0         1,521
**total**       **10,162**    **13,297**
===========  =============  ============

The growth is concentrated where the surface was previously silent rather than
merely terse: the resources advertised nothing at all; three tools stated no
failure behaviour; ``get_data_range`` and ``get_current_state`` documented neither
the per-producer ``instances`` grouping they return nor the partial-failure
reporting; and ``list_sources`` claimed to be "the only one needing no arguments",
which ``get_documentation`` disproves. ``query_history``, the largest single
description, *shrank* by 301 bytes - its behaviour kept, the justifications for
that behaviour dropped, since a caller needs to know what a tool does and not why
it was designed that way.
"""

from mcp.server.mcpserver import MCPServer

import anyio
import pytest

from toinflux.mcp_prompts import register_prompts
from toinflux.mcp_read import register_read_tools
from toinflux.mcp_resources import register_resources
from toinflux.mcp_write import register_write_tools

# Which siblings each tool must name, so a caller can tell why to pick it rather than
# them. Nearest neighbour, not every tool: naming all nine in all nine would pay for
# the whole surface nine times over and discriminate nothing.
SIBLINGS = {
    "list_sources": {"list_fields", "query_history", "get_current_state", "get_documentation"},
    "list_fields": {"list_sources", "query_history", "get_documentation"},
    "query_history": {"list_sources", "list_fields", "get_current_state", "get_data_range"},
    # The pair a caller most often confuses: now versus recorded history.
    "get_current_state": {"query_history", "get_data_range", "list_sources"},
    "get_data_range": {"query_history", "get_current_state", "list_fields", "list_sources"},
    "get_documentation": {"list_fields", "list_sources"},
    # The question that raised this work: nothing in this description ruled out the reading
    # that it lists devices for every collector, because it never named the tool that
    # actually does that.
    "hue_list_devices": {"hue_set_light", "get_current_state"},
    "hue_set_light": {"hue_list_devices", "get_current_state", "query_history"},
    "speedtest_run": {"get_current_state", "query_history", "get_data_range"},
}

# Backticked identifiers that are payload keys, parameters or settings - not tools. The
# stale-reference check below flags any other underscored token, so a genuinely new key
# lands here and a renamed tool lands as a failure.
NON_TOOL_IDENTIFIERS = {
    "as_of",
    "brightness_pct",
    "group_by",
    "color_temp_k",
    "duration_seconds",
    "instance_tag",
    "limit_per_instance",
    "points_present",
    "retention.known",
    "span_seconds",
}

# Words that describe how a call fails. A description mentioning none of them is
# silent about its failure behaviour. Stems, so "fails"/"failure"/"failing" all count -
# the guard is that failure is addressed at all, not that it is worded one way.
FAILURE_WORDS = ("error", "reject", "refus", "fail", "unreachable")

# How a read tool states its side effects in prose. read_only_hint says the same thing
# in a field clients are told to distrust, so the description carries it as well. One
# phrase rather than a list of accepted wordings: the surface reads as one voice, and a
# guard that accepts any of six phrasings stops guarding anything.
READ_ONLY_PHRASE = "changes nothing"

# What each writing tool must say it does, since "read_only_hint is False" is a field a
# client may distrust and says nothing about *what* changes. Declared per tool rather
# than matched against a list of hopeful keywords: an earlier version of this guard
# accepted the substring "run", which "truncated" satisfies, so it passed on
# descriptions that said nothing at all. A new write tool fails until its own phrase is
# named here.
WRITE_EFFECT_PHRASES = {
    "hue_set_light": "changes a real device",
    "speedtest_run": "saturates the connection",
}

# Recorded ceilings, not predictions - see the table in this module's docstring for
# what is actually measured. Raising one is a deliberate decision that belongs in the
# commit message with its reason.
MAX_TOOL_BYTES = 11_500
MAX_SINGLE_TOOL_BYTES = 2_100
MAX_PROMPT_BYTES = 600
MAX_BYTES_PER_RESOURCE = 400
MAX_TOTAL_BYTES = 13_500

SETTINGS = {
    "sources": ["hue", "speedtest"],
    "influx": {"url": "http://influx.example", "user": "u", "password": "p"},
    "hue": {"host": "hue.example", "user": "abc", "db": "hue_db", "mcp_read_write": True},
    "speedtest": {"db": "speedtest_db", "mcp_read_write": True},
}


def _server():
    """Build the whole advertised surface: every read tool, both write-enabled
    sources' tools, all three prompts, and the per-source resources.

    Needs no mocking - registration reads settings and class metadata only, and
    ``enabled_sources`` is passed so the write/prompt gate does not construct
    handlers to decide.
    """
    server = MCPServer(name="surface")
    register_read_tools(server, SETTINGS, None)
    register_write_tools(server, SETTINGS, None, enabled_sources=["hue", "speedtest"])
    register_prompts(server, SETTINGS, None, enabled_sources=["hue", "speedtest"])
    register_resources(server, SETTINGS, None)
    return server


def _blen(text):
    """Size of one advertised string in bytes, which is what the context costs."""
    return len((text or "").encode("utf-8"))


def _prose(text):
    """One description as a single whitespace-normalised line.

    Every prose check below matches substrings, and a docstring keeps the newlines it
    was wrapped with - so "changes nothing" split across a line break would fail a
    guard that the description actually satisfies. Line wrapping is incidental to what
    was written, so it is normalised away before matching rather than worked around in
    each assertion.
    """
    return " ".join((text or "").split())


@pytest.fixture(scope="module")
def surface():
    """The three advertised lists, built once."""
    server = _server()
    return {
        "tools": anyio.run(server.list_tools),
        "prompts": anyio.run(server.list_prompts),
        "resources": anyio.run(server.list_resources),
    }


class TestEverythingAdvertisedIsDescribed:
    def test_the_fixture_registers_the_whole_surface(self, surface):
        # Nothing below can pass vacuously: if a registration is dropped or the
        # fixture stops enabling writes, this fails first and names what is missing.
        assert {tool.name for tool in surface["tools"]} == set(SIBLINGS)
        assert {prompt.name for prompt in surface["prompts"]} == {
            "home_status",
            "usage_trends",
            "control_device",
        }
        assert {str(resource.uri) for resource in surface["resources"]} == {
            "docs://reference",
            "schema://hue",
            "schema://speedtest",
            "state://hue",
            "state://speedtest",
        }

    @pytest.mark.parametrize("kind", ["tools", "prompts", "resources"])
    def test_every_registration_has_a_description(self, surface, kind):
        for item in surface[kind]:
            label = getattr(item, "name", None) or str(item.uri)
            assert item.description, f"{kind[:-1]} {label} has no description"

    @pytest.mark.parametrize("kind", ["tools", "prompts", "resources"])
    def test_every_registration_has_a_title_distinct_from_its_name(self, surface, kind):
        for item in surface[kind]:
            label = getattr(item, "name", None) or str(item.uri)
            assert item.title, f"{kind[:-1]} {label} has no title"
            assert item.title != item.name, f"{kind[:-1]} {label}'s title repeats its name"

    def test_each_resource_says_which_tool_covers_the_same_data(self, surface):
        # The design rule is that anything exposed as a resource is also a tool. A
        # client choosing between them can only know that if the resource says so.
        covering = {
            "docs://reference": "get_documentation",
            "schema://hue": "list_fields",
            "schema://speedtest": "list_fields",
            "state://hue": "get_current_state",
            "state://speedtest": "get_current_state",
        }
        for resource in surface["resources"]:
            tool = covering[str(resource.uri)]
            assert tool in _prose(resource.description), f"{resource.uri} does not name its tool `{tool}`"


class TestToolDescriptionsDiscriminate:
    def test_every_tool_names_its_declared_siblings(self, surface):
        for tool in surface["tools"]:
            for sibling in sorted(SIBLINGS[tool.name]):
                assert f"`{sibling}`" in _prose(tool.description), (
                    f"{tool.name} does not name its sibling `{sibling}` - a caller cannot tell "
                    f"why to pick one rather than the other"
                )

    def test_no_tool_names_a_tool_that_does_not_exist(self, surface):
        import re

        registered = {tool.name for tool in surface["tools"]}
        for tool in surface["tools"]:
            for token in re.findall(r"`([A-Za-z][\w.]*_[\w.]*)`", _prose(tool.description)):
                assert token in registered or token in NON_TOOL_IDENTIFIERS, (
                    f"{tool.name} names `{token}`, which is not a registered tool. Fix the "
                    f"reference, or add it to NON_TOOL_IDENTIFIERS if it is a key or setting"
                )

    def test_every_tool_states_how_it_fails(self, surface):
        for tool in surface["tools"]:
            lowered = _prose(tool.description).lower()
            assert any(word in lowered for word in FAILURE_WORDS), (
                f"{tool.name} says nothing about how it fails; the annotation fields cannot "
                f"carry that and clients are told to treat them as untrusted anyway"
            )

    def test_every_tool_says_whether_it_changes_anything(self, surface):
        # Side effects in prose, per the standard: read_only_hint is a hint a client
        # may distrust, so the description carries it too. A read tool says it changes
        # nothing; a write tool says what it changes.
        for tool in surface["tools"]:
            lowered = _prose(tool.description).lower()
            if tool.annotations.read_only_hint:
                assert READ_ONLY_PHRASE in lowered, (
                    f"{tool.name} is read-only but does not say so in prose " f"(expected {READ_ONLY_PHRASE!r})"
                )
            else:
                phrase = WRITE_EFFECT_PHRASES.get(tool.name)
                assert phrase, (
                    f"{tool.name} writes but WRITE_EFFECT_PHRASES does not say what it "
                    f"changes - decide, then assert it"
                )
                assert phrase in lowered, (
                    f"{tool.name} writes but its description does not say so " f"(expected {phrase!r})"
                )


class TestSurfaceBudget:
    """The total is tracked as a metric, so an addition is judged against a figure."""

    def test_tool_descriptions_stay_within_budget(self, surface):
        sizes = {tool.name: _blen(tool.description) for tool in surface["tools"]}
        total = sum(sizes.values())
        biggest, biggest_size = max(sizes.items(), key=lambda item: item[1])
        assert biggest_size <= MAX_SINGLE_TOOL_BYTES, (
            f"`{biggest}` is {biggest_size} bytes, over the {MAX_SINGLE_TOOL_BYTES}-byte " f"per-tool ceiling"
        )
        assert total <= MAX_TOOL_BYTES, (
            f"tool descriptions total {total} bytes, over the {MAX_TOOL_BYTES}-byte budget: " f"{sizes}"
        )

    def test_prompt_descriptions_stay_within_budget(self, surface):
        total = sum(_blen(prompt.description) for prompt in surface["prompts"])
        assert total <= MAX_PROMPT_BYTES, f"prompt descriptions total {total} bytes"

    def test_resource_descriptions_stay_within_budget(self, surface):
        # Per resource, not in total: the count grows with the number of configured
        # sources, so a total would fail on an install with more of them rather than
        # on a description that grew.
        for resource in surface["resources"]:
            size = _blen(resource.description)
            assert size <= MAX_BYTES_PER_RESOURCE, f"{resource.uri} is {size} bytes"

    def test_the_whole_surface_stays_within_budget(self, surface):
        total = sum(_blen(item.description) for kind in ("tools", "prompts", "resources") for item in surface[kind])
        assert total <= MAX_TOTAL_BYTES, (
            f"the advertised description surface totals {total} bytes, over the "
            f"{MAX_TOTAL_BYTES}-byte budget - raising it is a deliberate decision that "
            f"belongs in the commit message with its reason"
        )
