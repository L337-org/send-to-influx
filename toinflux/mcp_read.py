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

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.mcp_common import close_session, configured_sources, resolve_handler

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

# An identifier (measurement/field/tag key) is rejected only if it is empty or
# contains an ASCII control character (which could corrupt query formatting or a
# log line). The charset is otherwise unrestricted on purpose: field keys can
# legitimately contain punctuation - line protocol escapes only comma/equals/
# space/backslash, and collectors like Hue merely replace spaces with underscores
# (a light "Kitchen (main)" becomes the field key "Kitchen_(main)"), so a stricter
# charset would make real fields discoverable via SHOW FIELD KEYS yet unqueryable.
# Injection safety rests on the allowlist (a queried field must be a key that
# discovery actually returned) plus double-quote escaping in _quote_identifier,
# not on this gate.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

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
        """Return the metadata dict for a field in this schema - see the
        module-level :func:`metadata_for`."""
        return metadata_for(self.field_metadata, field)


def resolve_db(source_settings, influx_settings):
    """Return the database/bucket name the collector actually writes to, matching
    ``DataHandler._build_write_request()`` exactly: v2 (``influx.token`` set) uses
    ``bucket`` falling back to ``db``; v1 uses ``db`` only, ignoring ``bucket``.

    Mirroring the write path matters because a config can carry both keys - e.g.
    a stale ``bucket`` left after switching v2->v1 - and picking ``bucket`` in v1
    mode would send reads to a different database than the collectors write to.

    :param source_settings: the source's own settings block
    :param influx_settings: the ``influx`` block (its ``token`` selects the mode)
    :return: the db/bucket name (or None if unset)
    """
    if influx_settings.get("token"):
        return source_settings.get("bucket", source_settings.get("db"))
    return source_settings.get("db")


def build_schema(handler, discovered_fields, db):
    """Assemble a ReadSchema from a DataHandler instance's static class metadata,
    the live discovered field set, and the resolved db (see resolve_db).

    Note the field set comes from ``SHOW FIELD KEYS``, which is per-measurement,
    not per-tag. For the three MyEnergi devices that share the ``myenergi``
    measurement, that means each one's field list also shows the others' fields;
    a query for a field that belongs to a different device is still safe and
    simply returns no points (the device tag filter excludes it). Every other
    source owns its measurement, so this only affects the MyEnergi trio.

    :param handler: a constructed DataHandler subclass instance
    :param discovered_fields: field keys found via discover_fields()
    :param db: the resolved database/bucket name (from resolve_db)
    :return: ReadSchema
    """
    measurement = handler.MCP_MEASUREMENT or handler.source
    return ReadSchema(
        source=handler.source,
        measurement=measurement,
        db=db,
        tag_filters=dict(handler.MCP_TAG_FILTERS),
        allowed_fields=set(discovered_fields),
        field_metadata=dict(handler.MCP_FIELD_METADATA),
    )


def metadata_for(field_metadata, field):
    """Return the metadata dict for a field: an exact key match first, else the
    *longest* matching ``_``-delimited suffix (so ``Front_Door_stateValue`` picks
    up ``stateValue``, and a longer key wins over a shorter one it ends with -
    e.g. ``stateValue`` over ``value``). Empty dict when nothing matches.
    Longest-wins is deterministic regardless of dict order and stays correct as
    metadata grows.

    Kept module-level (not only a ReadSchema method) so the live current-state
    path can annotate a source's raw ``get_data()`` fields straight from the
    handler's ``MCP_FIELD_METADATA``, without building an InfluxDB-backed schema.

    :param field_metadata: a source's ``MCP_FIELD_METADATA`` mapping
    :param field: the field key to look up
    :return: the metadata dict (``{"unit"...}``/``{"codes"...}``) or ``{}``
    """
    if field in field_metadata:
        return field_metadata[field]
    best_key = None
    for key in field_metadata:
        if field.endswith(f"_{key}") and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return field_metadata[best_key] if best_key is not None else {}


