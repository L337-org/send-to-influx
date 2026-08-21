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

===========  ==============  ==================  ===================
category     5.3 (released)  after the prose     after the schema
                             pass                pass
===========  ==============  ==================  ===================
tools                 9,937              11,252               11,809
prompts                 225                 523                  523
resources                 0               1,521                1,651
**total**        **10,162**          **13,296**           **13,983**
===========  ==============  ==================  ===================

The columns are named after the change that produced each measurement rather than after a
release, because 5.3 is the last released version and both later columns are unreleased -
naming a release here would invent one. The "prose pass" is the change that gave every
registration a description and a title; the "schema pass" is the one that made
``list_fields`` sufficient to build a query and a chart from.

The schema pass adds 687 bytes, and all of it is contract rather than commentary - a
caller cannot use a payload key it has not been told about, nor rely on one it was told
about unconditionally:

* ``list_fields`` +464 (1,165 -> 1,629): four keys that were not in the payload before
  (``database``, ``tag_keys``, and each field's ``type`` and ``kind``), a new ``detail``
  parameter, and the three-word vocabulary ``kind`` uses. ``kind`` in particular is
  unusable without knowing that 'counter' forbids a mean - which is the failure the
  payload exists to prevent - so it is stated here rather than left to
  ``get_documentation``, because the decision it informs is made while reading this
  tool's result. The same edit *removed* two things to hold the raise down: an example of
  which bridges appear in ``instances`` (the sentence before it already states the rule),
  and a note that ``detail`` saves a round trip (a justification for the design, not
  something a caller acts on). A later +154 states the rule that *every* per-field key is
  absent rather than null when there is nothing to say - written once as a rule rather than
  as a caveat on each key, which is both shorter and more complete. ``type`` is the key
  where that surprises a reader, since InfluxDB does not report one for every field, and
  three separate descriptions had promised it unconditionally.
* ``get_documentation`` +93 and ``schema://<source>`` +130 between the two of them: both
  had stopped describing what they return. The generated reference now carries how each
  field may be aggregated and a per-field description, and the schema resource now
  carries the database, the tag keys and each field's type and kind - and both still
  advertised only "units and coded values". This is the same defect the prose pass was for,
  reappearing on the same day the payload grew, which is the argument for finding it in a
  review of the diff rather than trusting that a description follows its behaviour.

Identical on every supported Python since ``register_tool()`` dedents each docstring:
before it, 3.10-3.12 advertised 14,569 bytes where 3.13+ advertised 13,297, the
difference being pure leading whitespace. The budget assertions below are what found
that, on their first CI run.

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
    "tag_keys",
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
MAX_TOOL_BYTES = 11_900
MAX_SINGLE_TOOL_BYTES = 2_100
MAX_PROMPT_BYTES = 600
MAX_BYTES_PER_RESOURCE = 400
MAX_TOTAL_BYTES = 14_050

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


class TestTheSurfaceIsVersionIndependent:
    """The same bytes on every supported Python, and nothing bypassing the registrar.

    CPython 3.13 strips a docstring's leading indentation at compile time and 3.10-3.12
    do not, while the SDK advertises ``fn.__doc__`` verbatim - so before
    ``register_tool()`` existed, the older half of the supported range shipped every
    continuation line with eight leading spaces on it, 1,272 bytes of whitespace across
    the surface, invisible to anyone developing on 3.13+. The budget test below found it
    on its first CI run; these two keep it found.
    """

    def test_every_advertised_description_is_already_normalised(self, surface):
        # cleandoc is idempotent, so a correctly registered description equals its own
        # cleandoc - and an un-dedented one does not, because a common margin is exactly
        # what cleandoc removes. Real on 3.10-3.12, trivially true on 3.13+.
        #
        # Deliberately not "no line starts with whitespace": the bullet lists in
        # query_history and get_data_range wrap with a two-space hanging indent, which is
        # meaningful structure in the text rather than an artefact of the source. An
        # earlier version of this assertion banned all leading whitespace and failed on
        # exactly those, which would have meant flattening readable prose to satisfy a
        # guard that had the wrong rule.
        import inspect

        for kind in ("tools", "prompts", "resources"):
            for item in surface[kind]:
                label = getattr(item, "name", None) or str(item.uri)
                description = item.description or ""
                assert description == inspect.cleandoc(description), (
                    f"{kind[:-1]} {label} advertises an un-normalised description "
                    f"({len(description.encode())} bytes, "
                    f"{len(inspect.cleandoc(description).encode())} once dedented) - "
                    f"register through register_tool() so every supported Python "
                    f"advertises the same bytes"
                )

    def test_nothing_registers_a_tool_around_the_registrar(self):
        # The effect test above cannot catch a bypass on 3.13+, where the compiler hides
        # it, so the source is checked directly - the one guard that fails on every
        # version, including the machine the mistake is made on.
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        for module in ("toinflux/mcp_read.py", "toinflux/mcp_write.py"):
            text = (root / module).read_text(encoding="utf-8")
            assert "@server.tool(" not in text, (
                f"{module} registers a tool directly with @server.tool - use "
                f"@register_tool(server, ...) so the docstring is dedented first"
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
