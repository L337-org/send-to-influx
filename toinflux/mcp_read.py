"""InfluxDB read-query support for the MCP server.

The MCP server exposes each configured collector's history as a domain-aware
query tool, not a raw InfluxQL/Flux passthrough. This module owns the read
mechanics; the per-source domain knowledge (measurement name, disambiguating
tags, field units, coded-value meanings) lives on the ``DataHandler`` subclasses
themselves (the ``MCP_MEASUREMENT``/``MCP_TAG_FILTERS``/``MCP_FIELD_METADATA``
class attributes), so there is no parallel adapter hierarchy to keep in step with
the collectors.

Injection defence, in layers, because InfluxQL has no identifier parameter
binding:

* The measurement and disambiguating tags come from the source class's own
  static schema, never from model input.
* A requested field must exactly match a key the server itself discovered via
  ``SHOW FIELD KEYS`` against that measurement - the live field set is the
  allowlist, which also handles collectors whose field names are dynamic (Hue
  sensor names, per-lock Nuki prefixes).
* Every identifier that reaches a query is additionally validated against a
  strict charset and double-quoted with escaping.
* Time bounds are parsed in Python and re-emitted as RFC3339; the model's raw
  string never reaches the query.
* Aggregation is a fixed name->function map, and any GROUP BY interval is
  validated against a duration grammar.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import datetime
import logging
import re
from dataclasses import dataclass, field as dataclass_field


from mcp.types import ToolAnnotations

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.influx import (
    _get,
    build_edge_time_query,
    build_latest_query,
    discover_measurement_keys,
    discover_tag_values,
    resolve_db,
    run_query,
    single_series,
    _quote_identifier,
    _quote_string_literal,
    _validate_identifier,
)
from toinflux.general import INSTANCED_SOURCES, expand_sources, shares_measurement, render_values
from toinflux.mcp_common import (
    close_session,
    configured_sources,
    register_tool,
    resolve_handler,
    resolve_handlers,
)

# Every read tool is read-only and scoped to this server's own configured
# sources/measurements, never an open-ended external domain - shared so the six
# registrations below can't drift from each other on these two hints.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# User-facing aggregation name -> InfluxQL selector/aggregator function. "raw"
# is handled separately (no function, no GROUP BY) and is the default.
AGGREGATIONS = {
    "mean": "MEAN",
    "median": "MEDIAN",
    "min": "MIN",
    "max": "MAX",
    "sum": "SUM",
    "count": "COUNT",
    "first": "FIRST",
    "last": "LAST",
    "spread": "SPREAD",
    "stddev": "STDDEV",
}

# A relative time offset into the past, like "-24h", "-7d", "-90m". The leading
# "-" is required: the collectors only ever write points at the present time (even
# forecast values are stored at their collection time), so a future range has no
# data; an explicit ISO timestamp is still accepted for any future need.
_RELATIVE_TIME_RE = re.compile(r"^-\d+[smhdw]$")

# A GROUP BY interval duration like "5m", "1h", "1d".
_DURATION_RE = re.compile(r"^\d+[smhdw]$")

# Upper bound on points returned by a single query, so a broad range can't
# produce an unbounded response. Applied as a LIMIT; query_history's result
# reports the effective limit and whether the result was truncated by it, so the
# model can narrow the range or aggregate instead of silently seeing a partial view.
MAX_RESULT_POINTS = 5000
DEFAULT_RESULT_POINTS = 500

_RELATIVE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# How a field's value may legitimately be aggregated, which is not derivable from
# the value itself:
#
#   "gauge"    an instantaneous reading (or an average over one interval, like
#              Open-Meteo's radiation). Summing them adds up quantities that never
#              existed, so a sum is the one thing ruled out.
#   "interval" a quantity accumulated *during* its reporting interval - Octopus's
#              half-hourly consumption, Open-Meteo's precipitation. Summing is how a
#              total for a day or a week is obtained, which is what separates these
#              from a gauge and why they are not one.
#   "counter"  a running total that resets, where a mean produces a plausible number
#              that means nothing and only the last value or a difference between two
#              does.
#   "state"    a discrete code, flag or label, where nothing but first/last/count says
#              anything at all.
#
# Declared per field in a source's MCP_FIELD_METADATA and reported by list_fields.
#
# "interval" exists because three fields were previously declared gauges while their own
# descriptions said they were per-interval quantities, and the panel tool then advised
# callers away from `sum` - the correct aggregation for them. One vocabulary cannot both
# rule a sum out for a temperature and endorse it for a half-hour of consumption, so
# neither statement was true of "gauge" and it made the wrong one. The interval's
# *duration* is deliberately not part of the schema: a sum is right whatever it is, and
# it is observable anyway, since a point is stamped at its interval start so consecutive
# timestamps are spaced by it. It is also not uniformly knowable - gas granularity
# depends on the meter, and Open-Meteo's is the model's own.
FIELD_KINDS = frozenset({"gauge", "interval", "counter", "state"})

# An InfluxDB field type that can only be a state, whatever (if anything) the
# source declared: a string or a boolean has no arithmetic, so first/last/count is
# all there is. This is what lets a field carrying *no* static metadata still be
# aggregated safely, which matters because some field keys cannot be tabulated in
# advance - Hue's are the operator's own device names. A numeric field with nothing
# declared is deliberately left with no kind rather than assumed to be a gauge:
# "averaging this is fine" is exactly the wrong thing to say about a counter, and
# saying nothing is recoverable where saying that is not.
_STATE_INFLUX_TYPES = frozenset({"string", "boolean"})


@dataclass
class ReadSchema:
    """Everything the read layer needs to query one source, safely.

    ``measurement`` and ``tag_filters`` are the source class's static domain
    knowledge (never model input); ``allowed_fields`` is the live field set
    discovered from InfluxDB (the injection allowlist); ``field_metadata`` maps a
    field key - or a ``_``-delimited suffix, for collectors with dynamic prefixes
    like Nuki's per-lock fields - to a dict of annotations, **every key optional**:
    ``unit`` (str), ``codes`` (``{int: str}``), ``kind`` (one of
    :data:`FIELD_KINDS`) and ``description`` (str). A key is absent rather than
    empty where a source has nothing to say, so read it with ``.get()`` - a flag
    has no unit, and most fields need no description at all.

    Attributes:
        source (str): the source name this schema describes.
        measurement (str): the InfluxDB measurement it writes to.
        db (str): the database or bucket holding it.
        tag_filters (dict): tags that pick this source out of a shared measurement.
        allowed_fields (set): every field the measurement has recorded.
        field_metadata (dict): per-field unit, codes, kind and description.
        field_types (dict): per-field InfluxDB type, where the server reports one.
        tag_keys (set): the tag keys available to group by.
        instance_tag (str or None): the tag naming which instance produced a point.
        instance_values (set): the instance values actually recorded.
    """

    source: str
    measurement: str
    db: str
    tag_filters: dict = dataclass_field(default_factory=dict)
    allowed_fields: set = dataclass_field(default_factory=set)
    field_metadata: dict = dataclass_field(default_factory=dict)
    # ``field_types`` maps a discovered field key to its InfluxDB type ("float",
    # "integer", "string" or "boolean"), or to None where discovery could not say -
    # the key is always present, its value may not be. ``tag_keys`` holds every tag
    # the measurement carries. Both come from the same SHOW ... KEYS request that
    # produced ``allowed_fields``. Annotation only: ``allowed_fields`` remains the
    # single injection allowlist, so a schema built without them (a test, or a caller
    # that only needs the gate) simply reports no type and no dimensions.
    field_types: dict = dataclass_field(default_factory=dict)
    tag_keys: set = dataclass_field(default_factory=set)
    # The tag distinguishing producers within this measurement (from
    # MCP_INSTANCE_TAG), and the values it currently holds - the live allowlist for
    # an `instance` argument. None/empty for a source with a single producer, which
    # keeps every such source's behaviour and payload shape exactly as before.
    instance_tag: "str | None" = None
    instance_values: set = dataclass_field(default_factory=set)

    def metadata_for(self, field):
        """Return the metadata dict for a field in this schema.

        See the module-level :func:`metadata_for`.

        Args:
            field (str): the field key to look up

        Returns:
            dict: the field's metadata, empty when it has none
        """
        return metadata_for(self.field_metadata, field)


def build_schema(handler, discovered, db, instance_values=None):
    """Assemble a ReadSchema for a DataHandler instance.

    Combines its static class metadata, the live discovered keys, and the resolved db
    (see resolve_db).

    Note the field set comes from ``SHOW FIELD KEYS``, which is per-measurement,
    not per-tag. For the three MyEnergi devices that share the ``myenergi``
    measurement, that means each one's field list also shows the others' fields;
    a query for a field that belongs to a different device is still safe and
    simply returns no points (the device tag filter excludes it). Every other
    source owns its measurement, so this only affects the MyEnergi trio.

    Args:
        handler (DataHandler): a constructed DataHandler subclass instance
        discovered (MeasurementKeys): the measurement's keys, from discover_measurement_keys()
        db (str): the resolved database/bucket name (from resolve_db)
        instance_values (set or None): values of the source's instance tag found via
            discover_tag_values(), or None when it has no instance tag

    Returns:
        ReadSchema: the assembled schema for that handler
    """
    measurement = handler.MCP_MEASUREMENT or handler.source
    return ReadSchema(
        source=handler.source,
        measurement=measurement,
        db=db,
        tag_filters=handler.mcp_tag_filters(),
        allowed_fields=discovered.field_names,
        # The hook, not the class attribute: a source whose field keys are per-install
        # (Hue's are the operator's device names) resolves them here. Every other source
        # returns its declared table unchanged.
        field_metadata=handler.mcp_field_metadata(),
        field_types=dict(discovered.field_types),
        tag_keys=set(discovered.tag_keys),
        instance_tag=handler.MCP_INSTANCE_TAG,
        instance_values=set(instance_values or ()),
    )


def metadata_for(field_metadata, field):
    """Return the metadata dict for a field.

    An exact key match first, else the *longest* matching ``_``-delimited suffix (so
    ``Front_Door_stateValue`` picks up ``stateValue``, and a longer key wins over a
    shorter one it ends with - e.g. ``stateValue`` over ``value``). Empty dict when
    nothing matches.
    Longest-wins is deterministic regardless of dict order and stays correct as
    metadata grows.

    **The suffix match now serves history, not current writes.** It existed because Nuki
    prefixed every field key with its lock's name; since 5.3 each lock is a tag and the
    keys are bare, so an exact match covers everything being written today. It is kept
    deliberately rather than deleted: pre-migration Nuki points still carry prefixed keys
    until the migration's delete phase runs, so those fields remain queryable in the
    meantime - and this is what keeps them annotated with their units and decoded labels,
    which is exactly when an operator is deciding whether to migrate at all. Removable once
    no install can still hold pre-5.3 Nuki data, which is a condition rather than a date.

    Kept module-level (not only a ReadSchema method) so the live current-state
    path can annotate a source's raw ``get_data()`` fields straight from the
    handler's ``MCP_FIELD_METADATA``, without building an InfluxDB-backed schema.

    Args:
        field_metadata (dict): a source's ``MCP_FIELD_METADATA`` mapping
        field (str): the field key to look up

    Returns:
        dict: the field's metadata (``{"unit"...}``/``{"codes"...}``), or ``{}`` when it has none
    """
    if field in field_metadata:
        return field_metadata[field]
    best_key = None
    for key in field_metadata:
        if field.endswith(f"_{key}") and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return field_metadata[best_key] if best_key is not None else {}


def field_kind(meta, influx_type=None):
    """Return how a field may legitimately be aggregated, or None if unknown.

    A declared ``kind`` wins. Failing that, a coded field is a state by definition,
    and so is any string or boolean field - see ``_STATE_INFLUX_TYPES`` for why a
    *numeric* field with nothing declared is left unanswered instead of assumed to
    be a gauge.

    Args:
        meta (dict): the field's metadata dict (from :func:`metadata_for`)
        influx_type (str or None): the field's InfluxDB type, where discovery reported one

    Returns:
        str or None: one of ``FIELD_KINDS``, or None when the kind is not known
    """
    if meta.get("kind"):
        return meta["kind"]
    if meta.get("codes") or influx_type in _STATE_INFLUX_TYPES:
        return "state"
    return None


def _decode_code(value, codes):
    """Return the label for a coded value, or None.

    Only a genuine integer decodes - an int, or an integer-valued float (the
    collector writes every numeric field as a float, so a lock state arrives as
    1.0). A non-integer float (1.5) or a bool is never truncated to a code; it
    gets a null label rather than a wrong one.

    Args:
        value (object): the raw field value
        codes (dict or None): the field's ``{int: str}`` code map

    Returns:
        str or None: the decoded label, or None when there is no code for it
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return codes.get(value)
    if isinstance(value, float) and value.is_integer():
        return codes.get(int(value))
    return None


