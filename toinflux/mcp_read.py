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

from mcp.types import ToolAnnotations

from toinflux.exceptions import SourceConnectionError, ToolParamError
from toinflux.general import INSTANCED_SOURCES
from toinflux.mcp_common import close_session, configured_sources, resolve_handler, resolve_handlers

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
        tag_filters=handler.mcp_tag_filters(),
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


def _build_single_point_query(measurement, tag_filters, fields, order):
    """Build an InfluxQL SELECT for one point at either end of a measurement.

    Shared by :func:`build_latest_query` and :func:`build_edge_time_query` - kept as one
    implementation so the measurement/tag validation and quoting below cannot drift between
    the value read and the timestamp-only reads.

    Selects each field explicitly (not ``*``) so tag columns are excluded, and applies the
    source's static tag filters. Measurement, field and tag keys are charset-validated and
    double-quoted, tag values quoted string literals - the same layered defence as
    build_query. Fields come from discover_fields (the live allowlist), never model input.

    :param measurement: the InfluxDB measurement name
    :param tag_filters: static tag key/value filters (may be empty)
    :param fields: the field keys to select (non-empty)
    :param order: ``"DESC"`` for the newest point, ``"ASC"`` for the oldest
    :return: the InfluxQL query string
    """
    if order not in ("ASC", "DESC"):
        raise ValueError(f"order must be ASC or DESC, got {order!r}")
    _validate_identifier(measurement, "measurement")
    if fields is None:
        # Only for callers that read the timestamp and nothing else - see
        # build_edge_time_query. Enumerating fields is what keeps tag columns out of a
        # *value* read, which does not apply when no value is read.
        select = "*"
    else:
        select = ", ".join(_quote_identifier(_validate_identifier(f, "field")) for f in sorted(fields))
    query = f"SELECT {select} FROM {_quote_identifier(measurement)}"
    conditions = []
    for tag_key, tag_value in sorted(tag_filters.items()):
        _validate_identifier(tag_key, "tag")
        conditions.append(f"{_quote_identifier(tag_key)} = {_quote_string_literal(tag_value)}")
    if conditions:
        query += f" WHERE {' AND '.join(conditions)}"
    return query + f" ORDER BY time {order} LIMIT 1"


def build_latest_query(measurement, tag_filters, fields):
    """Build an InfluxQL SELECT for the single most recent point of a measurement -
    the current-state read for a non-live source (see MCP_LIVE_STATE).

    :param measurement: the InfluxDB measurement name
    :param tag_filters: static tag key/value filters (may be empty)
    :param fields: the field keys to select (non-empty)
    :return: the InfluxQL query string
    """
    return _build_single_point_query(measurement, tag_filters, fields, "DESC")


def build_edge_time_query(measurement, tag_filters, order):
    """Build an InfluxQL SELECT for the timestamp at one end of a measurement's data.

    ``ORDER BY time ASC`` answers "when did collection start, or where has older data aged
    out" - the oldest surviving point is the floor of what any history query can return,
    whatever retention permits in principle. ``DESC`` gives the newest.

    Selects ``*`` rather than enumerating fields, unlike :func:`build_latest_query`, because
    the caller reads only the ``time`` column. Enumerating them here would put every field
    key in the query string, and that string travels in a GET parameter: measured against a
    real InfluxDB with a 120-field measurement, the enumerated form was a 3.4 KB query, and a
    measurement grows with device count (a Nuki install prefixes fields per lock). A wide
    enough estate would exceed a reverse proxy's request-line limit, failing a read that has
    no need of the width. Tag columns coming back in the row are harmless when no value is
    read from it.

    :param measurement: the InfluxDB measurement name
    :param tag_filters: static tag key/value filters (may be empty)
    :param order: ``"ASC"`` for the oldest point, ``"DESC"`` for the newest
    :return: the InfluxQL query string
    """
    return _build_single_point_query(measurement, tag_filters, None, order)


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
    except ValueError as exc:
        # response.json() on a non-JSON body raises requests' JSONDecodeError, which
        # is BOTH a ValueError and a RequestException - catch it before the
        # RequestException handler so a parse failure isn't misreported as a
        # transport read failure. raise_for_status()'s HTTPError is a
        # RequestException but not a ValueError, so it still classifies as transport.
        logging.error("MCP read returned non-JSON (%s): %s", description, exc)
        raise SourceConnectionError(f"InfluxDB read returned an unparseable response ({description})") from exc
    except requests.exceptions.RequestException as exc:
        logging.error("MCP read failed (%s): %s", description, exc)
        raise SourceConnectionError(f"InfluxDB read failed ({description}): {exc}") from exc


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


