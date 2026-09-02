"""MCP resource registration for the read surface.

Resources are the addressable, listable view of the same read data the tools
serve. The design rule is that anything exposed as a resource is also exposed as a
tool, so these mirror the read tools (`get_documentation`, `list_fields`,
`get_current_state`) rather than adding behaviour of their own - MCP clients use
resources in limited ways, so the tools stay the workhorses and these are the
discoverable, attachable counterpart, built from the source classes' own metadata
via :mod:`toinflux.mcp_read`.

Three kinds:

* ``docs://reference`` - the units/coded-value documentation (Markdown).
* ``schema://<source>`` - one per source: the ``list_fields`` payload for it.
* ``state://<source>`` - one per source: its current/live state.

The per-source resources are registered concretely (one per configured source),
not as a single URI template, so a client's ``resources/list`` enumerates each
source's snapshot and schema directly.

Each carries a ``title`` and a ``description`` as well as its URI, name and MIME
type: the description is the only thing telling a client enumerating
``resources/list`` what a URI holds, whether reading it costs an InfluxDB round
trip, and which tool covers the same data. Both fields are optional in the MCP
schema and were absent until the surface pass that added them, so a client saw a
URI and a name and nothing else. ``tests/test_mcp_surface.py`` is the guard.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

from mcp.server.mcpserver.exceptions import ResourceError

from toinflux.mcp_common import configured_sources, translate_failures
from toinflux.mcp_read import build_documentation, current_state_result, list_fields_result


def _register_resource(server, *args, **kwargs):
    """Register a resource whose anticipated failures keep their own message.

    The resource-side counterpart of ``register_tool()``, and the same argument for
    the same reason: the SDK decides what a failure says to the client purely by its
    type. ``read_resource()`` re-raises ``ResourceError`` as it is, logs it at INFO,
    and sends the message on; anything else becomes
    ``UnexpectedResourceError("Error reading resource <uri>")``, logged at ERROR with
    a traceback and its text withheld. So a resource read that failed because
    InfluxDB was unreachable said only that it could not be read, and a client had no
    way to tell that from an unknown source or a bad schema.

    Unlike the tool half this was never a regression - mcp 2.0.0 flattened even a
    deliberate ``ResourceError`` to the generic message ("we should not leak the
    exception to the client"), so there was nothing to keep. 2.1.0 is what made a
    resource able to say why it failed, which is why this arrives with that upgrade.

    Only ``ToInfluxError`` is translated, for the reason spelled out in
    ``translate_failures()``: a bug must stay a crash, logged with its traceback and
    its text kept off the wire.

    :param server: the MCPServer instance
    :param args: passed to ``server.resource()`` (the URI)
    :param kwargs: passed to ``server.resource()`` (name, title, description, ...)
    :return: a decorator registering the function as a resource
    """

    def decorator(fn):
        return server.resource(*args, **kwargs)(translate_failures(fn, ResourceError))

    return decorator


def register_resources(server, settings, settings_file=None):
    """Register the read resources on a MCPServer server: the documentation
    reference, plus a schema and a current-state resource per configured source.
    Blocking work runs in a worker thread, mirroring the read tools.

    :param server: the MCPServer instance
    :param settings: parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per read
    :return: the server
    """
    import anyio

    @_register_resource(
        server,
        "docs://reference",
        name="data-reference",
        title="Data Reference",
        description=(
            "What every configured source reports and what its values mean: units, and the "
            "meaning of coded values (e.g. Nuki lock and door state codes). The same content "
            "the `get_documentation` tool returns. Static metadata, so reading it makes no "
            "InfluxDB or device request."
        ),
        mime_type="text/markdown",
    )
    async def _documentation_resource() -> str:
        return await anyio.to_thread.run_sync(build_documentation, settings, settings_file)

    for source in configured_sources(settings):
        _register_source_resources(server, anyio, source, settings, settings_file)
    return server


def _register_source_resources(server, anyio, source, settings, settings_file):
    """Register the schema and current-state resources for one source.

    A factory (not an inline loop body) so each resource closure binds its own
    ``source`` - a closure over the loop variable would make every resource read
    the last source.
    """

    @_register_resource(
        server,
        f"schema://{source}",
        name=f"{source}-schema",
        title=f"{source} schema",
        description=(
            f"Everything needed to query {source}: database, measurement, the tag keys to group "
            f"by, and each field's type, unit, coded-value meanings and aggregation - each omitted "
            f"where unknown, `type` included. Where several producers write to it, also the tag "
            f"telling them apart and the values it accepts. Discovered live from InfluxDB, which "
            f"must be reachable; the `list_fields` payload, `detail` false."
        ),
        mime_type="application/json",
    )
    async def _schema_resource() -> dict:
        return await anyio.to_thread.run_sync(list_fields_result, source, settings, settings_file)

    @_register_resource(
        server,
        f"state://{source}",
        name=f"{source}-state",
        title=f"{source} current state",
        description=(
            f"{source}'s state at the moment it is read, each field with its unit and any "
            f"decoded label - read live from the device where that is cheap, otherwise the "
            f"latest point recorded in InfluxDB, as the `state` field says. Reading changes "
            f"nothing; the same payload the `get_current_state` tool returns for {source}."
        ),
        mime_type="application/json",
    )
    async def _state_resource() -> dict:
        return await anyio.to_thread.run_sync(current_state_result, source, settings, settings_file)