def annotate_rows(schema, field, columns, values):
    """Shape a query's (columns, values) into a domain-aware result dict.

    Adds the field's unit (if known) and, for coded fields (Nuki state), a
    decoded label alongside each raw numeric value - an undocumented code is
    passed through with a null label rather than dropped, matching the collector's
    raw-passthrough rule.

    Args:
        schema (ReadSchema): the source's schema
        field (str): the field the rows are for
        columns (list): the result's column names
        values (list): one list per row

    Returns:
        dict: ``{"field", "unit", "points": [{"time", "value"[, "label"]}], ...}``
    """
    meta = schema.metadata_for(field)
    codes = meta.get("codes") or {}
    time_index = columns.index("time") if "time" in columns else 0
    # The value column is whichever isn't "time" (raw queries) or the aggregate
    # column name (mean/max/...); fall back to the last column.
    value_index = next((i for i, c in enumerate(columns) if c != "time"), len(columns) - 1)
    points = []
    for row in values:
        value = row[value_index]
        point = {"time": row[time_index], "value": value}
        if codes:
            point["label"] = _decode_code(value, codes)
        points.append(point)
    result = {"source": schema.source, "field": field, "points": points}
    if meta.get("unit"):
        result["unit"] = meta["unit"]
    if codes:
        result["codes"] = {str(code): label for code, label in codes.items()}
    return result


def _annotate_state_field(field_metadata, name, value):
    """Shape one current-state field into ``{"value"[, "unit"][, "label"]}``.

    Reuses the same per-field metadata (unit, coded-value labels) as the history tool.
    An undocumented coded value passes through with a null label.

    Args:
        field_metadata (dict): the source's ``MCP_FIELD_METADATA``
        name (str): the field key (possibly device-prefixed)
        value (object): the field's current value

    Returns:
        dict: the annotated entry
    """
    meta = metadata_for(field_metadata, name)
    entry = {"value": value}
    if meta.get("unit"):
        entry["unit"] = meta["unit"]
    codes = meta.get("codes")
    if codes:
        entry["label"] = _decode_code(value, codes)
    return entry


def parse_time_bound(value, *, now=None):
    """Parse a user/model time bound into an aware UTC datetime.

    Accepts the literal ``now``, a relative offset (``-24h``, ``-7d``, ...), or
    an ISO 8601 / RFC 3339 timestamp. A naive timestamp is assumed UTC. Only the
    parsed value is ever re-emitted into a query, never the raw input string.

    Args:
        value (str or None): the time expression
        now (datetime.datetime or None): reference time for ``now``/relative offsets (defaults to the current UTC time);
            injected for testability

    Returns:
        datetime.datetime: the bound as a timezone-aware UTC datetime

    Raises:
        ToolParamError: if the value can't be parsed
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ToolParamError(f"invalid time value: {value!r}")
    text = value.strip()
    if text == "now":
        return now
    if _RELATIVE_TIME_RE.match(text):
        digits = text.lstrip("-")
        seconds = int(digits[:-1]) * _RELATIVE_UNIT_SECONDS[digits[-1]]
        return now - datetime.timedelta(seconds=seconds)
    # Accept a trailing Z (RFC 3339) that fromisoformat historically rejected.
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except ValueError:
        raise ToolParamError(
            f"invalid time value: {value!r} - use 'now', a relative offset like '-24h', " "or an ISO 8601 timestamp"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _rfc3339(dt):
    """Format an aware datetime as an RFC3339 string InfluxQL accepts.

    Args:
        dt (datetime.datetime): the moment to render

    Returns:
        str: the same moment as an RFC 3339 UTC timestamp
    """
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp_limit(limit):
    """Validate and clamp a requested point limit into [1, MAX_RESULT_POINTS].

    Args:
        limit (int or None): the requested row limit, or None for the default

    Returns:
        int: the limit, held between 1 and MAX_RESULT_POINTS

    Raises:
        ToolParamError: if the value isn't an integer
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ToolParamError(f"invalid limit {limit!r}") from None
    return max(1, min(value, MAX_RESULT_POINTS))