@dataclass(frozen=True)
class QuerySeries:
    """One series from an InfluxQL result: its tag set, columns and rows.

    ``tags`` is empty for an ungrouped query. A ``GROUP BY`` on a tag returns one
    of these per tag value, which is what makes a per-instance answer possible.
    """

    tags: dict
    columns: list
    values: list


def run_query(session, influx_settings, db, query):
    """Execute an InfluxQL query and return **every** series it produced.

    A ``GROUP BY`` on a tag yields one series per tag value, each carrying its own
    ``tags`` map (verified against InfluxDB 1.8 and 2.7's v1-compatibility
    endpoint, whose responses are identical here). An earlier version returned only
    the first series, which silently discarded every producer but one - invisible
    while every query happened to be ungrouped, and wrong the moment one is not.
    Callers that genuinely cannot produce more than one series use
    :func:`single_series` to say so explicitly.

    :return: list of QuerySeries, empty when the query matched nothing
    :raises SourceConnectionError: on a transport/parse failure
    """
    url, kwargs = _influx_read_request(influx_settings, db, query)
    payload = _get(session, url, kwargs, "query")
    found = []
    for result in payload.get("results", []):
        if result.get("error"):
            raise SourceConnectionError(f"InfluxDB rejected the query: {result['error']}")
        for series in result.get("series", []):
            found.append(
                QuerySeries(
                    tags=dict(series.get("tags") or {}),
                    columns=series.get("columns", []),
                    values=series.get("values", []),
                )
            )
    return found


def single_series(series):
    """Flatten a :func:`run_query` result that must hold at most one series.

    For queries that cannot produce more than one - no ``GROUP BY`` on a tag, so
    InfluxQL merges every tag value into a single series - this restores the plain
    ``(columns, values)`` shape.

    **Raises rather than truncating if the assumption is violated.** Silently
    keeping the first series is the exact defect this module was just fixed for, so
    re-introducing it behind a helper would defeat the change: a later edit adding a
    tag ``GROUP BY`` without updating its consumer would go back to losing data
    invisibly. Failing loudly turns that into an immediate, obvious error instead.

    The condition is unreachable today - verified against a real InfluxDB 1.8 that
    every current caller's query returns exactly one series, including the
    aggregation path's ``GROUP BY time(...)``, which splits rows rather than series.
    So the guard costs nothing now and only fires on a genuine programming error.
    ``ValueError`` matches ``_build_single_point_query``'s existing internal-guard
    idiom; it is not a caller- or transport-level failure and must not be mapped to
    ToolParamError or SourceConnectionError.

    :param series: list of QuerySeries from run_query
    :return: (columns, values), or ([], []) when there is no series
    :raises ValueError: if given more than one series
    """
    if not series:
        return [], []
    if len(series) > 1:
        raise ValueError(
            f"single_series() got {len(series)} series, expected at most one - the query "
            f"grouped by a tag, so its consumer must handle every series (tag sets: "
            f"{[s.tags for s in series]})"
        )
    return series[0].columns, series[0].values


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

    :param row: one row from a result series
    :param index: column name -> position mapping
    :param name: the column wanted
    :return: the value, or None when the column is absent or the row is too short
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

    :param duration: an InfluxDB duration string, or None
    :return: seconds as int, or None when there is nothing parseable
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

    :param seconds: a whole number of seconds, or None
    :return: a duration string, "infinite" for 0, or None when not a number
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

    :param influx_settings: the ``influx`` settings block (must have a token)
    :param bucket: the bucket name to filter to
    :return: (url, requests kwargs)
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

    :return: dict describing the retention, for the ``retention`` key of the payload
    :raises SourceConnectionError: transport, parse, or an InfluxDB-reported error
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

    :return: dict describing the retention, for the ``retention`` key of the payload
    :raises SourceConnectionError: transport, parse, or no such bucket
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
    """Read the retention configuration bounding how far back data could go, degrading to
    an explicit "not known" rather than failing the whole call.

    Retention is the *second* half of the data-range answer and the less important one: if
    it cannot be read, the earliest/latest range is still worth returning. So a failure
    here is reported in place - ``known: false`` with the reason - rather than raised.
    Reported rather than omitted on purpose: a missing ``retention`` key reads as "no
    retention configured", i.e. kept forever, which is the same misleading direction as
    v2's ``0s``.

    :return: dict for the payload's ``retention`` key, always with a ``known`` flag
    """
    try:
        if influx_settings.get("token"):
            return _v2_retention(session, influx_settings, db)
        return _v1_retention(session, influx_settings, db)
    except SourceConnectionError as exc:
        logging.warning("Could not read retention configuration for %r: %s", db, exc)
        return {"known": False, "reason": str(exc)}


