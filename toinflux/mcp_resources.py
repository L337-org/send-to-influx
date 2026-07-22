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
* ``schema://<source>`` - one per source: its fields, units and code meanings.
* ``state://<source>`` - one per source: its current/live state.

The per-source resources are registered concretely (one per configured source),
not as a single URI template, so a client's ``resources/list`` enumerates each
source's snapshot and schema directly.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT License"

from toinflux.mcp_common import configured_sources
from toinflux.mcp_read import build_documentation, current_state_result, list_fields_result


def register_resources(server, settings, settings_file=None):
    """Register the read resources on a FastMCP server: the documentation
    reference, plus a schema and a current-state resource per configured source.
    Blocking work runs in a worker thread, mirroring the read tools.

    :param server: the FastMCP instance
    :param settings: parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per read
    :return: the server
    """
    import anyio

    @server.resource("docs://reference", name="data-reference", mime_type="text/markdown")
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

    @server.resource(f"schema://{source}", name=f"{source}-schema", mime_type="application/json")
    async def _schema_resource() -> dict:
        return await anyio.to_thread.run_sync(list_fields_result, source, settings, settings_file)

    @server.resource(f"state://{source}", name=f"{source}-state", mime_type="application/json")
    async def _state_resource() -> dict:
        return await anyio.to_thread.run_sync(current_state_result, source, settings, settings_file)