def build_query(
    schema, *, field, start, end, aggregation="raw", group_by=None, limit=DEFAULT_RESULT_POINTS, instance=None
):
    """Build a parameterised InfluxQL SELECT for a source's measurement.

    Every dynamic part is validated: the field against the schema's live
    allowlist, times parsed to RFC3339, aggregation against AGGREGATIONS, and any
    group_by against the duration grammar. Identifiers are charset-checked and
    double-quoted.

    Args:
        schema (ReadSchema): a ReadSchema (measurement, tag filters, allowed fields)
        field (str): the field key to query (must be in schema.allowed_fields)
        start (str or None): start time bound (see parse_time_bound)
        end (str or None): end time bound (see parse_time_bound)
        aggregation (str or None): one of AGGREGATIONS, or "raw" for un-aggregated points
        group_by (str or None): GROUP BY time interval (required when aggregating), e.g. "1h"
        limit (int or None): maximum points to return (clamped to MAX_RESULT_POINTS). When
            the query groups by the instance tag this is divided across the known instances,
            because InfluxDB applies LIMIT per series
        instance (str or None): restrict to one value of the source's instance tag; None
            leaves the query unscoped, which groups by that tag so producers stay
            distinguishable rather than being merged into one series

    Returns:
        str: the InfluxQL query

    Raises:
        ToolParamError: on any invalid parameter
    """
    if field not in schema.allowed_fields:
        raise ToolParamError(
            f"unknown field {field!r} for source {schema.source!r}; "
            f"available fields: {render_values(schema.allowed_fields, empty='(none)')}"
        )
    _validate_identifier(schema.measurement, "measurement")
    _validate_identifier(field, "field")

    # One reference time for both bounds, so a query with two relative bounds
    # (start='-24h', end='-1h') describes a self-consistent window rather than
    # measuring each end against a slightly different "now".
    now = datetime.datetime.now(datetime.timezone.utc)
    start_dt = parse_time_bound(start, now=now)
    end_dt = parse_time_bound(end, now=now)
    if start_dt >= end_dt:
        raise ToolParamError(f"start ({_rfc3339(start_dt)}) must be before end ({_rfc3339(end_dt)})")

    # Whether this query separates producers. Scoped to one instance it does not (the
    # WHERE clause narrows to a single series); unscoped on a source with an instance
    # axis it must, or InfluxQL merges every producer into one unlabelled series and
    # the answer silently mixes them - the defect this whole change exists to fix.
    group_by_instance = instance is None and bool(schema.instance_tag)
    instance_clause = ""
    if group_by_instance:
        _validate_identifier(schema.instance_tag, "tag")
        instance_clause = f", {_quote_identifier(schema.instance_tag)}"

    select_expr, group_clause = _select_and_group(field, aggregation, group_by, instance_clause)

    # LIMIT is applied *per series* once a query groups by a tag - verified against a
    # real InfluxDB 1.8, where LIMIT 2 across two hosts returned two rows each, not
    # two in total. Left alone, the result cap would quietly stop bounding anything:
    # N producers would multiply it. Divide it instead, so the caller's limit still
    # bounds the whole answer, and report the per-instance figure so the difference
    # is visible rather than silent.
    limit_value = _clamp_limit(limit)
    if group_by_instance:
        limit_value = max(1, limit_value // max(1, len(schema.instance_values)))

    where = [
        f"time >= {_quote_string_literal(_rfc3339(start_dt))}",
        f"time <= {_quote_string_literal(_rfc3339(end_dt))}",
    ]
    for tag_key, tag_value in sorted(schema.tag_filters.items()):
        _validate_identifier(tag_key, "tag")
        where.append(f"{_quote_identifier(tag_key)} = {_quote_string_literal(tag_value)}")
    if instance is not None:
        # Guarded here as well as in _validate_instance, because build_query is public and
        # reachable without it (the tests call it directly). Unguarded, a schema with no
        # axis reached _quote_identifier(None) and raised a bare AttributeError - which is
        # neither ToolParamError nor SourceConnectionError, so the MCP layer could not
        # tell a caller mistake from a transport failure. Same layering as the field
        # allowlist: validate at the boundary *and* where the value is interpolated.
        if not schema.instance_tag:
            raise ToolParamError(
                f"cannot scope source {schema.source!r} to {instance!r}: its measurement has a "
                f"single producer, so there is no tag to scope by"
            )
        _validate_identifier(schema.instance_tag, "tag")
        where.append(f"{_quote_identifier(schema.instance_tag)} = {_quote_string_literal(instance)}")

    return (
        f"SELECT {select_expr} FROM {_quote_identifier(schema.measurement)} "
        f"WHERE {' AND '.join(where)}{group_clause} "
        f"ORDER BY time DESC LIMIT {limit_value}"
    )


def _select_and_group(field, aggregation, group_by, instance_clause):
    """Return the SELECT expression and GROUP BY clause for a history query.

    Extracted from build_query to keep it within the project's complexity limit once
    instance grouping had to compose with time bucketing.

    Args:
        field (str): the field key to select; already validated against the live allowlist.
        aggregation (str): the aggregation name, looked up in the fixed map - never interpolated.
        group_by (str or None): the time bucket width, or None for raw points.
        instance_clause (str): the tag to group by, **already prefixed with a comma and a space** so it can be spliced
            straight after ``time(...)`` - literally ``, "host"`` - or an empty string when the query does not separate
            producers

    Returns:
        tuple: ``(select expression, group-by clause including its leading space)``

    Raises:
        ToolParamError: for an unknown aggregation, or a missing/malformed group_by
    """
    if aggregation == "raw":
        # A raw query still needs the tag in a GROUP BY to keep producers apart; there
        # is just no time bucket to combine it with.
        return _quote_identifier(field), f" GROUP BY{instance_clause[1:]}" if instance_clause else ""
    func = AGGREGATIONS.get(aggregation)
    if func is None:
        raise ToolParamError(f"unknown aggregation {aggregation!r}; choose one of: raw, {render_values(AGGREGATIONS)}")
    if not group_by:
        raise ToolParamError(f"aggregation {aggregation!r} requires a group_by interval (e.g. '1h')")
    if not _DURATION_RE.match(str(group_by)):
        raise ToolParamError(f"invalid group_by interval {group_by!r}; use a duration like '5m', '1h', '1d'")
    # Verified against a real InfluxDB 1.8: GROUP BY time(1h), "host" fill(none) is
    # valid and yields one series per host, each with its own buckets. Worth checking
    # rather than assuming, since the two grouping kinds compose here.
    return f"{func}({_quote_identifier(field)})", f" GROUP BY time({group_by}){instance_clause} fill(none)"


def build_panel_query(schema, field, aggregation, group_by_tags=()):
    """Build an InfluxQL SELECT for a *dashboard panel*.

    Uses Grafana's own macros for the time window and the bucket width.

    Deliberately separate from :func:`build_query` rather than a flag on it, because
    the two produce different things and mixing them would let one leak into the
    other. ``build_query`` runs here and now: it resolves concrete RFC3339 bounds and
    applies a LIMIT so a result cannot be unbounded. A panel query is never executed
    by this server at all - the panel supplies the window - so it carries ``$timeFilter``
    and ``time($__interval)`` in place of both, and no LIMIT, which would fight the
    panel's own ``maxDataPoints``. Verified against a real Grafana 13.2 and InfluxDB
    1.8 that both macros resolve and return data.

    The identifier handling is shared, which is the point of building it here rather
    than in the dashboard module: the measurement, field and tag keys go through the
    same charset validation and quoting as every other query, so the injection defence
    cannot drift between the two builders.

    Args:
        schema (ReadSchema): a ReadSchema (measurement, tag filters, allowed fields)
        field (str): the field key (must be in the schema's live allowlist)
        aggregation (str or None): one of AGGREGATIONS - never "raw", since a panel bucketed
            by ``$__interval`` always aggregates
        group_by_tags (list): tag keys to separate into their own series

    Returns:
        str: the InfluxQL query

    Raises:
        ToolParamError: for an unknown field or aggregation
    """
    if field not in schema.allowed_fields:
        raise ToolParamError(
            f"unknown field {field!r} for source {schema.source!r}; "
            f"available fields: {render_values(schema.allowed_fields, empty='(none)')}"
        )
    func = AGGREGATIONS.get(aggregation)
    if func is None:
        raise ToolParamError(f"unknown aggregation {aggregation!r}; choose one of: {render_values(AGGREGATIONS)}")
    _validate_identifier(schema.measurement, "measurement")
    _validate_identifier(field, "field")
    tags = ""
    for tag in group_by_tags:
        _validate_identifier(tag, "tag")
        tags += f", {_quote_identifier(tag)}"
    # $timeFilter is Grafana's placeholder for the panel's own window, so it is the
    # whole WHERE clause a panel needs beyond this source's disambiguating tags.
    where = ["$timeFilter"]
    for tag_key, tag_value in sorted(schema.tag_filters.items()):
        _validate_identifier(tag_key, "tag")
        where.append(f"{_quote_identifier(tag_key)} = {_quote_string_literal(tag_value)}")
    return (
        f"SELECT {func}({_quote_identifier(field)}) FROM {_quote_identifier(schema.measurement)} "
        f"WHERE {' AND '.join(where)} GROUP BY time($__interval){tags} fill(none)"
    )


_DURATION_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION_PART_RE = re.compile(r"(\d+)([wdhms])")
# The whole string must be unit/value pairs and nothing else. findall alone would accept a
# *prefix* - "720h junk" and "junk720h" both yielded 2592000 - turning a malformed value from
# some future InfluxDB into a confident retention figure reported as fact.
_DURATION_RE = re.compile(r"(?:\d+[wdhms])+")


def _cell(row, index, name):
    """Return a named column's value from an InfluxDB result row, or None.

    InfluxDB returns ``columns`` and ``values`` separately, and nothing guarantees every row
    is as long as the column list - a short row would make a bare ``row[index[name]]`` raise
    IndexError deep inside a read, instead of the "could not read that" the callers are
    written to expect. One reader for every row access in this module so the guard cannot be
    present at some sites and missing at others.

    Args:
        row (list): one row from a result series
        index (int): column name -> position mapping
        name (str): the column wanted

    Returns:
        object or None: the value, or None when the column is absent or the row is too short
    """
    position = index.get(name)
    if position is None or position >= len(row):
        return None
    return row[position]


def _influx_duration_seconds(duration):
    """Convert an InfluxDB duration string to whole seconds, or None if unparseable.

    v1 reports retention as e.g. ``720h0m0s``, ``1h0m0s`` or ``0s`` - concatenated
    unit/value pairs, not a single number - so the seconds equivalent is computed here to
    give callers something comparable with v2's ``everySeconds``. ``0s`` means keep
    forever, and is returned as 0 rather than None: that is a known answer, not a failure
    to parse, and the two must stay distinguishable.

    Args:
        duration (str or None): an InfluxDB duration string, or None

    Returns:
        int or None: whole seconds, or None when there is nothing parseable
    """
    if not isinstance(duration, str) or not _DURATION_RE.fullmatch(duration.strip()):
        return None
    parts = _DURATION_PART_RE.findall(duration.strip())
    return sum(int(value) * _DURATION_UNIT_SECONDS[unit] for value, unit in parts)


def _seconds_as_duration(seconds):
    """Render seconds in InfluxDB's own duration style, so v1 and v2 read alike.

    v2 reports retention in seconds; presenting it as ``720h0m0s`` alongside v1's identical
    string is what lets one answer be compared with another without the caller knowing
    which InfluxDB version produced it. Zero means keep forever and says so in words, since
    ``0s`` is easy to misread as "no data kept".

    Args:
        seconds (int or None): a whole number of seconds, or None

    Returns:
        str or None: a duration string, "infinite" for 0, or None when not a number
    """
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return None
    if seconds == 0:
        return "infinite"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h{minutes}m{secs}s"


def _influx_buckets_request(influx_settings, bucket):
    """Build (url, kwargs) for a GET of v2's ``/api/v2/buckets`` filtered to one bucket.

    Separate from :func:`_influx_read_request` because this is v2's *management* API, not
    the query endpoint - a different path, and the answer is JSON rather than an InfluxQL
    result set. The credential is the same one reads already use: querying a v2 bucket
    requires ``read:buckets`` on it, and that is the permission this endpoint wants, so a
    token that can query can read the bucket's retention. Verified against InfluxDB 2.7
    with a token scoped to read exactly one bucket.

    Args:
        influx_settings (dict): the ``influx`` settings block (must have a token)
        bucket (str): the bucket name to filter to

    Returns:
        tuple: ``(url, requests kwargs)``
    """
    params = {"name": bucket}
    # Pass org when configured, as the query path does: it disambiguates a token with
    # access to more than one org, and v2 answers 404 with a JSON error for an org that
    # does not exist, which the caller reports rather than mistaking for "no retention".
    if influx_settings.get("org"):
        params["org"] = influx_settings["org"]
    kwargs = {
        "headers": {"Authorization": f'Token {influx_settings["token"]}'},
        "params": params,
        "verify": not influx_settings.get("insecure", False),
        "timeout": influx_settings.get("timeout", 5),
    }
    return f'{influx_settings["url"]}/api/v2/buckets', kwargs


def _v1_retention(session, influx_settings, db):
    """Read v1's retention policy for a database via ``SHOW RETENTION POLICIES``.

    Prefers the policy flagged ``default``, since that is the one a write with no explicit
    policy lands in - which is every write this project makes.

    Args:
        session (requests.Session): the handler's session
        influx_settings (dict): the shared ``influx`` settings block
        db (str): the database or bucket to query

    Returns:
        dict: the retention, for the ``retention`` key of the payload

    Raises:
        SourceConnectionError: transport, parse, or an InfluxDB-reported error
    """
    _validate_identifier(db, "database")
    columns, values = single_series(
        run_query(session, influx_settings, db, f"SHOW RETENTION POLICIES ON {_quote_identifier(db)}")
    )
    if not values:
        raise SourceConnectionError(f"InfluxDB reported no retention policy for database {db!r}")
    index = {col: i for i, col in enumerate(columns)}
    rows = [row for row in values if _cell(row, index, "default")]
    row = (rows or values)[0]
    duration_seconds = _influx_duration_seconds(_cell(row, index, "duration"))
    shard_seconds = _influx_duration_seconds(_cell(row, index, "shardGroupDuration"))
    return {
        "known": True,
        "policy": _cell(row, index, "name"),
        # Rendered from the parsed seconds rather than passed through raw, so v1 and v2 cannot
        # disagree about how the same retention reads. v1 reports keep-forever as the literal
        # "0s" - easy to misread as "nothing is kept" - and a database created without an
        # explicit duration is exactly that case, so it is the common one, not a corner.
        "duration": _seconds_as_duration(duration_seconds) if duration_seconds is not None else None,
        "duration_seconds": duration_seconds,
        "shard_group_duration": _seconds_as_duration(shard_seconds) if shard_seconds is not None else None,
        "shard_group_duration_seconds": shard_seconds,
        "read_from": "v1 SHOW RETENTION POLICIES",
    }


def _v2_retention(session, influx_settings, bucket):
    """Read v2's retention rules for a bucket via ``/api/v2/buckets``.

    Deliberately *not* read through the v1-compatibility ``/query`` endpoint, even though
    ``SHOW RETENTION POLICIES`` succeeds there with the same credential. Verified against
    InfluxDB 2.7: for a bucket with 720h retention and a 24h shard group, that endpoint
    answers ``duration=0s`` and ``shardGroupDuration=168h0m0s`` - it reports the virtual
    DBRP mapping's own policy, not the bucket's. ``0s`` means "keep forever", so using it
    would tell an operator their data is never deleted when it expires in 30 days, and be
    wrong in the reassuring direction. The management API returns the real values.

    Args:
        session (requests.Session): the handler's session
        influx_settings (dict): the shared ``influx`` settings block
        bucket (str): the bucket whose retention is wanted

    Returns:
        dict: the retention, for the ``retention`` key of the payload

    Raises:
        SourceConnectionError: transport, parse, or no such bucket
    """
    url, kwargs = _influx_buckets_request(influx_settings, bucket)
    payload = _get(session, url, kwargs, f"read retention for bucket {bucket}")
    buckets = payload.get("buckets") or []
    if not buckets:
        # v2 answers 200 with an empty list for a name that matches nothing, so this is
        # not caught by raise_for_status.
        raise SourceConnectionError(f"InfluxDB has no bucket named {bucket!r}")
    rules = buckets[0].get("retentionRules") or []
    if not rules:
        # A bucket with no retention rules keeps data indefinitely - a real answer, not a
        # failure, and distinguishable from "could not find out" by known=True.
        return {"known": True, "duration_seconds": 0, "duration": "infinite", "read_from": "v2 /api/v2/buckets"}
    rule = rules[0]
    every = rule.get("everySeconds")
    shard = rule.get("shardGroupDurationSeconds")
    return {
        "known": True,
        "duration_seconds": every,
        "duration": _seconds_as_duration(every),
        "shard_group_duration_seconds": shard,
        "shard_group_duration": _seconds_as_duration(shard),
        "read_from": "v2 /api/v2/buckets",
    }


def _retention_for(session, influx_settings, db):
    """Read the retention configuration bounding how far back data could go.

    Degrades to an explicit "not known" rather than failing the whole call.

    Retention is the *second* half of the data-range answer and the less important one: if
    it cannot be read, the earliest/latest range is still worth returning. So a failure
    here is reported in place - ``known: false`` with the reason - rather than raised.
    Reported rather than omitted on purpose: a missing ``retention`` key reads as "no
    retention configured", i.e. kept forever, which is the same misleading direction as
    v2's ``0s``.

    Args:
        session (requests.Session): the handler's session
        influx_settings (dict): the shared ``influx`` settings block
        db (str): the database or bucket to query

    Returns:
        dict: the payload's ``retention`` key, always carrying a ``known`` flag
    """
    try:
        if influx_settings.get("token"):
            return _v2_retention(session, influx_settings, db)
        return _v1_retention(session, influx_settings, db)
    except SourceConnectionError as exc:
        logging.warning("Could not read retention configuration for %r: %s", db, exc)
        return {"known": False, "reason": str(exc)}


def configured_instances(source, settings):
    """Return the instance values configured for a source, or an empty list.

    The configured half of an instance allowlist. Uses ``expand_sources()`` - the same
    function the collectors use to decide what runs - so the read tools and the collectors
    cannot disagree about which targets exist. A source with no separate targets expands to
    a single ``None`` instance, which is not a value and is filtered out.

    Args:
        source (str): source name (already validated as configured)
        settings (dict): parsed settings dict

    Returns:
        list: the configured instance values, empty for a single-target source
    """
    if source.lower() not in INSTANCED_SOURCES:
        return []
    return [instance for _, instance in expand_sources([source.lower()], settings) if instance is not None]


def resolve_schema(source, settings, settings_file, instance=None):
    """Build a fully-populated ReadSchema for a source.

    Its static class metadata plus the live field allowlist discovered from InfluxDB.
    Constructs a handler from current settings each call, so a live settings edit is
    picked up.

    ``instance`` scopes the schema to one target of an instanced source, which shows up as
    an extra tag filter (a Hue bridge's ``host``) - see ``DataHandler.mcp_tag_filters()``.
    ``None`` leaves the read unscoped, spanning every target, which is the right answer when
    the caller did not name one.

    Args:
        source (str): the configured source name.
        settings (dict): the parsed settings document.
        settings_file (str or None): the settings path, for constructing the handler.
        instance (str or None): the instance to scope to, or None for all of them

    Returns:
        tuple: ``(handler, schema)`` - the caller closes the handler's session

    Raises:
        ToolParamError: for an unknown/unusable source, or an instance that is not configured
        SourceConnectionError: if field discovery fails
    """
    handler = resolve_handler(source, settings, settings_file, instance=instance)
    measurement = handler.MCP_MEASUREMENT or handler.source
    # Use the handler's own freshly-loaded influx block, not the server's startup
    # snapshot - the handler was constructed from current settings, so an edit to
    # influx.url/credentials mid-run is honoured (matching the per-source config
    # this call already picks up), and discovery/query can't disagree about which
    # InfluxDB they target. resolve_db() mirrors the write path's v1/v2 db choice
    # so reads hit the same database the collectors write to.
    influx_settings = handler.settings["influx"]
    db = resolve_db(handler.source_settings, influx_settings)
    try:
        keys = discover_measurement_keys(handler.session, influx_settings, db, measurement)
        # Only for a source that declares an instance axis, so nothing else pays for
        # an extra round trip. Discovered rather than configured: the values are
        # whatever has actually been written, so a new collector host is queryable
        # without touching this install's settings.
        instance_values = None
        if handler.MCP_INSTANCE_TAG:
            configured = set(configured_instances(source, settings))
            if shares_measurement(source):
                # Configured only. Where several sources write to one measurement - the three
                # MyEnergi types, told apart by the same `device` tag that now carries the
                # operator's label - a value discovered in the data cannot be attributed to
                # one of them, so a zappi query would otherwise accept an eddi's label. The
                # config does distinguish them, being separate blocks and separate sources,
                # so it is the authority here. Discovery is skipped rather than filtered
                # afterwards, since its answer could not be attributed anyway.
                #
                # Consequence worth knowing: a decommissioned MyEnergi device's history stops
                # being reachable by label, where a decommissioned Hue bridge's does not. The
                # asymmetry is real and follows from Hue owning its measurement outright.
                instance_values = configured
            else:
                # The union matters in both directions. Discovered alone would refuse a
                # bridge that is configured but has not collected yet - which Hue's
                # predecessor `bridge` parameter accepted - and would leave query_history
                # disagreeing with get_current_state, which reads live from whatever is
                # configured. Configured alone would lose a decommissioned bridge's history.
                instance_values = (
                    discover_tag_values(handler.session, influx_settings, db, measurement, handler.MCP_INSTANCE_TAG)
                    | configured
                )
        # Inside the try on purpose. build_schema() resolves the handler's own device
        # (MyEnergi's mcp_tag_filters() calls device()), so it can raise ConfigError - and when it
        # did, from the return line outside this block, the caller never received the handler and
        # nothing ever closed its session. One leaked connection pool per call, on the path a
        # misconfigured source retries.
        schema = build_schema(handler, keys, db, instance_values)
    except Exception:
        # A fresh requests.Session is created per handler (per tool call); close
        # it if discovery fails, or a long-running server accumulates open
        # connection pools/FDs on intermittent errors. On success the caller owns
        # the returned handler and closes its session when done.
        close_session(handler.session)
        raise
    return handler, schema


def _list_sources_result(settings, settings_file):
    """Build the list_sources tool payload (runs in a worker thread).

    Args:
        settings (dict): parsed settings dict
        settings_file (str or None): settings path, threaded to the handler's own load

    Returns:
        dict: the ``list_sources`` payload
    """
    out = []
    for source in configured_sources(settings):
        try:
            handler = resolve_handler(source, settings, settings_file)
        except ToolParamError:
            continue
        # Constructed only to read class metadata; close its session immediately.
        try:
            entry = {
                "source": source,
                "measurement": handler.MCP_MEASUREMENT or handler.source,
                "description": handler.MCP_DESCRIPTION,
            }
            # The tag name only - static class metadata, so this stays a no-InfluxDB
            # call. Deliberately not the values: enumerating them means a query per
            # source, and the entry point for reads should not become the most expensive
            # tool on the server. list_fields carries them, and its description says so.
            if handler.MCP_INSTANCE_TAG:
                entry["instance_tag"] = handler.MCP_INSTANCE_TAG
            out.append(entry)
        finally:
            close_session(handler.session)
    return {"sources": out}


def _field_entry(schema, name, detail=False):
    """Describe one field of a schema.

    Its name, type, unit, coded values, kind and, on request, its description.

    Every key but ``field`` is omitted when there is nothing to say, so a caller
    can tell "no unit" from "unit unknown" the only way that is honest - by the key
    not being there at all.

    Args:
        schema (ReadSchema): the source's ReadSchema
        name (str): the field key
        detail (bool): include the prose description, where the field has one

    Returns:
        dict: the field's entry for the payload
    """
    meta = schema.metadata_for(name)
    entry = {"field": name}
    influx_type = schema.field_types.get(name)
    if influx_type:
        entry["type"] = influx_type
    if meta.get("unit"):
        entry["unit"] = meta["unit"]
    kind = field_kind(meta, influx_type)
    if kind:
        entry["kind"] = kind
    if meta.get("codes"):
        entry["codes"] = {str(code): label for code, label in meta["codes"].items()}
    if detail and meta.get("description"):
        entry["description"] = meta["description"]
    return entry


def list_fields_result(source, settings, settings_file, detail=False):
    """Build the list_fields tool payload (runs in a worker thread).

    Everything needed to construct a query is in one payload: the database, the
    measurement, every field with its type/unit/coded values/kind, and the tag keys
    that are available to group by. Only the per-field prose is optional, being the
    one bulky part - see the ``detail`` flag, which adds it to this same call rather
    than to a second one.

    Args:
        source (str): the configured source name whose fields to list.
        settings (dict): the parsed settings document.
        settings_file (str): the settings path, for re-resolving the handler.
        detail (bool): include each field's description where it has one

    Returns:
        dict: the list_fields payload - the source's database, measurement, tag keys and
            one entry per field
    """
    handler, schema = resolve_schema(source, settings, settings_file)
    try:
        fields = [_field_entry(schema, name, detail=detail) for name in sorted(schema.allowed_fields)]
        result = {
            "source": source,
            "measurement": schema.measurement,
            # The db/bucket a query has to name. Previously only get_data_range
            # reported it, so building a query meant a second call that also did
            # retention work for one short string.
            "database": schema.db,
            "fields": fields,
            # Every dimension the measurement can be grouped by, not just the one a
            # source declares as its instance axis - a MyEnergi device or a Nuki lock
            # was in the data and in no schema a caller could see.
            "tag_keys": sorted(schema.tag_keys),
        }
        if schema.instance_tag:
            # Reported here rather than in list_sources because this call already makes
            # an InfluxDB round trip: the values are live, so listing them costs nothing
            # extra here and would cost a query per source there. list_sources names the
            # tag so a caller knows to come and get them.
            result["instance_tag"] = schema.instance_tag
            result["instances"] = sorted(schema.instance_values)
        return result
    finally:
        close_session(handler.session)


def _validate_instance(schema, instance) -> None:
    """Check an ``instance`` argument against the source's live tag values.

    Two separate refusals, and both matter. A source with no instance axis is told so
    outright rather than having the value ignored - accepting it, running an unscoped
    query and echoing it back would tell the caller the answer was narrowed when it was
    not, the same dishonesty Hue's predecessor ``bridge`` guard existed to prevent. A value the tag has
    never held is refused with the known values listed, because the alternative is a
    confidently empty result that reads as "no data" rather than "no such producer".

    The allowlist is the live discovered set, which is also what keeps the value safe to
    interpolate - the same layering as a queried field name.

    Args:
        schema (ReadSchema): the source's schema
        instance (str or None): the instance the caller asked for

    Raises:
        ToolParamError: if the source has no axis, or the value is not one of its discovered values
    """
    if instance is None:
        return
    if not schema.instance_tag:
        raise ToolParamError(
            f"'instance' does not apply to source {schema.source!r} - its measurement has a single "
            f"producer, so there is nothing to scope to"
        )
    if instance not in schema.instance_values:
        # "accepted", not "recorded": the allowlist is the union of what is present in the
        # data and what is configured, so a configured target that has not collected yet is
        # in this list without ever having been recorded. Calling it recorded would state
        # something untrue about the very value being offered as an alternative.
        known = ", ".join(sorted(schema.instance_values)) or "(none configured or recorded yet)"
        raise ToolParamError(
            f"unknown {schema.instance_tag} {instance!r} for source {schema.source!r}; " f"accepted values: {known}"
        )


def _query_history_result(
    settings, settings_file, *, source, field, start, end, aggregation, group_by, limit, instance=None
):
    """Build the query_history tool payload (runs in a worker thread).

    ``instance`` names a value of the source's instance tag, which for Hue is the bridge's
    ``host``. Hue's older ``bridge`` parameter is gone rather than deprecated: an MCP client
    fetches the tool schema at session start, so there is no persisted caller to keep
    compatible, and a second name for one concept would cost context on every session to
    serve a window shorter than one conversation.

    Scoping happens in one place, ``build_query``, via the shared instance mechanism.
    Previously ``bridge`` took a different route - ``resolve_schema(instance=...)``, which
    added the tag through ``Hue.mcp_tag_filters()`` - so the same idea had two
    implementations that could drift. The handler is now resolved unscoped and the filter
    applied at the query, which is also why the Hue-specific branch here is gone.

    Args:
        settings (dict): parsed settings dict
        settings_file (str or None): settings path, threaded to the handler's own load
        source (str): the source to query
        field (str): the field to return
        start (str or None): inclusive lower time bound
        end (str or None): exclusive upper time bound
        aggregation (str or None): how to aggregate, or None for raw points
        group_by (str or None): the interval to group into
        limit (int or None): the maximum rows to return
        instance (str or None): which instance to scope to

    Returns:
        dict: the ``query_history`` payload
    """
    handler, schema = resolve_schema(source, settings, settings_file)
    try:
        # `instance` names a value of a tag in the *data*, which is not the same question as
        # resolve_schema's own `instance` (a collector work unit in INSTANCED_SOURCES - a
        # bridge with its own credentials and worker). A read from InfluxDB needs no
        # credentials, so the handler is resolved unscoped and the scoping is a query
        # predicate. The two concepts stay distinct underneath; callers see one parameter.
        _validate_instance(schema, instance)
        result = _run_query_history(handler, schema, field, start, end, aggregation, group_by, limit, instance=instance)
        # Say what was actually queried: without this the model cannot tell a single-producer
        # answer from an estate-wide one, and the two mean different things.
        if instance is not None:
            result["instance"] = instance
            result["instance_tag"] = schema.instance_tag
        return result
    finally:
        close_session(handler.session)


def _run_query_history(handler, schema, field, start, end, aggregation, group_by, limit, instance=None):
    """Execute the query and shape the payload.

    The session lifecycle is owned by the caller. Split out so _query_history_result's
    ``finally`` stays a thin wrapper.

    Two payload shapes, and which one you get depends on the *source*, never on how
    many producers it happens to have:

    - A source with no instance axis, or a query scoped to one instance, returns flat
      ``points`` - byte-identical to before this existed.
    - An unscoped query on a source with an axis returns ``instances``, keyed by tag
      value. Keyed even when only one value exists, so nothing reading the payload
      depends on the producer count - the same reasoning as Hue's per-bridge map.

    Never a merged series: two hosts' ping interleaved in one unlabelled list is not a
    partial answer, it is a wrong one.

    Args:
        handler (DataHandler): a constructed handler for the source
        schema (ReadSchema): the source's schema
        field (str): the field to return
        start (str or None): inclusive lower time bound
        end (str or None): exclusive upper time bound
        aggregation (str or None): how to aggregate, or None for raw points
        group_by (str or None): the interval to group into
        limit (int or None): the maximum rows to return
        instance (str or None): which instance to scope to

    Returns:
        dict: the ``query_history`` payload
    """
    # handler.settings["influx"], not the startup snapshot, so the query runs
    # against the same (possibly freshly-edited) InfluxDB the schema was
    # discovered from - see resolve_schema.
    query = build_query(
        schema,
        field=field,
        start=start,
        end=end,
        aggregation=aggregation,
        group_by=group_by,
        limit=limit,
        instance=instance,
    )
    series = run_query(handler.session, handler.settings["influx"], schema.db, query)
    grouped = instance is None and bool(schema.instance_tag)
    # Mirrors build_query's division exactly: InfluxDB applies LIMIT per series once a
    # query groups by a tag, so the figure reported has to be the one actually in force
    # or `truncated` would be measured against a limit that was never applied.
    effective_limit = _clamp_limit(limit)
    if grouped:
        effective_limit = max(1, effective_limit // max(1, len(schema.instance_values)))

    if not grouped:
        columns, values = single_series(series)
        result = annotate_rows(schema, field, columns, values)
        # `truncated` means the result reached the limit, so more data *may* exist
        # beyond it - if exactly `limit` points exist, nothing more does. A prompt to
        # narrow the range or aggregate, not a guarantee of omitted data.
        result["limit"] = effective_limit
        result["truncated"] = len(result["points"]) >= effective_limit
        return result

    instances = {}
    unit, codes = None, None
    for one in series:
        annotated = annotate_rows(schema, field, one.columns, one.values)
        unit = unit or annotated.get("unit")
        codes = codes or annotated.get("codes")
        key = one.tags.get(schema.instance_tag)
        if key is None:
            # A grouped query always tags its series, so this would mean InfluxDB
            # answered a shape we did not ask for. Skipping it silently is how the
            # original defect looked; name it instead.
            logging.warning(
                "Query for %r grouped by %r returned a series with no %s tag; ignoring it",
                schema.source,
                schema.instance_tag,
                schema.instance_tag,
            )
            continue
        # Only report producers this source owns, and only *after* the untagged case above -
        # filtering first would silently drop an untagged series, which is the very thing that
        # warning exists to prevent. A grouped query on a shared measurement returns every
        # source's producers, the three MyEnergi types sharing `myenergi`, so without this a
        # zappi query would answer with the eddi and harvi devices too. For a source that owns
        # its measurement the allowlist already holds the discovered values, so nothing is
        # filtered and behaviour is unchanged. One rule serves both.
        #
        # Deliberately not guarded on the allowlist being non-empty. An empty allowlist means
        # this source owns nothing in the measurement, so the honest answer is nothing -
        # skipping the filter there reported *every* producer instead, which for a shared
        # measurement means answering a zappi question with the eddi and harvi devices.
        # Reproduced before fixing; maximally wrong rather than merely incomplete.
        if key not in schema.instance_values:
            continue
        instances[key] = {
            "points": annotated["points"],
            "truncated": len(annotated["points"]) >= effective_limit,
        }
    result = {
        "source": schema.source,
        "field": field,
        "instance_tag": schema.instance_tag,
        "instances": instances,
        # Named rather than plain `limit`: it is per instance here, not a total, and a
        # caller comparing it against the number it passed would otherwise be misled.
        "limit_per_instance": effective_limit,
        "truncated": any(entry["truncated"] for entry in instances.values()),
    }
    if unit:
        result["unit"] = unit
    if codes:
        result["codes"] = codes
    return result


def _latest_recorded(handler):
    """Read the most recent recorded point for a non-live source from InfluxDB.

    Args:
        handler (DataHandler): a constructed DataHandler (caller owns its session)

    Returns:
        tuple: ``({field: value}, as_of)`` with as_of in unix seconds or None; empty dict when nothing is recorded
    """
    influx_settings = handler.settings["influx"]
    db = resolve_db(handler.source_settings, influx_settings)
    measurement = handler.MCP_MEASUREMENT or handler.source
    fields = discover_measurement_keys(handler.session, influx_settings, db, measurement).field_names
    if not fields:
        return {}, None
    query = build_latest_query(measurement, handler.mcp_tag_filters(), fields)
    columns, values = single_series(run_query(handler.session, influx_settings, db, query))
    return _row_to_state(fields, columns, values)


def _row_to_state(fields, columns, values):
    """Turn a single-point result row into ``({field: value}, as_of)``.

    Args:
        fields (set): the field names wanted
        columns (list): the result's column names
        values (list): the single result row

    Returns:
        tuple: ``({field: value}, as_of)``, or ``({}, None)`` when there is no row
    """
    if not values:
        return {}, None
    row = values[0]
    index = {col: i for i, col in enumerate(columns)}
    # Skip a field that came back NULL (a field written in some points but not the
    # latest one) rather than reporting a meaningless None as its current value.
    data = {name: _cell(row, index, name) for name in sorted(fields) if _cell(row, index, name) is not None}
    return data, _cell(row, index, "time")


def _latest_recorded_per_instance(handler):
    """Read the latest recorded point *per producer*.

    For a non-live source with an instance axis.

    Speedtest is the case: several hosts write to one measurement, so a single
    ungrouped "latest point" answers with whichever host happened to write most
    recently and says nothing about which - a plausible-looking answer that is simply
    the wrong question. Grouping by the tag gets every host's own latest in one round
    trip, because InfluxDB applies LIMIT 1 per series once grouped.

    Args:
        handler (DataHandler): a constructed DataHandler whose MCP_INSTANCE_TAG is set

    Returns:
        dict: ``{tag value: (fields, as_of)}``, empty when nothing is recorded yet

    Raises:
        SourceConnectionError: on a transport/parse failure
    """
    influx_settings = handler.settings["influx"]
    db = resolve_db(handler.source_settings, influx_settings)
    measurement = handler.MCP_MEASUREMENT or handler.source
    fields = discover_measurement_keys(handler.session, influx_settings, db, measurement).field_names
    if not fields:
        return {}
    tag = handler.MCP_INSTANCE_TAG
    query = build_latest_query(measurement, handler.mcp_tag_filters(), fields, group_by_tag=tag)
    out = {}
    for one in run_query(handler.session, influx_settings, db, query):
        key = one.tags.get(tag)
        if key is None:
            logging.warning(
                "Latest-state query for %r grouped by %r returned an untagged series; ignoring it",
                handler.source,
                tag,
            )
            continue
        out[key] = _row_to_state(fields, one.columns, one.values)
    return out


def current_state_result(source, settings, settings_file):
    """Build the get_current_state payload (runs in a worker thread).

    A live source (MCP_LIVE_STATE, the default) reports from its own get_data() -
    a cheap API/MQTT read of the device's current state. A non-live source
    (Speedtest, Octopus) reports the latest recorded point from InfluxDB and never
    calls get_data(). Every field carries the same unit/decoded-label annotation
    as the history tool, so a coded value reads back as its label ("locked"), not
    a bare number.

    A source with several instances (a Hue install with more than one bridge) reports each
    one separately, under ``instances`` keyed by instance, because two bridges can carry
    the same field name - one "Kitchen" per floor - and a single flat map would silently
    lose one of them. The grouping is used whenever the source is instanced, even with a
    single bridge, so nothing reading the payload depends on how many are configured.

    A failing instance does not suppress the others: its entry carries ``error`` while the
    rest carry ``fields``, so a partial answer arrives *with* its failure status. Only when
    every instance fails is a ``SourceConnectionError`` raised, since then there is nothing
    useful to return.

    Args:
        source (str): the source to read
        settings (dict): parsed settings dict
        settings_file (str or None): settings path, threaded to the handler's own load

    Returns:
        dict: the ``get_current_state`` payload

    Raises:
        ToolParamError: unknown/unusable source
        SourceConnectionError: a live get_data() or InfluxDB read failed for every instance
    """
    handlers = resolve_handlers(source, settings, settings_file)
    try:
        first = handlers[0][1]
        result = {"source": first.source, "state": "live" if first.MCP_LIVE_STATE else "last_recorded"}
        if first.MCP_DESCRIPTION:
            result["description"] = first.MCP_DESCRIPTION

        if len(handlers) == 1 and handlers[0][0] is None:
            # A source with a data axis but a single collector work unit - Speedtest,
            # where each host is its own process, so the producers exist in InfluxDB
            # rather than in this install's config. Only answerable per producer when
            # the state comes *from* InfluxDB: a live read reflects this machine alone
            # and could not speak for the others, so a live source keeps the flat shape.
            if first.MCP_INSTANCE_TAG and first.MCP_LIVE_STATE_COVERS_ALL_INSTANCES:
                # One live read covers every producer - Nuki, whose locks all arrive over a
                # single MQTT subscription. get_data() already returns {instance: {field:
                # value}}, so no InfluxDB read is needed and the answer is genuinely live for
                # every lock rather than only the handler's own.
                as_of = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                result["instance_tag"] = first.MCP_INSTANCE_TAG
                result["instances"] = {
                    key: {"fields": _annotate_state(first, fields), "as_of": as_of}
                    for key, fields in sorted((first.get_data() or {}).items())
                }
                return result
            if first.MCP_INSTANCE_TAG and not first.MCP_LIVE_STATE:
                per_instance = _latest_recorded_per_instance(first)
                result["instance_tag"] = first.MCP_INSTANCE_TAG
                result["instances"] = {
                    key: {"fields": _annotate_state(first, fields), "as_of": as_of}
                    for key, (fields, as_of) in sorted(per_instance.items())
                }
                return result
            # Single-target source: the historical flat shape, unchanged.
            fields, as_of = _instance_state(first)
            result["fields"] = fields
            result["as_of"] = as_of
            return result

        instances, failures = {}, 0
        for instance, handler in handlers:
            try:
                fields, as_of = _instance_state(handler)
                instances[instance] = {"fields": fields, "as_of": as_of}
            except SourceConnectionError as exc:
                logging.warning("Could not read current state for %s: %s", handler.worker_label, exc)
                instances[instance] = {"error": str(exc)}
                failures += 1
        if failures == len(handlers):
            raise SourceConnectionError(
                f"could not read current state for any configured target of {source!r}: "
                # instance!r for the same reason as mcp_write's unreachable list.
                + "; ".join(f"{instance!r}: {entry['error']}" for instance, entry in instances.items())
            )
        result["instances"] = instances
        return result
    finally:
        for _, handler in handlers:
            close_session(handler.session)


def _instance_state(handler):
    """Read one handler's current state as ``(annotated fields, as_of)``.

    Live sources report from their own ``get_data()``; a non-live source (Speedtest,
    Octopus) reads the latest recorded point instead and never calls ``get_data()``.

    Args:
        handler (DataHandler): a constructed DataHandler subclass instance

    Returns:
        tuple: (field name -> annotated value, unix seconds the state is as of)

    Raises:
        SourceConnectionError: the live read or the InfluxDB read failed
    """
    if handler.MCP_LIVE_STATE:
        data = handler.get_data() or {}
        as_of = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    else:
        data, as_of = _latest_recorded(handler)
    return _annotate_state(handler, data), as_of


def _annotate_state(handler, data):
    """Annotate a ``{field: value}`` map with units and decoded labels.

    Shared by the flat and the per-instance current-state paths so a coded value reads
    back as its label in both - a second copy would eventually annotate one and not the
    other.

    Args:
        handler (DataHandler): the source's DataHandler, for its field metadata
        data (dict): raw field name -> value

    Returns:
        dict: field name to annotated value
    """
    # The hook, so a live current-state read is annotated with the same units list_fields
    # reports. For Hue that costs an InfluxDB lookup on a call that has just read the
    # bridge; the override degrades to nothing if InfluxDB is unreachable, so a device
    # read never fails because the annotation could not be resolved.
    field_metadata = handler.mcp_field_metadata()
    return {name: _annotate_state_field(field_metadata, name, value) for name, value in sorted(data.items())}


def _edge_time(handler, schema, order_query):
    """Return the unix-seconds timestamp of one edge point, or None when there is no data.

    None covers every way the timestamp can be absent rather than wrong: no points matched,
    or the row came back without a usable ``time`` column (see :func:`_cell`). An InfluxDB
    transport failure or a server-side error still raises, because that is not the same thing
    as "there is no data" and must not be reported as an empty range.

    Args:
        handler (DataHandler): constructed DataHandler (caller owns its session)
        schema (ReadSchema): the ReadSchema for the source
        order_query (str): the built query (see :func:`build_edge_time_query`)

    Returns:
        int or None: unix seconds, or None when there is no usable time

    Raises:
        SourceConnectionError: transport failure, unparseable response, or an InfluxDB-reported query error
    """
    columns, values = single_series(run_query(handler.session, handler.settings["influx"], schema.db, order_query))
    if not values:
        return None
    index = {col: i for i, col in enumerate(columns)}
    return _cell(values[0], index, "time")


def _edge_times_per_instance(handler, schema, order):
    """Return ``{tag value: unix seconds}`` for one edge, per producer.

    An estate-wide range hides exactly what matters when producers differ: a host added
    last week and one collecting for a year report the same span if they are merged, so
    "how far back does this go" gets an answer that is true of the measurement and false
    of every host in it.

    Args:
        handler (DataHandler): the source's DataHandler, for the session and settings.
        schema (ReadSchema): the source's resolved ReadSchema.
        order (str): ``"ASC"`` for each producer's oldest point, ``"DESC"`` for its newest

    Returns:
        dict: tag value to unix seconds, omitting a producer with no usable time

    Raises:
        SourceConnectionError: transport failure or an InfluxDB-reported query error
    """
    query = build_edge_time_query(schema.measurement, schema.tag_filters, order, group_by_tag=schema.instance_tag)
    out = {}
    for one in run_query(handler.session, handler.settings["influx"], schema.db, query):
        key = one.tags.get(schema.instance_tag)
        if key is None or not one.values:
            continue
        index = {col: i for i, col in enumerate(one.columns)}
        stamp = _cell(one.values[0], index, "time")
        if stamp is not None:
            out[key] = stamp
    return out


def data_range_result(source, settings, settings_file):
    """Build the get_data_range payload (runs in a worker thread).

    Answers "how far back does this go", which none of the other read tools can: they
    either need a range already known (``query_history``) or describe only the present
    (``get_current_state``). Two separate facts, deliberately both reported:

    * The **actual** range - the oldest and newest points present. That is the floor on
      what any history query can return, whatever retention permits in principle, and it
      reflects both when collection started and where older data has aged out.
    * The **configured** retention bounding how far back data could ever go, independent
      of collection history. A three-year-old install with 30-day retention has three
      years of history and 30 days of data; only reporting both distinguishes that from an
      install that started last month.

    For an instanced source (a Hue install with more than one bridge) the range covers
    every bridge, since they share one measurement - matching ``query_history``'s
    unqualified default rather than inventing a per-bridge answer here.

    A source with an instance *axis* additionally reports per producer under
    ``instances``. Merging them would answer a question nobody asked: a host added last
    week and one collecting for a year share a span that is true of the measurement and
    false of both. The overall figures stay alongside, because retention bounds the
    database rather than any one producer and the two are read together.

    Args:
        source (str): source name from a tool argument
        settings (dict): parsed settings dict
        settings_file (str or None): settings path, threaded to the handler's own load

    Returns:
        dict: the ``get_data_range`` payload

    Raises:
        ToolParamError: unknown or unusable source
        SourceConnectionError: the InfluxDB range read failed (retention failure alone degrades to ``retention.known =
            false`` instead)
    """
    handler, schema = resolve_schema(source, settings, settings_file)
    try:
        result = {"source": schema.source, "measurement": schema.measurement, "database": schema.db}
        if handler.MCP_DESCRIPTION:
            result["description"] = handler.MCP_DESCRIPTION

        if not schema.allowed_fields:
            # No fields discovered means nothing has ever been written for this
            # measurement. Report that plainly rather than as a failure - a source
            # configured today legitimately has no data yet - but still report retention,
            # which is configured independently of whether anything was collected.
            result.update({"earliest": None, "latest": None, "span_seconds": None, "points_present": False})
        else:
            if schema.instance_tag:
                # Per producer as well as overall. The overall figures stay, because
                # retention is a property of the database rather than of any one producer
                # and the two answers are read together.
                first = _edge_times_per_instance(handler, schema, "ASC")
                last = _edge_times_per_instance(handler, schema, "DESC")
                result["instance_tag"] = schema.instance_tag
                result["instances"] = {
                    key: {
                        "earliest": first.get(key),
                        "latest": last.get(key),
                        "span_seconds": (
                            last[key] - first[key]
                            if isinstance(first.get(key), int) and isinstance(last.get(key), int)
                            else None
                        ),
                    }
                    for key in sorted(set(first) | set(last))
                }
            earliest = _edge_time(handler, schema, build_edge_time_query(schema.measurement, schema.tag_filters, "ASC"))
            latest = _edge_time(handler, schema, build_edge_time_query(schema.measurement, schema.tag_filters, "DESC"))
            span = latest - earliest if isinstance(earliest, int) and isinstance(latest, int) else None
            result.update(
                {
                    "earliest": earliest,
                    "latest": latest,
                    "span_seconds": span,
                    "points_present": earliest is not None,
                }
            )
        result["retention"] = _retention_for(handler.session, handler.settings["influx"], schema.db)
        return result
    finally:
        close_session(handler.session)


# How each kind is worded in the generated reference. Written once here rather than
# at the call site, so the document and the tool payload cannot describe the same
# vocabulary differently.
_KIND_PROSE = {
    "gauge": "gauge (instantaneous)",
    # Says where to find the period rather than naming one, since it varies by field and
    # is not recorded: a point is stamped at its interval start, so the spacing shows it.
    "interval": "interval total (sum it; the period is the spacing between points)",
    "counter": "counter (running total, resets)",
    "state": "state (discrete code or label)",
}


def _documentation_field_line(key, meta):
    """Render one field as a Markdown bullet for the generated reference.

    Kept out of :func:`build_documentation` so that function stays within the
    project's complexity limit as the metadata grows more keys.

    Args:
        key (str): the field key (or the ``_``-suffix that stands for a family of them)
        meta (dict): the field's metadata dict

    Returns:
        str: the bullet line for that field
    """
    bits = []
    if meta.get("unit"):
        bits.append(f"unit {meta['unit']}")
    kind = field_kind(meta)
    if kind:
        bits.append(_KIND_PROSE[kind])
    codes = meta.get("codes")
    if codes:
        bits.append("values: " + ", ".join(f"{code}={label}" for code, label in sorted(codes.items())))
    line = f"- `{key}`" + (f" - {'; '.join(bits)}" if bits else "")
    # The description is its own sentence, keeping its full stop, rather than another
    # semicolon-separated bit: several of them contain a semicolon themselves, which left
    # no visible boundary between the short facts and the prose.
    if meta.get("description"):
        line += (". " if bits else " - ") + meta["description"]
    return line


def build_documentation(settings, settings_file):
    """Assemble a static Markdown reference of every configured source.

    Its description and, per annotated field, the unit, any coded-value meanings, how it
    may be aggregated, and what it means where the name does not say.

    Generated from the source classes' own MCP metadata (MCP_DESCRIPTION +
    MCP_FIELD_METADATA), so it can't drift from what the tools expose and needs no
    packaged docs file. Gives the model a one-call, InfluxDB-free overview of what
    every source and field means - orientation the per-source list_fields (a live
    InfluxDB round trip) doesn't provide in one place, and the place the per-field
    prose is available for every source at once rather than one at a time.

    Args:
        settings (dict): parsed settings dict
        settings_file (str or None): settings path, for constructing handlers

    Returns:
        str: the Markdown document
    """
    lines = [
        "# send-to-influx data reference",
        "",
        "What each configured source reports, and what its values mean. Each field gives its unit "
        "where it has one and how it may be aggregated: a gauge is an instantaneous reading (never "
        "sum them), an interval total is a quantity accumulated over its reporting period (sum them "
        "for a total), a counter a running total that resets (take its last value or a difference, "
        "never its mean), a state a discrete code or label. Field keys may carry a per-device prefix "
        "(e.g. a Nuki lock's name); the meanings below are keyed by the base name.",
        "",
    ]
    for source in configured_sources(settings):
        try:
            handler = resolve_handler(source, settings, settings_file)
        except ToolParamError:
            continue
        try:
            description = handler.MCP_DESCRIPTION
            # The class attribute, NOT mcp_field_metadata(): this function's whole
            # contract is that it needs no InfluxDB round trip, and the tool description
            # says so. Calling the hook here made get_documentation query InfluxDB once
            # per Hue source and quietly broke that promise. The cost is that a source
            # with only per-install metadata is absent from the generated reference,
            # which is the honest trade - list_fields is where those fields are described.
            field_metadata = handler.MCP_FIELD_METADATA
        finally:
            close_session(handler.session)
        lines.append(f"## {source}")
        if description:
            lines.append(description)
        lines.append("")
        for key in sorted(field_metadata):
            lines.append(_documentation_field_line(key, field_metadata[key]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _documentation_result(settings, settings_file):
    """Build the get_documentation tool payload (runs in a worker thread).

    Args:
        settings (dict): parsed settings dict
        settings_file (str or None): settings path, threaded to the handler's own load

    Returns:
        dict: the ``get_documentation`` payload
    """
    return {"format": "markdown", "content": build_documentation(settings, settings_file)}


def register_read_tools(server, settings, settings_file=None):
    """Register the read-only MCP tools on a MCPServer server.

    Lists the queryable sources, lists a source's fields, and queries a field's history.
    Blocking HTTP runs in a worker thread so the async event loop is not stalled during
    an InfluxDB round trip.

    Every tool's description is part of the advertised surface and is held to the
    AI-consumer standard: it names the sibling tools a caller might otherwise reach
    for, and states preconditions, side effects and error behaviour in prose rather
    than leaving them to the annotation fields (which clients are told to treat as
    untrusted hints). ``tests/test_mcp_surface.py`` is the guard.

    Args:
        server (MCPServer): the MCPServer instance
        settings (dict): the parsed settings dict
        settings_file (str or None): settings path, for re-resolving handlers per call

    Returns:
        MCPServer: the same server, for chaining
    """
    import anyio

    @register_tool(server, title="List Data Sources", annotations=_READ_ONLY)
    async def list_sources() -> dict:  # noqa: DOC201
        """List the configured collector sources whose data can be read, each with
        its InfluxDB measurement and a line on what it reports.

        The entry point for reads: start here, then `list_fields` for a source's
        exact field names, then `query_history` for recorded history or
        `get_current_state` for the present moment. `get_documentation` is the other
        no-argument call - it explains what every field means, where this names the
        sources and their measurements.

        A source whose measurement holds several producers (e.g. Speedtest, one per
        collecting host) reports the tag that tells them apart as `instance_tag`; the
        values that tag holds come from `list_fields`, because listing them costs a
        query per source.

        Reads configuration only and changes nothing - no InfluxDB or device request -
        so it answers even when nothing is reachable, and a configured source whose
        settings are unusable is left out rather than failing the call.
        """
        return await anyio.to_thread.run_sync(_list_sources_result, settings, settings_file)

    @register_tool(server, title="List Source Fields", annotations=_READ_ONLY)
    async def list_fields(source: str, detail: bool = False) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Describe one source well enough to query it and chart the result: its
        `database` and `measurement`, its `tag_keys` to group by, and every field with
        its InfluxDB `type`, any `unit`, any coded values, and its `kind`. Every
        per-field key except the name is absent rather than null when there is nothing
        to say - `type` included, since InfluxDB does not always report one.

        `kind` is how a value may legitimately be aggregated: 'gauge' is an
        instantaneous reading (never sum them), 'interval' a quantity accumulated over
        its reporting period (sum them for a total), 'counter' a running total that
        resets (read its last value or a difference, never its mean), 'state' a discrete
        code or label. Absent means the source has not said - not that averaging is safe.

        `detail` adds each field's description, where its name and unit do not already
        say what it is. `get_documentation` carries the same prose for every source at
        once, with no InfluxDB round trip.

        Call this before `query_history`: it rejects a field this did not list, and
        exact names are not guessable (spaces become underscores). Use `list_sources`
        when you don't yet know which source you want.

        Where the source's measurement holds several producers, also returns
        `instance_tag` (what tells them apart, e.g. 'host') and `instances` - the
        values `query_history`'s `instance` accepts, being the producers present in
        the data plus any target configured but not yet collecting.

        The field set is discovered live from InfluxDB, and reading it changes
        nothing. A source that has never written anything lists no fields - that
        is "nothing recorded yet", not "no such source". An unknown source is an
        error, and so is an unreachable InfluxDB: neither is reported as an empty
        list.
        """
        return await anyio.to_thread.run_sync(list_fields_result, source, settings, settings_file, detail)

    @register_tool(server, title="Query Historical Data", annotations=_READ_ONLY)
    async def query_history(  # noqa: DOC101,DOC103,DOC108,DOC201
        source: str,
        field: str,
        start: str = "-24h",
        end: str = "now",
        aggregation: str = "raw",
        group_by: "str | None" = None,
        limit: int = DEFAULT_RESULT_POINTS,
        instance: "str | None" = None,
    ) -> dict:
        """Read a field's recorded history for one source from InfluxDB.

        Reads only, and changes nothing: to change a device use that source's control
        tool (e.g. `hue_set_light`), for the present moment use `get_current_state`,
        and to find out what range of history exists use `get_data_range`.

        Get valid `source`/`field` names from `list_sources`/`list_fields` first. An
        unknown field, or a start/end/aggregation/group_by/instance that does not
        parse, is an error rather than empty data - as is an unreachable InfluxDB.

        - start/end: 'now', a past offset like '-24h'/'-7d' (the leading '-' is
          required; the future holds no data), or an ISO 8601 timestamp. Defaults to
          the last 24 hours; start must be before end.
        - aggregation: 'raw' for individual points, or one of mean/median/min/max/
          sum/count/first/last/spread/stddev, each of which requires a `group_by`.
        - group_by: a bucket interval like '5m'/'1h'/'1d' (only with an aggregation).
        - limit: maximum points returned, 1..5000 (a value outside that is clamped).
        - instance: restricts the read to one producer, where the source's
          measurement holds several - `instance='pi4'` for one Speedtest host,
          `instance='hue.example.com'` for one Hue bridge. Omit it and every producer
          is reported separately under `instances`, keyed by tag value, never merged
          into one series. `list_fields` lists the accepted values; an unknown one is
          an error, and so is passing this at all for a single-producer source.

        Points come back newest-first, each with a unix-seconds `time` and `value`,
        plus a decoded `label` for a coded field (e.g. a Nuki lock state).
        `truncated` is true when as many points came back as the limit allowed, so
        more may exist beyond it - narrow the range or aggregate to be sure of a
        complete view. A scoped or single-producer result reports the `limit` in
        force; a per-instance one reports `limit_per_instance`, because InfluxDB
        applies the limit to each producer separately and calling that a total would
        misstate it.
        """
        return await anyio.to_thread.run_sync(
            lambda: _query_history_result(
                settings,
                settings_file,
                source=source,
                field=field,
                start=start,
                end=end,
                aggregation=aggregation,
                group_by=group_by,
                limit=limit,
                instance=instance,
            )
        )

    @register_tool(server, title="Get Current State", annotations=_READ_ONLY)
    async def get_current_state(source: str) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Read a source's state *now* - is the light on, is the door locked, what is
        the power draw at this moment.

        Use this rather than `query_history` (trends, and "when did X change") or
        `get_data_range` (how far back the records go); it covers every configured
        source, where `hue_list_devices` covers only Hue's controllable devices.

        Most sources are read live from the device or API (Hue bridge, Nuki,
        MyEnergi, weather, carbon intensity); Speedtest and Octopus instead return
        the latest point recorded in InfluxDB, because a live read would be slow or
        no fresher. The `state` field says which: 'live' or 'last_recorded'. Reading
        changes nothing.

        `source` is a name from `list_sources`; an unknown one is an error. Returns
        `state` and `as_of` (unix seconds) with a `fields` map of each field's value
        plus any `unit` and decoded `label`, so a lock state reads back as 'locked'
        rather than a bare number. Where a source has several producers (Hue bridges,
        Nuki locks, Speedtest hosts) the fields are grouped per producer under
        `instances` instead, even when there is only one. An unreachable producer
        carries an `error` there while the rest still report fields; only when every
        one fails is the whole call an error.
        """
        return await anyio.to_thread.run_sync(current_state_result, source, settings, settings_file)

    @register_tool(server, title="Get Data Range & Retention", annotations=_READ_ONLY)
    async def get_data_range(source: str) -> dict:  # noqa: DOC101,DOC103,DOC108,DOC201
        """Report how far back a source's data goes, and how long InfluxDB keeps it.

        Answers "when did collection start", "how far back can I query", "how long is
        data kept". Use it before `query_history`, which needs a range you already
        know; `get_current_state` describes only the present moment, and
        `list_fields` only which fields exist.

        Two different facts, both reported, because they answer different questions:

        - `earliest`/`latest` (unix seconds) and `span_seconds`: the oldest and
          newest points actually present - the real floor on what history can
          return. `points_present` is false, with null timestamps, when nothing has
          been collected yet.
        - `retention`: what InfluxDB is configured to keep, whatever was actually
          collected. `duration` is a string like '720h0m0s', or 'infinite' when data
          never expires, with `duration_seconds` alongside for arithmetic; v1 also
          reports the `policy` name, and both versions the shard group duration.

        The two differ, and the difference is the point: an install collecting for
        three years under 30-day retention has 30 days of data.

        Where a source has several producers, each also gets its own range under
        `instances`, with the overall figures alongside - retention bounds the
        database rather than any one producer.

        `source` is a name from `list_sources`; an unknown one is an error, as is an
        unreachable InfluxDB, and reading changes nothing. A failure to read
        retention alone does not fail the call: `retention.known` comes back false
        with a `reason`, reported rather than omitted so it is never mistaken for
        unlimited retention.
        """
        return await anyio.to_thread.run_sync(data_range_result, source, settings, settings_file)

    @register_tool(server, title="Get Field Documentation", annotations=_READ_ONLY)
    async def get_documentation() -> dict:  # noqa: DOC201
        """Return a Markdown reference of what every configured source reports and
        what its values mean: units, the meaning of coded values (e.g. Nuki lock and
        door state codes), how each field may be aggregated, and a description where
        a field's name does not say what it is.

        The cheapest orientation call: no arguments, no InfluxDB round trip, every
        source at once. `list_fields` is the per-source counterpart and lists the
        fields actually present in InfluxDB, which this cannot know; `list_sources`
        names the sources and their measurements without the field detail.

        Returns `{format: 'markdown', content: ...}`. Built from the source classes'
        own metadata, so it changes nothing and cannot fail on an unreachable source
        or InfluxDB; a source whose configuration is unusable is omitted.
        """
        return await anyio.to_thread.run_sync(_documentation_result, settings, settings_file)

    return server
