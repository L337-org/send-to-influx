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
__license__ = "MIT License"

import datetime
import logging
import re
import warnings
from dataclasses import dataclass, field as dataclass_field

import requests
import urllib3

from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.general import get_class, resolve_default_source

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

# A safe InfluxDB identifier (measurement/field/tag key). Field keys are always
# double-quoted in the queries this module builds, but the extra charset gate is
# cheap defence-in-depth and rejects anything a discovery step might return that
# could confuse the parser.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

# A relative time offset like "-24h", "-7d", "-90m", or the literal "now".
_RELATIVE_TIME_RE = re.compile(r"^-?\d+[smhdw]$")

# A GROUP BY interval duration like "5m", "1h", "1d".
_DURATION_RE = re.compile(r"^\d+[smhdw]$")

# Upper bound on points returned by a single query, so a broad range can't
# produce an unbounded response. Applied as a LIMIT and surfaced in the result.
MAX_RESULT_POINTS = 5000
DEFAULT_RESULT_POINTS = 500

_RELATIVE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class QueryParamError(ValueError):
    """A read-query parameter was invalid (unknown field, bad time, etc.).

    Distinct from SourceConnectionError: this is a caller/model mistake to be
    reported back as a tool error, not a transport failure to retry.
    """


@dataclass
class ReadSchema:
    """Everything the read layer needs to query one source, safely.

    ``measurement`` and ``tag_filters`` are the source class's static domain
    knowledge (never model input); ``allowed_fields`` is the live field set
    discovered from InfluxDB (the injection allowlist); ``field_metadata`` maps a
    field key - or a ``_``-delimited suffix, for collectors with dynamic prefixes
    like Nuki's per-lock fields - to ``{"unit": str, "codes": {int: str}}`` for
    result annotation.
    """

    source: str
    measurement: str
    db: str
    tag_filters: dict = dataclass_field(default_factory=dict)
    allowed_fields: set = dataclass_field(default_factory=set)
    field_metadata: dict = dataclass_field(default_factory=dict)

    def metadata_for(self, field):
        """Return the metadata dict for a field: an exact key match first, else
        the *longest* matching ``_``-delimited suffix (so ``Front_Door_stateValue``
        picks up ``stateValue``, and a longer key wins over a shorter one it ends
        with - e.g. ``stateValue`` over ``value``). Empty dict when nothing
        matches. Longest-wins is deterministic regardless of dict order and stays
        correct as metadata grows."""
        if field in self.field_metadata:
            return self.field_metadata[field]
        best_key = None
        for key in self.field_metadata:
            if field.endswith(f"_{key}") and (best_key is None or len(key) > len(best_key)):
                best_key = key
        return self.field_metadata[best_key] if best_key is not None else {}


def build_schema(handler, discovered_fields):
    """Assemble a ReadSchema from a DataHandler instance's static class metadata
    and the live discovered field set.

    Note the field set comes from ``SHOW FIELD KEYS``, which is per-measurement,
    not per-tag. For the three MyEnergi devices that share the ``myenergi``
    measurement, that means each one's field list also shows the others' fields;
    a query for a field that belongs to a different device is still safe and
    simply returns no points (the device tag filter excludes it). Every other
    source owns its measurement, so this only affects the MyEnergi trio.

    :param handler: a constructed DataHandler subclass instance
    :param discovered_fields: field keys found via discover_fields()
    :return: ReadSchema
    """
    measurement = handler.MCP_MEASUREMENT or handler.source
    db = handler.source_settings.get("bucket", handler.source_settings.get("db"))
    return ReadSchema(
        source=handler.source,
        measurement=measurement,
        db=db,
        tag_filters=dict(handler.MCP_TAG_FILTERS),
        allowed_fields=set(discovered_fields),
        field_metadata=dict(handler.MCP_FIELD_METADATA),
    )


def annotate_rows(schema, field, columns, values):
    """Shape a query's (columns, values) into a domain-aware result dict.

    Adds the field's unit (if known) and, for coded fields (Nuki state), a
    decoded label alongside each raw numeric value - an undocumented code is
    passed through with a null label rather than dropped, matching the collector's
    raw-passthrough rule.

    :return: {"field", "unit", "points": [{"time", "value"[, "label"]}], ...}
    """
    meta = schema.metadata_for(field)
    codes = meta.get("codes") or {}
    time_index = columns.index("time") if "time" in columns else 0
    # The value column is whichever isn't "time" (raw queries) or the aggregate
    # column name (mean/max/...); fall back to the last column.
    value_index = next((i for i, c in enumerate(columns) if c != "time"), len(columns) - 1)
    points = []
    for row in values:
        point = {"time": row[time_index], "value": row[value_index]}
        if codes and isinstance(row[value_index], (int, float)) and not isinstance(row[value_index], bool):
            point["label"] = codes.get(int(row[value_index]))
        points.append(point)
    result = {"source": schema.source, "field": field, "points": points}
    if meta.get("unit"):
        result["unit"] = meta["unit"]
    if codes:
        result["codes"] = {str(code): label for code, label in codes.items()}
    return result


