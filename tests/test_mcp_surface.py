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
  its name. Both fields are optional in the MCP schema - verified against revision
  2026-07-28 of the specification, whose Tool/Prompt/Resource data types make them
  so - and nothing but a test therefore stops a new registration shipping without
  them, which is exactly how the resources shipped from 5.0 to 5.3 advertising a
  URI, a name and nothing else.
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

The recorded budget is the constants below rather than a figure repeated here: a
measurement written into prose is a second copy of a number the assertions already
hold, and only the assertions are what CI reads. ``architecture/mcp-server.md``
carries the reasoning for the surface's shape and the history of how it grew.
"""

from mcp.server.mcpserver import MCPServer

import anyio
import pytest

from toinflux.mcp_common import TRANSLATES_FAILURES
from toinflux.mcp_dashboards import register_dashboard_tools
from toinflux.mcp_prompts import register_prompts
from toinflux.mcp_read import register_read_tools
from toinflux.mcp_resources import register_resources
from toinflux.mcp_write import register_write_tools

# Which siblings each tool must name, so a caller can tell why to pick it rather than
# them. Nearest neighbour, not every tool: naming every tool in every description would
# pay for the whole surface once per tool and discriminate nothing.
SIBLINGS = {
    "list_sources": {"list_fields", "query_history", "get_current_state", "get_documentation"},
    "list_fields": {"list_sources", "query_history", "get_documentation"},
    # The dashboard tool's neighbours are the two a caller would otherwise reach for:
    # the raw schema, and running a query rather than being handed one.
    "suggest_dashboard_panels": {"list_fields", "query_history"},
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
    "panel_type",
    "value_mappings",
    "avoid_aggregations",
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
MAX_TOOL_BYTES = 13_550
MAX_SINGLE_TOOL_BYTES = 2_100
MAX_PROMPT_BYTES = 600
MAX_BYTES_PER_RESOURCE = 400
MAX_TOTAL_BYTES = 15_750

SETTINGS = {
    "sources": ["hue", "speedtest"],
    "influx": {"url": "http://influx.example", "user": "u", "password": "p"},
    "hue": {"host": "hue.example", "user": "abc", "db": "hue_db", "mcp_read_write": True},
    "speedtest": {"db": "speedtest_db", "mcp_read_write": True},
}


def _server():
    """Build the whole advertised surface: every read tool, both write-enabled
    sources' tools, every prompt, and the per-source resources.

    Needs no mocking - registration reads settings and class metadata only, and
    ``enabled_sources`` is passed so the write/prompt gate does not construct
    handlers to decide.
    """
    server = MCPServer(name="surface")
    register_read_tools(server, SETTINGS, None)
    register_dashboard_tools(server, SETTINGS, None)
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


@pytest.fixture(scope="module")
def registered():
    """The server itself, for the checks that need the registered callables.

    The ``surface`` fixture holds what a client is *told*; this holds what would
    actually run, which is where the failure-translation guard has to look.
    """
    return _server()


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

    def test_no_description_advertises_leaked_source_indentation(self, surface):
        """A description must not carry its docstring's own indentation.

        `register_tool()` runs `cleandoc`, which strips the indent *common* to every line
        after the first - so a docstring whose paragraphs are indented inconsistently has
        only the smallest prefix removed, and the rest is advertised as leading
        whitespace. That is the same waste `register_tool` exists to prevent, arriving by
        a different route, and neither the byte budget nor the prose guards notice it: it
        looks like ordinary bytes.

        Found in `suggest_dashboard_panels`, which advertised 112 bytes of it from the
        commit that added it - its body was at 16 spaces and one later-edited paragraph at
        8, so `cleandoc` removed 8 and left 8 on fourteen lines.

        Two spaces is deliberate continuation - `query_history` indents its parameter list
        that way - so the threshold is four, which no hand-written continuation uses.
        """
        offenders = []
        for kind in ("tools", "prompts", "resources"):
            for item in surface[kind]:
                for line in (item.description or "").splitlines():
                    if line.strip() and (len(line) - len(line.lstrip())) >= 4:
                        offenders.append(f"{getattr(item, 'name', item)}: {line[:60]!r}")
        assert not offenders, "advertised description(s) carrying leaked indentation: " + "; ".join(offenders)

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


class TestEveryAdvertisedThingExplainsItsFailures:
    """The guard for the hole that let a whole half of the surface go unchecked.

    The tool half of this was caught by CI, but only because two tests in
    test_mcp_read.py happened to assert on an error message. The resource half was
    caught by nothing: test_mcp_resources.py covered three happy-path reads and no
    failure at all, so `state://hue` answering "Error reading resource state://hue" to
    an unreachable InfluxDB, an unusable source and a genuine bug alike was invisible
    to the entire suite. It was found by reading the SDK, which is not a process.

    Per-item tests could not have closed that: they cover what someone remembered to
    write one for, which is exactly what went missing. So this asks the built server
    instead. Every tool and every resource it advertises must carry a wrapper from
    `translate_failures()`, whichever module registered it and whenever it was added -
    including a module that does not exist yet, which is the case the source greps in
    the two registrar modules cannot reach.
    """

    def test_every_registered_tool_translates_anticipated_failures(self, registered):
        unwrapped = sorted(
            tool.name
            for tool in registered._tool_manager.list_tools()
            if not getattr(tool.fn, TRANSLATES_FAILURES, False)
        )
        assert not unwrapped, (
            f"tools {unwrapped} do not translate ToolParamError/SourceConnectionError - register "
            f"through register_tool() so a caller mistake reaches the model with its message "
            f"instead of a bare 'Error executing tool <name>'"
        )

    def test_every_registered_resource_translates_anticipated_failures(self, registered):
        # Templates as well as static resources. `list_resources()` returns only `_resources`;
        # a templated URI (`state://{source}`, the alternative this module's docstring records
        # considering) lands in the separate `_templates` dict, so reading one list alone would leave
        # a whole registration kind unchecked while still reporting "no bad entries found".
        registry = registered._resource_manager
        entries = [(str(r.uri), getattr(r, "fn", None)) for r in registry.list_resources()]
        entries += [(t.uri_template, getattr(t, "fn", None)) for t in registry.list_templates()]
        unwrapped = sorted(uri for uri, fn in entries if not getattr(fn, TRANSLATES_FAILURES, False))
        assert not unwrapped, (
            f"resources {unwrapped} do not translate ToInfluxError - register through "
            f"_register_resource() so a failed read says why instead of a bare "
            f"'Error reading resource <uri>'"
        )

    def test_the_guard_is_actually_reading_the_surface(self, registered):
        """A guard that silently enumerated nothing would pass forever.

        Both assertions above are "no bad entries found", which an empty list satisfies -
        so if a future SDK renames `_tool_manager`, stops holding the callable on `.fn`,
        or registers through some other path, they would go green while checking nothing
        at all. This fails instead, which is the difference between a check that skipped
        and a check that passed.
        """
        tools = registered._tool_manager.list_tools()
        resources = registered._resource_manager.list_resources()
        templates = registered._resource_manager.list_templates()
        assert {tool.name for tool in tools} == set(SIBLINGS), "not every tool reached the tool manager"
        assert len(resources) == 5, f"enumerated {len(resources)} resources, expected the fixture's 5"
        # No templated URIs today. Asserted rather than assumed: the moment one is added, this fails
        # and points at the guard above, which has to keep covering both kinds.
        assert not templates, f"a resource template appeared ({[t.uri_template for t in templates]}) - see above"
        assert all(callable(getattr(tool, "fn", None)) for tool in tools), "Tool.fn is no longer the callable"
        assert all(
            callable(getattr(resource, "fn", None)) for resource in resources
        ), "Resource.fn is no longer the callable"