def _decode_code(value, codes):
    """Return the label for a coded value, or None.

    Only a genuine integer decodes - an int, or an integer-valued float (the
    collector writes every numeric field as a float, so a lock state arrives as
    1.0). A non-integer float (1.5) or a bool is never truncated to a code; it
    gets a null label rather than a wrong one.
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
    """Shape one current-state field into ``{"value"[, "unit"][, "label"]}``,
    reusing the same per-field metadata (unit, coded-value labels) as the history
    tool. An undocumented coded value passes through with a null label.

    :param field_metadata: the source's ``MCP_FIELD_METADATA``
    :param name: the field key (possibly device-prefixed)
    :param value: the field's current value
    :return: the annotated entry dict
    """
    meta = metadata_for(field_metadata, name)
    entry = {"value": value}
    if meta.get("unit"):
        entry["unit"] = meta["unit"]
    codes = meta.get("codes")
    if codes:
        entry["label"] = _decode_code(value, codes)
    return entry


def _validate_identifier(value, kind):
    """Return ``value`` if it is a safe InfluxDB identifier, else raise.

    :param value: candidate identifier
    :param kind: what it is, for the error message (e.g. "field")
    :raises ToolParamError: if the value isn't a safe identifier
    """
    if not isinstance(value, str) or not value or _CONTROL_CHAR_RE.search(value):
        raise ToolParamError(f"invalid {kind} name: {value!r}")
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
    :raises ToolParamError: if the value can't be parsed
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
    """Format an aware datetime as an RFC3339 string InfluxQL accepts."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp_limit(limit):
    """Validate and clamp a requested point limit into [1, MAX_RESULT_POINTS].

    :raises ToolParamError: if the value isn't an integer
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ToolParamError(f"invalid limit {limit!r}") from None
    return max(1, min(value, MAX_RESULT_POINTS))


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
    :raises ToolParamError: on any invalid parameter
    """
    if field not in schema.allowed_fields:
        raise ToolParamError(
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
        raise ToolParamError(f"start ({_rfc3339(start_dt)}) must be before end ({_rfc3339(end_dt)})")

    if aggregation == "raw":
        select_expr = _quote_identifier(field)
        group_clause = ""
    else:
        func = AGGREGATIONS.get(aggregation)
        if func is None:
            raise ToolParamError(
                f"unknown aggregation {aggregation!r}; choose one of: raw, {', '.join(sorted(AGGREGATIONS))}"
            )
        select_expr = f"{func}({_quote_identifier(field)})"
        if not group_by:
            raise ToolParamError(f"aggregation {aggregation!r} requires a group_by interval (e.g. '1h')")
        if not _DURATION_RE.match(str(group_by)):
            raise ToolParamError(f"invalid group_by interval {group_by!r}; use a duration like '5m', '1h', '1d'")
        group_clause = f" GROUP BY time({group_by}) fill(none)"

    limit_value = _clamp_limit(limit)

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


def build_latest_query(measurement, tag_filters, fields):
    """Build an InfluxQL SELECT for the single most recent point of a measurement -
    the current-state read for a non-live source (see MCP_LIVE_STATE).

    Selects each field explicitly (not ``*``) so tag columns are excluded, and
    applies the source's static tag filters. Measurement, field and tag keys are
    charset-validated and double-quoted, tag values quoted string literals - the
    same layered defence as build_query. Fields come from discover_fields (the
    live allowlist), never model input.

    :param measurement: the InfluxDB measurement name
    :param tag_filters: static tag key/value filters (may be empty)
    :param fields: the field keys to select (non-empty)
    :return: the InfluxQL query string
    """
    _validate_identifier(measurement, "measurement")
    select = ", ".join(_quote_identifier(_validate_identifier(f, "field")) for f in sorted(fields))
    query = f"SELECT {select} FROM {_quote_identifier(measurement)}"
    conditions = []
    for tag_key, tag_value in sorted(tag_filters.items()):
        _validate_identifier(tag_key, "tag")
        conditions.append(f"{_quote_identifier(tag_key)} = {_quote_string_literal(tag_value)}")
    if conditions:
        query += f" WHERE {' AND '.join(conditions)}"
    return query + " ORDER BY time DESC LIMIT 1"


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
        # A per-result error (wrong db, auth, ...) is returned in a 200 body, same
        # as run_query - surface it, or an empty field set would later masquerade
        # as every field being "unknown" and hide the real InfluxDB failure.
        if result.get("error"):
            raise SourceConnectionError(f"InfluxDB rejected the field discovery: {result['error']}")
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


def resolve_schema(source, settings, settings_file):
    """Build a fully-populated ReadSchema for a source: its static class metadata
    plus the live field allowlist discovered from InfluxDB. Constructs a handler
    from current settings each call, so a live settings edit is picked up.

    :raises ToolParamError: for an unknown/unusable source
    :raises SourceConnectionError: if field discovery fails
    """
    handler = resolve_handler(source, settings, settings_file)
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
        fields = discover_fields(handler.session, influx_settings, db, measurement)
    except Exception:
        # A fresh requests.Session is created per handler (per tool call); close
        # it if discovery fails, or a long-running server accumulates open
        # connection pools/FDs on intermittent errors. On success the caller owns
        # the returned handler and closes its session when done.
        close_session(handler.session)
        raise
    return handler, build_schema(handler, fields, db)


def _list_sources_result(settings, settings_file):
    """Build the list_sources tool payload (runs in a worker thread)."""
    out = []
    for source in configured_sources(settings):
        try:
            handler = resolve_handler(source, settings, settings_file)
        except ToolParamError:
            continue
        # Constructed only to read class metadata; close its session immediately.
        try:
            out.append(
                {
                    "source": source,
                    "measurement": handler.MCP_MEASUREMENT or handler.source,
                    "description": handler.MCP_DESCRIPTION,
                }
            )
        finally:
            close_session(handler.session)
    return {"sources": out}


def list_fields_result(source, settings, settings_file):
    """Build the list_fields tool payload (runs in a worker thread)."""
    handler, schema = resolve_schema(source, settings, settings_file)
    try:
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
    finally:
        close_session(handler.session)


def _query_history_result(settings, settings_file, *, source, field, start, end, aggregation, group_by, limit):
    """Build the query_history tool payload (runs in a worker thread)."""
    handler, schema = resolve_schema(source, settings, settings_file)
    try:
        return _run_query_history(handler, schema, field, start, end, aggregation, group_by, limit)
    finally:
        close_session(handler.session)


def _run_query_history(handler, schema, field, start, end, aggregation, group_by, limit):
    """Execute the query and shape the payload (session lifecycle owned by the
    caller). Split out so _query_history_result's finally: stays a thin wrapper."""
    # handler.settings["influx"], not the startup snapshot, so the query runs
    # against the same (possibly freshly-edited) InfluxDB the schema was
    # discovered from - see resolve_schema.
    query = build_query(
        schema, field=field, start=start, end=end, aggregation=aggregation, group_by=group_by, limit=limit
    )
    columns, values = run_query(handler.session, handler.settings["influx"], schema.db, query)
    result = annotate_rows(schema, field, columns, values)
    # Surface the effective limit and whether the query hit it. `truncated` means
    # exactly that - the result reached the limit, so more data *may* exist beyond
    # it (if precisely `limit` points exist, nothing more does) - it's a prompt to
    # narrow the range or aggregate, not a guarantee of omitted data. build_query
    # already validated limit, so this can't raise.
    effective_limit = _clamp_limit(limit)
    result["limit"] = effective_limit
    result["truncated"] = len(result["points"]) >= effective_limit
    return result


def _latest_recorded(handler):
    """Read the most recent recorded point for a non-live source from InfluxDB.

    :param handler: a constructed DataHandler (caller owns its session)
    :return: (``{field: value}`` dict, ``as_of`` unix-seconds or None). Empty dict
        when the measurement has no fields or no points recorded yet.
    """
    influx_settings = handler.settings["influx"]
    db = resolve_db(handler.source_settings, influx_settings)
    measurement = handler.MCP_MEASUREMENT or handler.source
    fields = discover_fields(handler.session, influx_settings, db, measurement)
    if not fields:
        return {}, None
    query = build_latest_query(measurement, handler.MCP_TAG_FILTERS, fields)
    columns, values = run_query(handler.session, influx_settings, db, query)
    if not values:
        return {}, None
    row = values[0]
    index = {col: i for i, col in enumerate(columns)}
    # Skip a field that came back NULL (a field written in some points but not the
    # latest one) rather than reporting a meaningless None as its current value.
    data = {name: row[index[name]] for name in sorted(fields) if name in index and row[index[name]] is not None}
    as_of = row[index["time"]] if "time" in index else None
    return data, as_of


def current_state_result(source, settings, settings_file):
    """Build the get_current_state payload (runs in a worker thread).

    A live source (MCP_LIVE_STATE, the default) reports from its own get_data() -
    a cheap API/MQTT read of the device's current state. A non-live source
    (Speedtest, Octopus) reports the latest recorded point from InfluxDB and never
    calls get_data(). Every field carries the same unit/decoded-label annotation
    as the history tool, so a coded value reads back as its label ("locked"), not
    a bare number.

    :raises ToolParamError: unknown/unusable source
    :raises SourceConnectionError: a live get_data() or InfluxDB read failed
    """
    handler = resolve_handler(source, settings, settings_file)
    try:
        if handler.MCP_LIVE_STATE:
            data = handler.get_data() or {}
            as_of = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            state_kind = "live"
        else:
            data, as_of = _latest_recorded(handler)
            state_kind = "last_recorded"
        field_metadata = handler.MCP_FIELD_METADATA
        fields = {name: _annotate_state_field(field_metadata, name, value) for name, value in sorted(data.items())}
        result = {
            "source": handler.source,
            "state": state_kind,
            "as_of": as_of,
            "fields": fields,
        }
        if handler.MCP_DESCRIPTION:
            result["description"] = handler.MCP_DESCRIPTION
        return result
    finally:
        close_session(handler.session)


def build_documentation(settings, settings_file):
    """Assemble a static Markdown reference of every configured source: its
    description and, per annotated field, the unit and any coded-value meanings.

    Generated from the source classes' own MCP metadata (MCP_DESCRIPTION +
    MCP_FIELD_METADATA), so it can't drift from what the tools expose and needs no
    packaged docs file. Gives the model a one-call, InfluxDB-free overview of what
    every source and field means - orientation the per-source list_fields (a live
    InfluxDB round trip) doesn't provide in one place.

    :param settings: parsed settings dict
    :param settings_file: settings path, for constructing handlers
    :return: the Markdown document as a string
    """
    lines = [
        "# send-to-influx data reference",
        "",
        "What each configured source reports, and what its values mean. Field keys may carry a "
        "per-device prefix (e.g. a Nuki lock's name); the meanings below are keyed by the base name.",
        "",
    ]
    for source in configured_sources(settings):
        try:
            handler = resolve_handler(source, settings, settings_file)
        except ToolParamError:
            continue
        try:
            description = handler.MCP_DESCRIPTION
            field_metadata = handler.MCP_FIELD_METADATA
        finally:
            close_session(handler.session)
        lines.append(f"## {source}")
        if description:
            lines.append(description)
        lines.append("")
        for key in sorted(field_metadata):
            meta = field_metadata[key]
            bits = []
            if meta.get("unit"):
                bits.append(f"unit {meta['unit']}")
            codes = meta.get("codes")
            if codes:
                bits.append("values: " + ", ".join(f"{code}={label}" for code, label in sorted(codes.items())))
            lines.append(f"- `{key}`" + (f" - {'; '.join(bits)}" if bits else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _documentation_result(settings, settings_file):
    """Build the get_documentation tool payload (runs in a worker thread)."""
    return {"format": "markdown", "content": build_documentation(settings, settings_file)}


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
        """List the collector sources whose history can be queried, each with its
        InfluxDB measurement.

        The entry point for reads and the only one needing no arguments: start
        here, then `list_fields` for a source's fields, then `query_history` to
        read them. Takes no parameters and returns every configured source; use
        `list_fields` when you already know the source and want its fields."""
        return await anyio.to_thread.run_sync(_list_sources_result, settings, settings_file)

    @server.tool()
    async def list_fields(source: str) -> dict:
        """List the field keys available for one source, each with any known unit
        and, for coded fields, what each numeric value means.

        Call this before `query_history`: a field name it did not list is
        rejected as an error, so use it to discover exact field names (they can
        contain spaces-as-underscores and punctuation). Use `list_sources`
        instead when you don't yet know which source you want. `source` is a
        source name from `list_sources`; an unknown one returns an error."""
        return await anyio.to_thread.run_sync(list_fields_result, source, settings, settings_file)

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
        """Query a field's history for a source from InfluxDB. Reads only; to
        change a device use that source's control tool, e.g. `hue_set_light`
        (when write-enabled).

        Discover valid `source`/`field` names with `list_sources`/`list_fields`
        first - an unknown field, or a start/end/aggregation/group_by that does
        not parse, returns an error rather than empty data.

        - start/end: 'now', a relative past offset like '-24h'/'-7d' (leading '-'
          required; the future has no data), or an ISO 8601 timestamp. Defaults to
          the last 24 hours; start must be before end.
        - aggregation: 'raw' (individual points) or one of mean/median/min/max/
          sum/count/first/last/spread/stddev, which each require a group_by interval.
        - group_by: a bucket interval like '5m'/'1h'/'1d' (only with aggregation).
        - limit: max points returned, 1..5000 (values outside are clamped).

        Points come back newest-first, each with a unix-seconds `time` and
        `value`; coded fields (e.g. Nuki lock state) also carry a decoded `label`.
        The result also reports the effective `limit` and a `truncated` flag -
        `truncated` is true when the query returned as many points as the limit
        allowed, so more data may exist beyond it; narrow the range or use an
        aggregation to be sure of a complete view.
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

    @server.tool()
    async def get_current_state(source: str) -> dict:
        """Get a source's current state *now* - the live answer to "is the light
        on?", "is the door locked?", "which devices are on?". Use this, not
        `query_history`, for the present moment; history is for trends and "when
        did X change?".

        For most sources this reads the device live (Hue bridge, Nuki, MyEnergi,
        weather, carbon intensity). For Speedtest and Octopus it returns the
        latest recorded reading from InfluxDB instead (a live read would be slow
        or no fresher) - the `state` field says which: 'live' or 'last_recorded'.

        `source` is a name from `list_sources`; an unknown one returns an error.
        Returns the source, its `state`/`as_of` (unix seconds), and a `fields` map
        of each field to its `value` plus any `unit` and decoded `label` (so a
        lock state reads back as 'locked', not a bare number)."""
        return await anyio.to_thread.run_sync(current_state_result, source, settings, settings_file)

    @server.tool()
    async def get_documentation() -> dict:
        """Get a reference for what every source reports and what its values mean -
        units, and the meaning of coded values (e.g. Nuki lock/door state codes).

        A good first call for orientation: it needs no arguments and no InfluxDB
        round trip, and covers all sources in one go (unlike `list_fields`, which
        is per-source and lists only the fields currently in InfluxDB). Returns
        `{format: 'markdown', content: ...}`."""
        return await anyio.to_thread.run_sync(_documentation_result, settings, settings_file)

    return server
