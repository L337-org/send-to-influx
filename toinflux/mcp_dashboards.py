"""Dashboard-panel suggestions for the MCP server: turn a source's schema into the
query, panel type, aggregation and value mappings a Grafana panel needs.

**Why this is a tool and not a prompt.** A prompt would have been cheaper - no
permanent advertised surface - but it fails at the one job this exists for. A prompt
is not in the model's tool list, so nothing tells a model that this data can be
charted; and MCP clients vary in whether they surface prompts at all. A tool name is
in context every session, which *is* the discovery mechanism. It is also the only
form that can be tested: "never take the mean of a counter" is a hope in prose and an
assertion in CI.

**Why this is its own module.** Every Grafana-specific fact this project knows lives
here and nowhere else - the panel type names, the unit identifiers, the value-mapping
shape. ``MCP_FIELD_METADATA`` and ``list_fields`` stay vendor-neutral, which was a
deliberate decision when this work was scoped: encoding another product's vocabulary
into the schema would undo the separation the schema depends on. A module boundary
makes that hold by construction rather than by intention - ``mcp_read`` does not
import this, so Grafana vocabulary cannot leak back into the schema.

**What it deliberately does not do: emit dashboard JSON.** The envelope is
version-dependent and untestable here. Measured against a real Grafana 13.2, a saved
dashboard came back *verbatim* with nothing added and no ``schemaVersion`` at all
(its ``meta.apiVersion`` is ``v0alpha1``), where older Grafana carries that field and
more filled-in defaults. Emitting a template for a product we do not control and
cannot assert against in CI would rot silently. So this returns the parts we own and
can test - the query above all, which is the error-prone half - and the caller wraps
them in whatever its own Grafana wants, reading an existing dashboard for the shape.

Everything below was established by execution against Grafana 13.2.0 and InfluxDB
1.8, not from recollection: the target field names, that ``$timeFilter`` and
``$__interval`` both resolve, that ``alias`` interpolates a tag value and passes a
literal through unchanged (and that without one the series is named
``<measurement>.<function>``), the value-mapping shape, and every unit identifier in
``GRAFANA_UNITS``.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

from mcp.types import ToolAnnotations

from toinflux.exceptions import ToolParamError
from toinflux.mcp_common import close_session, register_tool
from toinflux.mcp_read import build_panel_query, field_kind, resolve_schema

# This source's display unit -> Grafana's own unit identifier. Every id here was read
# out of the running Grafana's bundled frontend, because Grafana accepts any string
# server-side: a wrong id is stored happily, and what it then renders is *not* nothing -
# see below, which is the correction to an earlier belief recorded here.
#
# What an unrecognised id actually does, from `getValueFormat()` in grafana-data's
# valueFormats.ts: an id containing no recognised `key:` prefix falls through to
# `toFixedUnit(id)`, which returns `{text, suffix: " " + id}`. So a bare unknown string
# renders as a literal suffix - `123 W/m²` - rather than being dropped. Grafana's own
# test pins the equivalent explicit form: `suffix:d` on 1532.82 gives `1533 d`.
#
# The consequence for a typo: it does not vanish, it appears on the axis. That is more
# visible than the "unformatted axis" this comment used to claim, not less.
#
# So a unit Grafana has no identifier for is *emitted* as its `suffix:` form rather than
# withheld: W/m2, gCO2/kWh and pence/kWh all label their axis correctly. They were
# withheld while this comment claimed a passed-through string rendered as nothing, which
# cost a correct label for no reason.
#
# "kWh or m3" is the one exception and stays absent: it is two units rather than one,
# because Octopus reports gas in whichever the meter uses, so no single suffix is honest.
GRAFANA_UNITS = {
    "W": "watt",
    "kWh": "kwatth",
    # All three of Hue's temperature settings, not just the default: an install with
    # hue.temperature_units: F emits °F, and leaving it unmapped meant a bare axis on a
    # temperature panel of all things. Grafana has ids for each (categories.ts).
    "°C": "celsius",
    "°F": "fahrenheit",
    "K": "kelvin",
    "%": "percent",
    "bits/s": "bps",
    "ms": "ms",
    "Hz": "hertz",
    "mm": "lengthmm",
    "km/h": "velocitykmh",
    "lux": "lux",
    # Units Grafana has no identifier for, emitted as its explicit custom-suffix form so
    # the axis reads "123 W/m²" rather than a bare number.
    #
    # `suffix:<text>` rather than the bare string, though both render identically today:
    # an unrecognised id falls through to `toFixedUnit(id)`, which is the same formatter
    # `suffix:` selects. The difference is what happens later - a bare `W/m²` would
    # silently start using Grafana's own formatter, possibly one that rescales the value,
    # if an identifier of that name were ever added. `suffix:` cannot be captured that way.
    #
    # Not a theoretical rescale: run against @grafana/data 13.2.0's own getValueFormat(),
    # `watt` turns 3200 into "3.20 kW", where `suffix:W/m²` gives "812.40 W/m²" and a bare
    # `W/m²` gives the same - today. An id nobody defines shows its own text ("42.00
    # not-a-real-unit"), which is how a typo here surfaces.
    "W/m²": "suffix:W/m²",
    "gCO2/kWh": "suffix:gCO2/kWh",
    # Shortened on purpose: the suffix repeats on every tick and in every tooltip, so
    # "12.5 pence/kWh (inc. VAT)" would be unreadable on an axis. The full string, VAT
    # qualifier and all, is what list_fields and get_documentation report - which is where
    # a caller reads what a field means, rather than off a chart.
    "pence/kWh (inc. VAT)": "suffix:p/kWh",
    # "kWh or m³" is deliberately absent and stays that way: it is two units, not one, so
    # no single suffix is honest. Octopus reports gas in whichever the meter uses.
}

# How each field kind is charted, and how it must not be aggregated.
#
# `avoid` is the load-bearing half and the reason this is a tool: taking the mean of a
# counter that resets produces a plausible line that means nothing, and nothing in the
# data itself says so. Saying which aggregations are wrong is more useful than naming
# only the right one, because a caller composing its own query needs to recognise the
# mistake, not just copy the suggestion.
_KIND_PANELS = {
    # An instantaneous reading, so a sum adds up quantities that never existed.
    #
    # That warning is only sound because interval quantities have their own kind now.
    # While they were declared gauges, `sum` was listed here and the tool advised callers
    # away from the right aggregation for Octopus consumption and Open-Meteo
    # precipitation; dropping it then made the warning useless for a real temperature.
    # Splitting the kind is what lets both statements be true at once, which is the
    # argument for having done it rather than picking whichever error was cheaper.
    "gauge": {"panel_type": "timeseries", "aggregation": "mean", "avoid": ("sum",)},
    # A quantity accumulated during its reporting interval. Summing gives the total for
    # any range, which is the whole point of the kind, and nothing is ruled out: a mean
    # is the average interval, legitimately, so long as nobody reads it as a rate.
    #
    # Charted as a time series rather than a bar chart, which would arguably suit an
    # interval total better - `barchart` is a real Grafana panel type but is not one this
    # was verified against, and an unverified panel type is exactly what this module
    # refuses to emit elsewhere.
    "interval": {"panel_type": "timeseries", "aggregation": "sum", "avoid": ()},
    # A running total that resets. The last value in a bucket is the total for it; a
    # mean or a median of a sawtooth is a number with no referent, and summing totals
    # double-counts.
    "counter": {"panel_type": "timeseries", "aggregation": "last", "avoid": ("mean", "median", "sum", "spread")},
    # A discrete code, flag or label. Nothing arithmetic applies - the mean of "locked"
    # and "unlocked" is not a state - so only first/last/count survive.
    "state": {
        "panel_type": "state-timeline",
        "aggregation": "last",
        "avoid": ("mean", "median", "sum", "min", "max", "spread", "stddev"),
    },
}

# A field whose kind the source never declared. `last` is the one aggregation that
# cannot be wrong for any kind, so it is what gets suggested - and `kind` is left out
# of the result entirely, so a caller can see that nothing was endorsed rather than
# reading a default as a recommendation. Deliberately not "gauge with mean": that
# would say averaging is safe about a field that might be a counter, which is the
# exact failure this module exists to prevent.
_UNDECLARED = {"panel_type": "timeseries", "aggregation": "last", "avoid": ()}

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def value_mappings(codes):
    """Render a coded field's meanings as Grafana value mappings.

    The shape verified against a real Grafana 13.2, which stored it unchanged:
    ``[{"type": "value", "options": {"1": {"text": "locked", "index": 0}}}]``. Keys are
    strings because that is what Grafana writes, and ``index`` fixes the legend order.

    :param codes: the field's ``{int: str}`` code map
    :return: the mappings list, or an empty list when there are no codes
    """
    if not codes:
        return []
    options = {str(code): {"text": label, "index": i} for i, (code, label) in enumerate(sorted(codes.items()))}
    return [{"type": "value", "options": options}]


def series_tags(schema):
    """Return the tag keys a panel should separate into their own series.

    Every tag the measurement carries *except* those the source already pins to one
    value: grouping by a tag that a WHERE clause has narrowed to a single value adds a
    series dimension with one member, which is noise. So a Zappi (``device`` pinned to
    its own label) groups by nothing, while Hue's ``host`` and Nuki's ``device`` - real
    axes with several members - are grouped.

    :param schema: the source's ReadSchema
    :return: sorted list of tag keys
    """
    return sorted(set(schema.tag_keys) - set(schema.tag_filters))


def _alias(field, tags):
    """Grafana's series-name expression for a panel.

    Always set, because without one Grafana names the series after the query: a panel
    with no alias came back as ``myenergi.last`` on a real Grafana 13.2, which is what a
    legend then shows. So a panel that separates producers is aliased ``$tag_<key>``,
    which interpolates that tag's value, and a panel with a single series falls back to
    the field name - a literal alias, verified to pass through unchanged.

    **Two or more tags is untested.** No source this project ships produces it - each
    has at most one unpinned tag - so the space-joined form below is the natural
    extension rather than an observed one, and is documented as such rather than left
    looking verified. It is reachable only by a measurement growing a second free tag.

    :param field: the field being charted, the fallback name for a single series
    :param tags: the tag keys being grouped by
    :return: the alias expression
    """
    if not tags:
        return field
    return " ".join(f"$tag_{tag}" for tag in tags)


def panel_spec(schema, field, tags):
    """Describe one field as a dashboard panel: its query, type, aggregation, unit and
    value mappings.

    :param schema: the source's ReadSchema
    :param field: the field key
    :param tags: tag keys to separate into series (see :func:`series_tags`)
    :return: the panel spec dict
    """
    meta = schema.metadata_for(field)
    kind = field_kind(meta, schema.field_types.get(field))
    plan = _KIND_PANELS.get(kind, _UNDECLARED)
    spec = {
        "field": field,
        "panel_type": plan["panel_type"],
        "aggregation": plan["aggregation"],
        "query": build_panel_query(schema, field, plan["aggregation"], tags),
    }
    if kind:
        spec["kind"] = kind
    if plan["avoid"]:
        spec["avoid_aggregations"] = list(plan["avoid"])
    unit = GRAFANA_UNITS.get(meta.get("unit"))
    if unit:
        spec["unit"] = unit
    mappings = value_mappings(meta.get("codes"))
    if mappings:
        spec["value_mappings"] = mappings
    spec["alias"] = _alias(field, tags)
    return spec


def suggest_panels_result(source, settings, settings_file, fields=None):
    """Build the suggest_dashboard_panels payload (runs in a worker thread).

    :param source: source name from a tool argument
    :param settings: parsed settings dict
    :param settings_file: settings path, threaded to the handler's own load
    :param fields: field keys to describe, or None for every recorded field
    :return: dict payload
    :raises ToolParamError: unknown source, or a field the source has not recorded
    :raises SourceConnectionError: the InfluxDB schema read failed
    """
    handler, schema = resolve_schema(source, settings, settings_file)
    try:
        wanted = sorted(schema.allowed_fields) if fields is None else list(fields)
        unknown = [name for name in wanted if name not in schema.allowed_fields]
        if unknown:
            raise ToolParamError(
                f"unknown field(s) {', '.join(repr(name) for name in unknown)} for source {source!r}; "
                f"available fields: {', '.join(sorted(schema.allowed_fields)) or '(none)'}"
            )
        tags = series_tags(schema)
        return {
            "source": source,
            "database": schema.db,
            "measurement": schema.measurement,
            "datasource_type": "influxdb",
            "series_tags": tags,
            "panels": [panel_spec(schema, name, tags) for name in wanted],
        }
    finally:
        close_session(handler.session)


def register_dashboard_tools(server, settings, settings_file=None):
    """Register the dashboard-suggestion tool on a MCPServer server.

    :param server: the MCPServer instance
    :param settings: the parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per call
    """
    import anyio

    @register_tool(server, title="Suggest Dashboard Panels", annotations=_READ_ONLY)
    async def suggest_dashboard_panels(source: str, fields: "list[str] | None" = None) -> dict:
        """Describe a source's fields as chart panels: per field an InfluxQL `query`,
        a `panel_type`, the `aggregation` to use, `avoid_aggregations`, a Grafana
        `unit`, `value_mappings` decoding a coded field to labels, and an `alias`
        naming each series after its tag.

        Use this when the goal is a chart or dashboard. Each `query` already carries
        the measurement, this source's disambiguating tags, `$timeFilter` and
        `time($__interval)`, so it drops into a panel target with `rawQuery` true.

        Read `aggregation` and `avoid_aggregations` together: averaging a counter that
        resets gives a plausible line that means nothing, and summing an instantaneous
        reading adds up quantities that never existed - no unit or value reveals either.

        These are panel *parts*, not a dashboard document. Assemble them into the shape
        your Grafana wants - copy an existing dashboard for the envelope and its
        datasource uid, neither of which this can know.

        `list_fields` is the schema behind this, when you want fields and not a chart;
        `query_history` runs a query here rather than handing you one.

        `unit` is a Grafana identifier, or its `suffix:` form where Grafana has none.
        `kind` and `avoid_aggregations` are absent where the source never said how a
        field may be aggregated - which is not permission to average it. Reads the
        field set live from InfluxDB and changes nothing; an unknown source or field
        is an error, and so is an unreachable InfluxDB.
        """
        return await anyio.to_thread.run_sync(suggest_panels_result, source, settings, settings_file, fields)
