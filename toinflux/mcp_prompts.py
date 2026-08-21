"""MCP prompts for the read/control surface.

Prompts are reusable, parameterised task templates a user invokes from the client
(Claude Desktop/Mobile). They add no capability - they orient the model on how to
combine the read tools (current state, history, documentation) and, when enabled,
the device-write tools, for the tasks this server is actually for:

* ``home_status`` - summarise the current state of everything (or a focus area).
* ``usage_trends`` - historical analysis and cross-source comparison.
* ``control_device`` - the check-state -> act flow for a device request. Registered
  only when at least one source has writes enabled, mirroring the write tools, so a
  read-only install never offers a control prompt for a capability that isn't there.

Kept generic/parameterised (a free-text focus/question/request), never hard-coding
specific devices, so they fit any install's sources.

Each carries a ``title`` and a one-line ``description`` naming the sibling prompt
a user might otherwise pick. Both are deliberately kept to one line: unlike a tool
description, a prompt's is picker text a person reads, and the instructions the
model follows live in the returned message rather than in the advertised surface.
``tests/test_mcp_surface.py`` is the guard.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

from toinflux.mcp_write import writable_enabled_sources


def register_prompts(server, settings, settings_file=None, enabled_sources=None):
    """Register the task prompts on a MCPServer server. ``home_status`` and
    ``usage_trends`` are always registered; ``control_device`` only when a source
    has writes enabled (so it isn't offered on a read-only install).

    :param server: the MCPServer instance
    :param settings: parsed settings dict
    :param settings_file: settings path, for the write-enabled check
    :param enabled_sources: the pre-computed write-enabled source list, if the
        caller already has it (build_mcp_server shares one computation with
        register_write_tools); ``None`` computes it here, so the function still
        stands alone.
    :return: the server
    """
    if enabled_sources is None:
        enabled_sources = writable_enabled_sources(settings, settings_file)

    @server.prompt(
        name="home_status",
        title="Home Status",
        description=(
            "Summarise your devices' state right now, optionally narrowed to a focus like 'lights' "
            "or 'energy'. For recorded history rather than the present moment, use usage_trends."
        ),
    )
    def home_status(focus: str = "") -> str:
        scope = f" Focus on: {focus.strip()}." if focus.strip() else ""
        return (
            "Give me a concise summary of my smart-home and energy devices right now." + scope + "\n\n"
            "Use `list_sources` to see what's available, then `get_current_state` for the relevant "
            "sources to read their live state (values already carry units and decoded labels). Group "
            "the summary sensibly (e.g. lights, security, energy, weather) and call out anything that "
            "may need attention - a door unlocked or open, a low or critical battery, a device that's "
            "offline, or a reading that looks wrong. This is about the present moment, so don't use "
            "`query_history`."
        )

    @server.prompt(
        name="usage_trends",
        title="Usage Trends",
        description=(
            "Answer a question from recorded history, e.g. 'electricity this month vs last month'. "
            "For the present moment rather than a trend, use home_status."
        ),
    )
    def usage_trends(question: str) -> str:
        return (
            f"Help me answer this from my recorded history: {question}\n\n"
            "Find the right source, fields and units with `get_documentation` (or `list_sources`/"
            "`list_fields`), then use `query_history` to pull the data: pick a time range that matches "
            "the question and aggregate where it makes sense (e.g. `sum` for energy over a period, "
            "`mean` for a rate; use `group_by` to bucket by hour/day). To compare periods, query each "
            "and compare. Cross-reference sources where the question needs it (for example carbon "
            "intensity against charging power). Present the answer with units, and say so if the "
            "available history doesn't fully cover the period asked about."
        )

    if enabled_sources:

        @server.prompt(
            name="control_device",
            title="Control a Device",
            description=(
                "Carry out a device control request, e.g. 'turn on the kitchen light' - checking "
                "state first and confirming what changed. Offered only where a source has writes "
                "enabled; home_status and usage_trends only read."
            ),
        )
        def control_device(request: str) -> str:
            return (
                f"I'd like to control a device: {request}\n\n"
                "Pick the right control tool from the ones available - each controllable source has "
                "its own (e.g. `hue_list_devices`/`hue_set_light` for Hue lights and plugs, "
                "`speedtest_run` to trigger a speed test). List the devices first where the tool "
                "offers it; if the request is ambiguous or names a device that isn't listed, ask me "
                "rather than guessing - actuating the wrong device isn't recoverable. Where it helps, "
                "check the current state first with `get_current_state`. Then apply the change and "
                "confirm what actually changed. If the requested control isn't available (the device "
                "is read-only, or control isn't enabled for that source), tell me plainly instead of "
                "working around it."
            )

    return server