def resolve_schema(source, settings, settings_file, instance=None):
    """Build a fully-populated ReadSchema for a source: its static class metadata
    plus the live field allowlist discovered from InfluxDB. Constructs a handler
    from current settings each call, so a live settings edit is picked up.

    ``instance`` scopes the schema to one target of an instanced source, which shows up as
    an extra tag filter (a Hue bridge's ``host``) - see ``DataHandler.mcp_tag_filters()``.
    ``None`` leaves the read unscoped, spanning every target, which is the right answer when
    the caller did not name one.

    :param instance: the instance to scope to, or None for all of them
    :raises ToolParamError: for an unknown/unusable source, or an instance that is not
        configured
    :raises SourceConnectionError: if field discovery fails
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


def _query_history_result(
    settings, settings_file, *, source, field, start, end, aggregation, group_by, limit, bridge=None
):
    """Build the query_history tool payload (runs in a worker thread).

    ``bridge`` scopes the query to one Hue bridge by adding its ``host`` tag to the filter.
    Left out, the query spans every bridge - deliberately, since Hue writes all of them to
    one measurement and an unqualified question about the estate should get an answer about
    the estate. The value never reaches the query as given: it is resolved against the
    configured bridges first, so an unknown one is refused.

    It is rejected outright for a source that has no instances. Such a source would accept
    the instance, ignore it (its tag filters do not vary), run an unscoped query - and then
    the result would echo ``bridge`` back, telling the caller the query was scoped when it
    was not. Refusing is the only honest answer.
    """
    # Only judge 'bridge' once the source is a usable name. A non-string would raise
    # AttributeError from .lower() here, escaping the ToolParamError/SourceConnectionError
    # contract the MCP layer uses to tell a caller mistake from a transport failure; a blank
    # one would be reported as a 'bridge' problem when the source is what is wrong. Either way
    # the condition matches resolve_handler()'s, so skipping the guard hands the value to that
    # validation and the message names the parameter actually at fault.
    usable_source = isinstance(source, str) and source.strip()
    if bridge is not None and usable_source and source.lower() not in INSTANCED_SOURCES:
        raise ToolParamError(
            f"'bridge' does not apply to source {source!r} - it has a single target. "
            f"Only these sources have separate targets to scope to: {', '.join(sorted(INSTANCED_SOURCES))}"
        )
    handler, schema = resolve_schema(source, settings, settings_file, instance=bridge)
    try:
        result = _run_query_history(handler, schema, field, start, end, aggregation, group_by, limit)
        # Say what was actually queried: without this the model cannot tell a single-bridge
        # answer from an estate-wide one, and the two mean different things.
        if bridge is not None:
            result["bridge"] = bridge
        return result
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
    # Ungrouped today, so exactly one series; the per-instance grouping that makes
    # this return several is the next slice, and changes this consumer with it.
    columns, values = single_series(run_query(handler.session, handler.settings["influx"], schema.db, query))
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
    query = build_latest_query(measurement, handler.mcp_tag_filters(), fields)
    columns, values = single_series(run_query(handler.session, influx_settings, db, query))
    if not values:
        return {}, None
    row = values[0]
    index = {col: i for i, col in enumerate(columns)}
    # Skip a field that came back NULL (a field written in some points but not the
    # latest one) rather than reporting a meaningless None as its current value.
    data = {name: _cell(row, index, name) for name in sorted(fields) if _cell(row, index, name) is not None}
    as_of = _cell(row, index, "time")
    return data, as_of


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

    :raises ToolParamError: unknown/unusable source
    :raises SourceConnectionError: a live get_data() or InfluxDB read failed for every
        instance
    """
    handlers = resolve_handlers(source, settings, settings_file)
    try:
        first = handlers[0][1]
        result = {"source": first.source, "state": "live" if first.MCP_LIVE_STATE else "last_recorded"}
        if first.MCP_DESCRIPTION:
            result["description"] = first.MCP_DESCRIPTION

        if len(handlers) == 1 and handlers[0][0] is None:
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
                + "; ".join(f"{instance}: {entry['error']}" for instance, entry in instances.items())
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

    :param handler: a constructed DataHandler subclass instance
    :return: (field name -> annotated value, unix seconds the state is as of)
    :rtype: tuple
    :raises SourceConnectionError: the live read or the InfluxDB read failed
    """
    if handler.MCP_LIVE_STATE:
        data = handler.get_data() or {}
        as_of = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    else:
        data, as_of = _latest_recorded(handler)
    field_metadata = handler.MCP_FIELD_METADATA
    return (
        {name: _annotate_state_field(field_metadata, name, value) for name, value in sorted(data.items())},
        as_of,
    )


def _edge_time(handler, schema, order_query):
    """Return the unix-seconds timestamp of one edge point, or None when there is no data.

    None covers every way the timestamp can be absent rather than wrong: no points matched,
    or the row came back without a usable ``time`` column (see :func:`_cell`). An InfluxDB
    transport failure or a server-side error still raises, because that is not the same thing
    as "there is no data" and must not be reported as an empty range.

    :param handler: constructed DataHandler (caller owns its session)
    :param schema: the ReadSchema for the source
    :param order_query: the built query (see :func:`build_edge_time_query`)
    :return: unix seconds as int, or None
    :raises SourceConnectionError: transport failure, unparseable response, or an
        InfluxDB-reported query error
    """
    columns, values = single_series(run_query(handler.session, handler.settings["influx"], schema.db, order_query))
    if not values:
        return None
    index = {col: i for i, col in enumerate(columns)}
    return _cell(values[0], index, "time")


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

    :param source: source name from a tool argument
    :param settings: parsed settings dict
    :param settings_file: settings path, threaded to the handler's own load
    :return: dict payload
    :raises ToolParamError: unknown or unusable source
    :raises SourceConnectionError: the InfluxDB range read failed (retention failure alone
        degrades to ``retention.known = false`` instead)
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
    """Register the read-only MCP tools on a MCPServer server: list the queryable
    sources, list a source's fields, and query a field's history. Blocking HTTP
    runs in a worker thread so the async event loop isn't stalled during an
    InfluxDB round trip.

    :param server: the MCPServer instance
    :param settings: the parsed settings dict
    :param settings_file: settings path, for re-resolving handlers per call
    """
    import anyio

    @server.tool(title="List Data Sources", annotations=_READ_ONLY)
    async def list_sources() -> dict:
        """List the collector sources whose history can be queried, each with its
        InfluxDB measurement.

        The entry point for reads and the only one needing no arguments: start
        here, then `list_fields` for a source's fields, then `query_history` to
        read them. Takes no parameters and returns every configured source; use
        `list_fields` when you already know the source and want its fields."""
        return await anyio.to_thread.run_sync(_list_sources_result, settings, settings_file)

    @server.tool(title="List Source Fields", annotations=_READ_ONLY)
    async def list_fields(source: str) -> dict:
        """List the field keys available for one source, each with any known unit
        and, for coded fields, what each numeric value means.

        Call this before `query_history`: a field name it did not list is
        rejected as an error, so use it to discover exact field names (they can
        contain spaces-as-underscores and punctuation). Use `list_sources`
        instead when you don't yet know which source you want. `source` is a
        source name from `list_sources`; an unknown one returns an error."""
        return await anyio.to_thread.run_sync(list_fields_result, source, settings, settings_file)

    @server.tool(title="Query Historical Data", annotations=_READ_ONLY)
    async def query_history(
        source: str,
        field: str,
        start: str = "-24h",
        end: str = "now",
        aggregation: str = "raw",
        group_by: "str | None" = None,
        limit: int = DEFAULT_RESULT_POINTS,
        bridge: "str | None" = None,
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
        - bridge: Hue only - rejected for any other source, which has a single target.
          Only useful with more than one bridge configured. Restricts the
          query to that bridge; omitted, the query covers every bridge, since they share one
          measurement. Two bridges can hold the same field name (a "Kitchen" per floor), so
          an unqualified query can mix them - pass `bridge` when the answer must be about
          one. `get_current_state` lists the configured bridges. The result echoes `bridge`
          when one was used, so a single-bridge answer is distinguishable from an
          estate-wide one.

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
                bridge=bridge,
            )
        )

    @server.tool(title="Get Current State", annotations=_READ_ONLY)
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

    @server.tool(title="Get Data Range & Retention", annotations=_READ_ONLY)
    async def get_data_range(source: str) -> dict:
        """Get how far back a source's data goes, and how long InfluxDB keeps it.

        Answers "how far back do my records go", "when did collection start", "how long is
        data kept". Use this before `query_history` when you don't know what range exists -
        history needs a range, this tells you what range is there. Unlike
        `get_current_state` (the present moment) this describes the whole span.

        `source` is a name from `list_sources`; an unknown one returns an error.

        Two different facts, both reported, because they answer different questions:

        - `earliest`/`latest` (unix seconds) and `span_seconds`: the oldest and newest
          points actually present - the real floor on what history can return.
          `points_present` is false when nothing has been collected yet, with the
          timestamps null.
        - `retention`: what InfluxDB is configured to keep, independent of what was
          collected. `duration` is a string like '720h0m0s', or 'infinite' when data is
          never expired, with `duration_seconds` alongside for arithmetic; v1 also reports
          the `policy` name, and both versions report the shard group duration.

        The two differ, and the difference is the point: an install collecting for three
        years with 30-day retention has 30 days of data, not three years.

        `retention.known` is false, with a `reason`, when the configuration could not be
        read - the range is still returned in that case rather than failing the call. It is
        reported rather than omitted so an unreadable retention is never mistaken for
        unlimited retention."""
        return await anyio.to_thread.run_sync(data_range_result, source, settings, settings_file)

    @server.tool(title="Get Field Documentation", annotations=_READ_ONLY)
    async def get_documentation() -> dict:
        """Get a reference for what every source reports and what its values mean -
        units, and the meaning of coded values (e.g. Nuki lock/door state codes).

        A good first call for orientation: it needs no arguments and no InfluxDB
        round trip, and covers all sources in one go (unlike `list_fields`, which
        is per-source and lists only the fields currently in InfluxDB). Returns
        `{format: 'markdown', content: ...}`."""
        return await anyio.to_thread.run_sync(_documentation_result, settings, settings_file)

    return server