def _validate_identifier(value, kind):
    """Return ``value`` if it is a safe InfluxDB identifier, else raise.

    :param value: candidate identifier
    :param kind: what it is, for the error message (e.g. "field")
    :raises QueryParamError: if the value isn't a safe identifier
    """
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise QueryParamError(f"invalid {kind} name: {value!r}")
    return value


def _quote_identifier(value):
    """Double-quote an InfluxDB identifier, escaping backslashes and quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote_string_literal(value):
    """Single-quote an InfluxQL string literal (used for tag values)."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def parse_time_bound(value, *, now=None):
    """Parse a user/model time bound into an aware UTC datetime.

    Accepts the literal ``now``, a relative offset (``-24h``, ``-7d``, ...), or
    an ISO 8601 / RFC 3339 timestamp. A naive timestamp is assumed UTC. Only the
    parsed value is ever re-emitted into a query, never the raw input string.

    :param value: the time expression
    :param now: reference time for ``now``/relative offsets (defaults to
        the current UTC time); injected for testability
    :return: timezone-aware UTC datetime
    :raises QueryParamError: if the value can't be parsed
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise QueryParamError(f"invalid time value: {value!r}")
    text = value.strip()
    if text == "now":
        return now
    if _RELATIVE_TIME_RE.match(text):
        sign = -1 if text.startswith("-") else 1
        digits = text.lstrip("-")
        seconds = int(digits[:-1]) * _RELATIVE_UNIT_SECONDS[digits[-1]]
        return now + datetime.timedelta(seconds=sign * seconds)
    # Accept a trailing Z (RFC 3339) that fromisoformat historically rejected.
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except ValueError:
        raise QueryParamError(
            f"invalid time value: {value!r} - use 'now', a relative offset like '-24h', " "or an ISO 8601 timestamp"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _rfc3339(dt):
    """Format an aware datetime as an RFC3339 string InfluxQL accepts."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_query(schema, *, field, start, end, aggregation="raw", group_by=None, limit=DEFAULT_RESULT_POINTS):
    """Build a parameterised InfluxQL SELECT for a source's measurement.

    Every dynamic part is validated: the field against the schema's live
    allowlist, times parsed to RFC3339, aggregation against AGGREGATIONS, and any
    group_by against the duration grammar. Identifiers are charset-checked and
    double-quoted.

    :param schema: a ReadSchema (measurement, tag filters, allowed fields)
    :type schema: ReadSchema
    :param field: the field key to query (must be in schema.allowed_fields)
    :param start: start time bound (see parse_time_bound)
    :param end: end time bound (see parse_time_bound)
    :param aggregation: one of AGGREGATIONS, or "raw" for un-aggregated points
    :param group_by: GROUP BY time interval (required when aggregating), e.g. "1h"
    :param limit: maximum points to return (clamped to MAX_RESULT_POINTS)
    :return: the InfluxQL query string
    :raises QueryParamError: on any invalid parameter
    """
    if field not in schema.allowed_fields:
        raise QueryParamError(
            f"unknown field {field!r} for source {schema.source!r}; "
            f"available fields: {', '.join(sorted(schema.allowed_fields)) or '(none)'}"
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
        raise QueryParamError(f"start ({_rfc3339(start_dt)}) must be before end ({_rfc3339(end_dt)})")

    if aggregation == "raw":
        select_expr = _quote_identifier(field)
        group_clause = ""
    else:
        func = AGGREGATIONS.get(aggregation)
        if func is None:
            raise QueryParamError(
                f"unknown aggregation {aggregation!r}; choose one of: raw, {', '.join(sorted(AGGREGATIONS))}"
            )
        select_expr = f"{func}({_quote_identifier(field)})"
        if not group_by:
            raise QueryParamError(f"aggregation {aggregation!r} requires a group_by interval (e.g. '1h')")
        if not _DURATION_RE.match(str(group_by)):
            raise QueryParamError(f"invalid group_by interval {group_by!r}; use a duration like '5m', '1h', '1d'")
        group_clause = f" GROUP BY time({group_by}) fill(none)"

    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        raise QueryParamError(f"invalid limit {limit!r}") from None
    limit_value = max(1, min(limit_value, MAX_RESULT_POINTS))

    where = [
        f"time >= {_quote_string_literal(_rfc3339(start_dt))}",
        f"time <= {_quote_string_literal(_rfc3339(end_dt))}",
    ]
    for tag_key, tag_value in sorted(schema.tag_filters.items()):
        _validate_identifier(tag_key, "tag")
        where.append(f"{_quote_identifier(tag_key)} = {_quote_string_literal(tag_value)}")

    return (
        f"SELECT {select_expr} FROM {_quote_identifier(schema.measurement)} "
        f"WHERE {' AND '.join(where)}{group_clause} "
        f"ORDER BY time DESC LIMIT {limit_value}"
    )


def _influx_read_request(influx_settings, db, query):
    """Build (url, kwargs) for a GET /query, mirroring _build_write_request's
    v1/v2 branch: token+org via the v2 /query compatibility endpoint (Token
    header), else v1 /query with HTTP basic auth. epoch=s returns numeric unix
    timestamps rather than RFC3339 strings.

    :param influx_settings: the ``influx`` settings block
    :param db: the database/bucket name to query
    :param query: the InfluxQL query string
    :return: (url, requests kwargs)
    """
    timeout = influx_settings.get("timeout", 5)
    params = {"db": db, "q": query, "epoch": "s"}
    url = f'{influx_settings["url"]}/query'
    if influx_settings.get("token"):
        # The v1-compatibility /query endpoint resolves the bucket via its DBRP
        # mapping (keyed by db) and the token is already org-scoped, so org isn't
        # strictly required - but pass it when set, mirroring the v2 write path
        # and disambiguating a token with access to more than one org.
        if influx_settings.get("org"):
            params["org"] = influx_settings["org"]
        kwargs = {"headers": {"Authorization": f'Token {influx_settings["token"]}'}, "params": params}
    else:
        kwargs = {"auth": (influx_settings["user"], influx_settings["password"]), "params": params}
    kwargs["verify"] = not influx_settings.get("insecure", False)
    kwargs["timeout"] = timeout
    return url, kwargs


def _get(session, url, kwargs, description):
    """Issue a GET and return parsed JSON, mapping failures to
    SourceConnectionError with a message naming what was attempted."""
    try:
        with warnings.catch_warnings():
            if not kwargs.get("verify", True):
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            response = session.get(url, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as exc:
        logging.error("MCP read failed (%s): %s", description, exc)
        raise SourceConnectionError(f"InfluxDB read failed ({description}): {exc}") from exc
    except ValueError as exc:
        logging.error("MCP read returned non-JSON (%s): %s", description, exc)
        raise SourceConnectionError(f"InfluxDB read returned an unparseable response ({description})") from exc


def discover_fields(session, influx_settings, db, measurement):
    """Return the set of field keys present in a measurement, via SHOW FIELD
    KEYS. This is the live allowlist a queried field is checked against. The
    measurement is charset-validated (it comes from the source class's static
    schema, but validating is cheap) before interpolation.

    :return: set of field-key strings (possibly empty)
    :raises SourceConnectionError: on a transport/parse failure
    """
    _validate_identifier(measurement, "measurement")
    query = f"SHOW FIELD KEYS FROM {_quote_identifier(measurement)}"
    url, kwargs = _influx_read_request(influx_settings, db, query)
    payload = _get(session, url, kwargs, f"discover fields for {measurement}")
    fields = set()
    for result in payload.get("results", []):
        for series in result.get("series", []):
            name_index = series.get("columns", []).index("fieldKey") if "fieldKey" in series.get("columns", []) else 0
            for row in series.get("values", []):
                if row and isinstance(row[name_index], str):
                    fields.add(row[name_index])
    return fields


def run_query(session, influx_settings, db, query):
    """Execute an InfluxQL query and return its first series' (columns, values),
    or ([], []) when the query matched nothing.

    :return: (columns, values) where columns is a list of names and values is a
        list of rows
    :raises SourceConnectionError: on a transport/parse failure
    """
    url, kwargs = _influx_read_request(influx_settings, db, query)
    payload = _get(session, url, kwargs, "query")
    for result in payload.get("results", []):
        if result.get("error"):
            raise SourceConnectionError(f"InfluxDB rejected the query: {result['error']}")
        for series in result.get("series", []):
            return series.get("columns", []), series.get("values", [])
    return [], []


def configured_sources(settings):
    """Return the lowercased source names the MCP read tools expose - the same
    ``sources:`` list the collectors run, so the two can't drift. Falls back to
    the single default source when no list is configured."""
    raw = settings.get("sources")
    if isinstance(raw, list) and raw:
        return [str(src).lower() for src in raw if isinstance(src, str)]
    return [resolve_default_source(settings)]


def _resolve_handler(source, settings, settings_file):
    """Construct the DataHandler for a configured source, or raise
    QueryParamError if the name isn't one the read tools expose. Case-insensitive,
    matching the collector factory."""
    if not isinstance(source, str) or not source.strip():
        raise QueryParamError(f"source must be a non-empty string (got {source!r})")
    available = configured_sources(settings)
    if source.lower() not in available:
        raise QueryParamError(
            f"unknown source {source!r}; available sources: {', '.join(sorted(available)) or '(none)'}"
        )
    try:
        return get_class(source, settings_file)
    except ConfigError as exc:
        raise QueryParamError(f"source {source!r} is not usable: {exc}") from exc


def resolve_schema(source, settings, settings_file):
    """Build a fully-populated ReadSchema for a source: its static class metadata
    plus the live field allowlist discovered from InfluxDB. Constructs a handler
    from current settings each call, so a live settings edit is picked up.

    :raises QueryParamError: for an unknown/unusable source
    :raises SourceConnectionError: if field discovery fails
    """
    handler = _resolve_handler(source, settings, settings_file)
    measurement = handler.MCP_MEASUREMENT or handler.source
    db = handler.source_settings.get("bucket", handler.source_settings.get("db"))
    fields = discover_fields(handler.session, settings["influx"], db, measurement)
    return handler, build_schema(handler, fields)


def _list_sources_result(settings, settings_file):
    """Build the list_sources tool payload (runs in a worker thread)."""
    out = []
    for source in configured_sources(settings):
        try:
            handler = _resolve_handler(source, settings, settings_file)
        except QueryParamError:
            continue
        out.append({"source": source, "measurement": handler.MCP_MEASUREMENT or handler.source})
    return {"sources": out}


def _list_fields_result(source, settings, settings_file):
    """Build the list_fields tool payload (runs in a worker thread)."""
    _handler, schema = resolve_schema(source, settings, settings_file)
    fields = []
    for name in sorted(schema.allowed_fields):
        meta = schema.metadata_for(name)
        entry = {"field": name}
        if meta.get("unit"):
            entry["unit"] = meta["unit"]
        if meta.get("codes"):
            entry["codes"] = {str(code): label for code, label in meta["codes"].items()}
        fields.append(entry)
    return {"source": source, "measurement": schema.measurement, "fields": fields}


def _query_history_result(settings, settings_file, *, source, field, start, end, aggregation, group_by, limit):
    """Build the query_history tool payload (runs in a worker thread)."""
    handler, schema = resolve_schema(source, settings, settings_file)
    query = build_query(
        schema, field=field, start=start, end=end, aggregation=aggregation, group_by=group_by, limit=limit
    )
    columns, values = run_query(handler.session, settings["influx"], schema.db, query)
    return annotate_rows(schema, field, columns, values)


def register_read_tools(server, settings, settings_file=None):
    """Register the read-only MCP tools on a FastMCP server: list the queryable
    sources, list a source's fields, and query a field's history. Blocking HTTP
    runs in a worker thread so the async event loop isn't stalled during an
    InfluxDB round trip.

    :param server: the FastMCP instance
    :param settings: the parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per call
    """
    import anyio

    @server.tool()
    async def list_sources() -> dict:
        """List the collector sources whose history can be queried, with the
        InfluxDB measurement each maps to."""
        return await anyio.to_thread.run_sync(_list_sources_result, settings, settings_file)

    @server.tool()
    async def list_fields(source: str) -> dict:
        """List the field keys available for a source, with any known unit and
        the meanings of coded fields. Use this to discover what `query_history`
        can ask for."""
        return await anyio.to_thread.run_sync(_list_fields_result, source, settings, settings_file)

    @server.tool()
    async def query_history(
        source: str,
        field: str,
        start: str = "-24h",
        end: str = "now",
        aggregation: str = "raw",
        group_by: "str | None" = None,
        limit: int = DEFAULT_RESULT_POINTS,
    ) -> dict:
        """Query a field's history for a source from InfluxDB.

        - start/end: 'now', a relative offset like '-24h'/'-7d', or an ISO 8601
          timestamp. Defaults to the last 24 hours.
        - aggregation: 'raw' (individual points) or one of mean/median/min/max/
          sum/count/first/last/spread/stddev, which require a group_by interval.
        - group_by: a bucket interval like '5m'/'1h'/'1d' (only with aggregation).
        - limit: max points returned (capped server-side).

        Points come back newest-first, each with a unix-seconds time and value;
        coded fields (e.g. Nuki lock state) also carry a decoded label.
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
            )
        )

    return server
