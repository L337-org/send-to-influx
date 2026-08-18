"""Parent class for data handlers to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import time
import logging
import warnings
from collections import deque
from itertools import islice
import urllib3
import requests
from toinflux.general import load_settings
from toinflux.exceptions import ConfigError


class InfluxWriteError(Exception):
    """
    Raised when a write to InfluxDB fails.

    :ivar status_code: the HTTP status code of the failed write, or None when no
        response was received at all (connection error/timeout). Defaults to None via
        a class-level fallback and is set as an instance attribute after construction
        (see _post_line) rather than via a custom __init__, so the exception's
        args/str() stay a plain single message.
    :vartype status_code: int or None
    """

    status_code = None


# Bound on how many failed points each source buffers in memory before the oldest is
# dropped to make room for new ones - see DataHandler._write_buffers.
MAX_BUFFERED_POINTS = 500

# How many times a buffered point may be *rejected* by the server (a non-transient 4xx -
# the server received it and said no) before it's dropped as unsendable. Connection
# failures, 5xx responses, and the transient 4xxs below never count towards this, so an
# ordinary outage - however long - can't age points out; only a point the server itself
# keeps refusing (malformed, outside the retention window, oversized, or a misbehaving
# middlebox answering for InfluxDB) is given up on, and even then only after this many
# separate attempts, so one transient 4xx (e.g. a proxy hiccup) doesn't discard data
# InfluxDB never saw.
MAX_POINT_REJECTIONS = 5

# 4xx statuses that describe a transient server/connection condition, not a verdict on
# the submitted payload: 408 Request Timeout, 429 Too Many Requests. Counting these as
# point rejections would age valid points out of the buffer during rate limiting.
TRANSIENT_CLIENT_ERRORS = frozenset({408, 429})

# How many buffered points are flushed per HTTP request. InfluxDB's write endpoints
# natively accept multiple newline-separated points per body, so recovering from a long
# outage costs a handful of requests instead of one per point; per-point posting is the
# fallback used only to isolate the offender when a whole chunk is rejected.
FLUSH_CHUNK_SIZE = 100


def _is_point_rejection(status_code):
    """True when an HTTP status code means the server received and rejected the submitted
    *payload* (a 4xx other than the transient 408/429) - as opposed to a connection
    failure (None), a server-side error (5xx), or a rate-limit/timeout condition that
    says nothing about the point's validity."""
    return status_code is not None and 400 <= status_code < 500 and status_code not in TRANSIENT_CLIENT_ERRORS


def _format_field_value(value):
    """
    Format a value as an InfluxDB line protocol field value.

    Booleans become ``true``/``false`` and strings are quoted with internal
    backslashes/quotes escaped. Numbers (including ints) are left as bare,
    unsuffixed values so they're always written as InfluxDB's float field
    type - deliberately not using the ``i`` integer suffix, since a field's
    type is fixed by its first write and existing databases already have
    these fields established as float.

    :param value: field value to format
    :return: line protocol representation of the value
    :rtype: str
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def escape_key_or_tag_value(value):
    """
    Escape a value for use as an InfluxDB line protocol key or tag value.

    Per the line protocol spec, commas, equals signs and spaces must be
    backslash-escaped in measurement/tag/field keys and tag values (field
    *values* follow different quoting rules, handled by _format_field_value).

    **A newline cannot be escaped** - the line protocol has no escape for one, because a
    newline is what separates points. A tag value containing one therefore terminates the
    point early and turns the remainder into a *second* point, which reached InfluxDB as
    data the operator never configured. Demonstrated with a Hue host or MyEnergi label of
    ``"Garage\nmyenergi,device=Injected fake=1"``.

    So this refuses rather than escapes. Callers should reject such a value at the
    configuration boundary, where the error can name the settings key at fault - this is the
    backstop that makes it impossible to reach a write by any route, present and future.

    :param value: key or tag value to escape
    :return: escaped line protocol representation
    :rtype: str
    :raises InfluxWriteError: the value contains a character that cannot appear in a tag
    """
    value = str(value)
    if any(char in value for char in "\n\r"):
        raise InfluxWriteError(
            f"a line protocol key or tag value cannot contain a newline (got {value!r}); "
            f"it would split the point in two"
        )
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def worker_label(source, instance=None):
    """
    Format a worker's identity for log messages: ``source`` or ``source@instance``.

    Display only - never parsed, never a dict key (that is ``DataHandler.worker_key``),
    and never an emitted tag value. Lives at module level as well as on the handler
    because the supervisor in ``sendtoinflux.py`` must label a worker before its handler
    has been constructed - two separate formatters would eventually disagree.

    Tests ``is not None`` rather than truthiness on purpose: only ``None`` means
    "single-target source, no instance". A blank-but-present instance is a
    misconfiguration, and rendering it as a bare source name would both hide that and
    disagree with ``worker_key``, which keeps the value verbatim - so the label shows it
    (as ``source@``) instead of silently swallowing it.

    An instance equal to the source name collapses to the bare source, because that is what
    a MyEnergi device's label defaults to on a legacy single-device install: without this
    every log line for such an install would read ``zappi@zappi``, changing the output an
    operator greps for no reason. ``worker_key`` is unaffected and keeps the instance, since
    it is an identity rather than a label.

    :param source: source name
    :type source: str
    :param instance: the worker's instance, or None for a single-target source
    :return: label for log output
    :rtype: str
    """
    if instance is None or instance == source:
        return f"{source}"
    return f"{source}@{instance}"


class DataHandler:
    """Class to send data to InfluxDB"""

    # Whether this source is event-driven over a held-open connection rather than
    # polled on a timer. False for every HTTP/API source (they have no persistent
    # connection to hold); set True by MqttDataHandler, whose broker subscription
    # lets it write a point the instant a message arrives. sendtoinflux.py's worker
    # branches on this: a streaming source runs its blocking stream loop
    # (stream_source_data) instead of the poll-then-sleep cycle. A property of the
    # transport, not a config option.
    STREAMING = False

    # --- MCP read schema (domain knowledge for the read-query tool) ---
    # The InfluxDB measurement this source writes to; None means "same as the
    # source name" (true for most sources - hue, speedtest, octopus, ...).
    # Overridden where they differ (openmeteo -> weather) or where several
    # sources share one measurement distinguished by a tag (the myenergi trio).
    MCP_MEASUREMENT: "str | None" = None
    # Tag key/value filters that disambiguate this source within a shared
    # measurement (e.g. {"device": "zappi"}); empty for a source that owns its
    # measurement outright.
    MCP_TAG_FILTERS: dict = {}
    # Field annotation for the read tool: maps a field key - or a _-delimited
    # suffix, for collectors with dynamic prefixes (Nuki's per-lock fields) - to
    # {"unit": str} and/or {"codes": {int: str}} for decoding numeric state
    # codes. Sourced from UNITS.md; kept here so the read tool is domain-aware
    # without a parallel schema to maintain.
    MCP_FIELD_METADATA: dict = {}
    # A short human description of what this source reports, surfaced by the MCP
    # read tools/resources (list_sources, the documentation tool, the per-source
    # resources) so the model knows what a source *is*, not just its name. Empty
    # on the base; every concrete source sets it.
    MCP_DESCRIPTION = ""
    # Whether the MCP current-state read may call this source's get_data() live
    # (a cheap API/MQTT read for most sources). False where a live read is
    # expensive or pointless: Speedtest (get_data() runs a full download/upload)
    # and Octopus (data is ~24 h delayed, so the API is no fresher than InfluxDB)
    # - for those, current-state reads the latest recorded point from InfluxDB
    # instead of ever calling get_data().
    MCP_LIVE_STATE = True
    # Whether this source implements a write/control path the MCP server can
    # expose. A subclass with MCP_WRITABLE = True provides its own vendor write
    # method(s) - the shape is per source, e.g. Hue's mcp_set_device_state()/
    # mcp_list_writable_devices() or Speedtest's mcp_trigger_run() - and is wired
    # to its own bespoke tool(s) by a per-source registrar in
    # _WRITE_TOOL_REGISTRARS (toinflux/mcp_write.py). Even for a writable source,
    # the write tools are only registered when the operator also opts in per
    # source via `<source>.mcp_read_write: true` - see mcp_write_enabled(). A
    # disabled capability isn't registered at all (least privilege), never
    # registered-and-refusing.
    MCP_WRITABLE = False
    # The tag key that distinguishes *producers* within this source's measurement,
    # or None when the measurement has only one. This is the tag as an **axis** -
    # something to enumerate and scope by - as opposed to MCP_TAG_FILTERS above,
    # which pins a tag to one constant value. The distinction is the whole point:
    # Speedtest deliberately tags every point with the collecting host, and Grafana
    # separates them, but the MCP read tools used to flatten every host into one
    # unlabelled series - so a two-host install got answers that silently mixed
    # them. Naming the axis is what lets a read enumerate it, scope to one value,
    # and report per value.
    #
    # It is deliberately per source rather than one global "collector" tag: the axis
    # means different things (Speedtest's collecting host, a Hue bridge, a Nuki
    # lock, a MyEnergi device), and most sources genuinely have only one producer.
    MCP_INSTANCE_TAG: "str | None" = None
    # Whether one live get_data() covers *every* producer of this source, or only the one
    # this handler serves. Three shapes exist and they are genuinely different: Speedtest
    # reads live but can only speak for the local host; Hue reads live per bridge, each
    # bridge having its own handler; Nuki reads every lock over one MQTT subscription, so a
    # single handler's live read covers them all. Only the third can report per instance
    # from a live read, which is what this distinguishes.
    MCP_LIVE_STATE_COVERS_ALL_INSTANCES = False

    def mcp_tag_filters(self):
        """Return the tag filters that scope this handler's reads.

        The class's static ``MCP_TAG_FILTERS`` by default (never model input). A subclass
        with instances overrides this to add a per-instance tag, so a read can be scoped to
        one target instead of merging them - Hue adds its bridge's ``host``, matching the
        tag its own writes carry.

        A method rather than the bare class attribute because the answer depends on *which*
        instance this handler serves, which a class attribute cannot express.

        :return: tag key/value filters for this handler's reads
        :rtype: dict
        """
        return dict(self.MCP_TAG_FILTERS)

    def heartbeat_tags(self):
        """Return extra tags for this handler's ``collector_status`` heartbeat.

        The heartbeat must be distinguishable per *writer*, or several writers overwrite
        one another's ok/consecutive_failures at second precision and a dead one is
        invisible - which is exactly what happened with two Speedtest hosts sharing
        ``collector_status,source=speedtest``.

        The base answer covers an instanced source, tagging its own instance (a Hue
        bridge). A source whose producers are separate *processes* rather than separate
        targets has ``instance`` None and overrides this instead - see Speedtest, which
        tags the collecting machine. Either way the tag has to match what the source's own
        data carries, or the health series and the measurement disagree about who wrote
        what.

        :return: extra tag key/value pairs, empty for a single-writer source
        :rtype: dict
        """
        if self.instance is not None:
            return {"host": self.instance}
        return {}

    def mcp_write_enabled(self):
        """Return True only when this source is writable *and* the operator has
        opted in with ``<source>.mcp_read_write: true`` (strict ``is True``, so a
        stray truthy string like ``"true"`` doesn't silently enable device
        control). The default is off - writes are opt-in per source."""
        return self.MCP_WRITABLE and self.source_settings.get("mcp_read_write", False) is True

    # Bounded per-worker buffer of points that failed to write, flushed on the next
    # successful send. Each entry is a mutable [line, rejection_count] pair - the count
    # tracks how many times the server has rejected (4xx) that specific point, so
    # _flush_buffer can give up on it after MAX_POINT_REJECTIONS. Class-level (shared
    # across instances/subclasses) rather than an instance attribute: the worker loop in
    # sendtoinflux.py discards and reconstructs the DataHandler instance after every
    # failure, so only a buffer that outlives the instance survives to be flushed later.
    #
    # Keyed by ``worker_key`` - (source, instance) - NOT by source name alone. A source
    # with several instances (a Hue install with more than one bridge) runs one worker
    # thread per instance, and they must not share a deque: _flush_buffer/_flush_head do
    # read-then-popleft sequences that are not atomic across threads, so a shared buffer
    # could double-post or lose points. validate_settings() refuses duplicate `sources:`
    # entries for exactly that reason; per-instance keys are what make several workers on
    # one source name safe. Mutating this dict from several threads is fine as it stands:
    # a single dict.setdefault() is atomic under the GIL, and each worker only ever
    # touches its own deque afterwards.
    #
    # deque(maxlen=...) evicts the oldest buffered point once a worker's buffer is full,
    # so a very long outage degrades gracefully instead of growing memory without bound -
    # note the bound is per worker, so N instances of a source can hold up to N *
    # MAX_BUFFERED_POINTS between them. Buffered lines are flushed to whatever
    # destination the *current* settings resolve to - an accepted limitation: editing
    # influx.url/bucket/db while a backlog exists re-routes that backlog to the new
    # destination.
    _write_buffers: dict = {}

    def __init__(self, source=None, settings_file=None, instance=None):
        self.settings = load_settings(settings_file)
        self.source = source
        # Which instance of the source this handler serves, for a source that can have
        # more than one target behind a single settings block (currently only Hue, whose
        # instance is a bridge host). None for every single-target source, which keeps
        # their worker_key/worker_label - and therefore their buffering, heartbeat and
        # log output - exactly as they were before instances existed. Deliberately
        # separate from self.source rather than folded into it: self.source is also the
        # settings-block key, the get_class() lookup name, the heartbeat's `source` tag
        # and the MCP measurement fallback, none of which vary per instance.
        self.instance = instance
        self.influx_header = None
        self.data = None
        self.timestamp = None
        self.session = requests.Session()

        if self.source and self.source in self.settings:
            self.source_settings = self.settings[self.source]
        else:
            raise ConfigError(f"Source {self.source} not found in settings")

    @property
    def worker_key(self):
        """
        Identity of the worker this handler belongs to, as ``(source, instance)``.

        Used to key anything that is per *worker* rather than per source: the write
        buffer here, and the supervisor's activity/stopped/stalled bookkeeping in
        ``sendtoinflux.py``. A tuple rather than a joined string on purpose - an
        instance may be an IPv6 literal, so any ``source@host`` form would be
        ambiguous to a later split on ``:``, and callers that need the settings-block
        name (e.g. reading ``settings[source]["interval"]``) must be able to take it
        back out without re-parsing.

        :return: (source name, instance or None)
        :rtype: tuple
        """
        return (self.source, self.instance)

    @property
    def worker_label(self):
        """
        Human-readable identity for log messages: ``source`` or ``source@instance``.

        Display only - never parsed, and never used as a dict key or an emitted tag
        value (see ``worker_key``). Delegates to the module-level ``worker_label()`` so
        that the supervisor in ``sendtoinflux.py``, which has to label a worker before its
        handler exists, formats it identically.

        :rtype: str
        """
        return worker_label(self.source, self.instance)

    def send_data(self, data=None, timestamp=None, use_buffer=True):
        """
        Sends data to influxDB.

        Before sending the new point, first tries to flush any points buffered from
        earlier failed writes to this source (oldest first) - see ``_write_buffers``.
        The flush happens even when this call has no data of its own, so a recovered
        source with a legitimately-empty reading still delivers its backlog. If the
        new point (or a buffered one) fails to send, it's appended to the buffer
        instead of being dropped, so a brief InfluxDB outage delays data rather than
        losing it. Either way, a failure still raises ``InfluxWriteError`` so the
        existing worker backoff/retry behaviour is unaffected.

        :param data: data to send to InfluxDB
        :type data: dict
        :param timestamp: unix epoch seconds to write the point at (matching the
            ``precision=s`` write parameter below). Defaults to ``self.timestamp``
            (set by some handlers' ``get_data()`` to the time of collection, e.g.
            a reading's own interval start) and falls back to the current time.
        :type timestamp: int or None
        :param use_buffer: when False, skip the backlog flush and don't buffer this
            point on failure - just POST it and raise if that fails. Used for
            fire-and-forget writes with no replay value (the collector_status
            heartbeat), which would otherwise consume buffer capacity that belongs
            to real measurements.
        :type use_buffer: bool
        :return: None
        :raises InfluxWriteError: if the write to InfluxDB fails
        """
        # if the data is not provided, use the data from the class
        if data is None:
            data = self.data

        if not data or not isinstance(data, dict):
            data_to_send = None
            if not self._log_missing_data(data, use_buffer):
                return
        else:
            if timestamp is None:
                timestamp = self.timestamp if self.timestamp is not None else int(time.time())
            data_to_send = (
                self.influx_header
                + ",".join(
                    f"{escape_key_or_tag_value(key)}={_format_field_value(value)}" for key, value in data.items()
                )
                + f" {timestamp}"
            )

        url, post_kwargs = self._build_write_request(self.settings["influx"])

        if not use_buffer:
            self._post_line(data_to_send, url, post_kwargs)
            return

        buffer = self._write_buffers.setdefault(self.worker_key, deque(maxlen=MAX_BUFFERED_POINTS))
        try:
            self._flush_buffer(buffer, url, post_kwargs)
            if data_to_send is not None:
                self._post_line(data_to_send, url, post_kwargs)
        except InfluxWriteError:
            if data_to_send is not None:
                self._buffer_point(buffer, data_to_send)
            raise

    def _log_missing_data(self, data, use_buffer):
        """
        Log appropriately for a send_data() call with no usable data of its own.

        A truthy non-dict isn't an empty reading, it's a handler bug - it gets its own
        explicit warning rather than hiding behind the no-data messages. An empty
        reading only warrants a warning when there's also no backlog to flush; a cycle
        that exists purely to drain the backlog logs at DEBUG.

        :param data: whatever the caller supplied (or self.data resolved to)
        :param use_buffer: the send_data() call's use_buffer flag
        :type use_buffer: bool
        :return: True when a backlog flush should still proceed, False when there is
            nothing at all for this call to do
        :rtype: bool
        """
        has_backlog = bool(use_buffer and self._write_buffers.get(self.worker_key))
        if data and not isinstance(data, dict):
            logging.warning("Ignoring non-dict data (%s) from worker '%s'", type(data).__name__, self.worker_label)
        elif not has_backlog:
            logging.warning("No data to send to InfluxDB")
        if not has_backlog:
            return False
        logging.debug("No new data for worker '%s'; flushing the buffered backlog only", self.worker_label)
        return True

    def _flush_buffer(self, buffer, url, kwargs):
        """
        Flush a source's buffered points, oldest first, in newline-joined chunks of
        FLUSH_CHUNK_SIZE per HTTP request (InfluxDB's write endpoints accept multi-point
        bodies natively, so a large backlog costs a handful of requests, not one each).

        A connection failure or 5xx stops the flush and re-raises, leaving everything
        in the buffer to retry next cycle - those failures say nothing about the points
        themselves, so they never count against them. A 4xx (the server received the
        chunk and rejected it) triggers a per-point pass over that chunk to isolate the
        offender(s): each rejected point's rejection count is incremented, and a point
        is only dropped - with a warning - once the server has rejected it
        MAX_POINT_REJECTIONS separate times, so neither a transiently-misbehaving
        middlebox answering 4xx for a down InfluxDB nor one bad point can cause
        unbounded loss or unbounded head-of-line blocking.

        :param buffer: the source's buffer (from ``_write_buffers``)
        :type buffer: collections.deque
        :param url: destination InfluxDB write URL
        :type url: str
        :param kwargs: extra requests.Session.post() kwargs (auth/headers/verify/timeout)
        :type kwargs: dict
        :return: None
        :raises InfluxWriteError: on a connection/5xx failure, or on a 4xx-rejected
            point that hasn't yet reached MAX_POINT_REJECTIONS
        """
        while buffer:
            # islice iterates the deque linearly - indexing a deque is O(n) per access,
            # which would make building the chunk O(k^2).
            chunk = list(islice(buffer, FLUSH_CHUNK_SIZE))
            if len(chunk) == 1:
                self._flush_head(buffer, url, kwargs)
                continue
            try:
                self._post_line("\n".join(entry[0] for entry in chunk), url, kwargs)
            except InfluxWriteError as exc:
                if not _is_point_rejection(exc.status_code):
                    logging.warning(
                        "Flushing %d buffered point(s) for worker '%s' failed; will retry next cycle",
                        len(buffer),
                        self.worker_label,
                    )
                    raise
                # The server rejected the chunk - isolate the offending point(s).
                for _ in chunk:
                    self._flush_head(buffer, url, kwargs)
                continue
            for _ in chunk:
                buffer.popleft()

    def _flush_head(self, buffer, url, kwargs):
        """
        POST the single point at the head of the buffer, removing it on success or -
        after MAX_POINT_REJECTIONS separate server rejections - dropping it with a
        warning. Any other failure re-raises with the point left in place.

        :param buffer: the source's buffer (from ``_write_buffers``)
        :type buffer: collections.deque
        :param url: destination InfluxDB write URL
        :type url: str
        :param kwargs: extra requests.Session.post() kwargs (auth/headers/verify/timeout)
        :type kwargs: dict
        :return: None
        :raises InfluxWriteError: on a connection/5xx failure, or a 4xx rejection
            below the MAX_POINT_REJECTIONS cap
        """
        entry = buffer[0]
        try:
            self._post_line(entry[0], url, kwargs)
        except InfluxWriteError as exc:
            if _is_point_rejection(exc.status_code):
                entry[1] += 1
                if entry[1] >= MAX_POINT_REJECTIONS:
                    logging.warning(
                        "Dropping buffered point for worker '%s' after %d server rejections: %s",
                        self.worker_label,
                        entry[1],
                        exc,
                    )
                    buffer.popleft()
                    return
            raise
        buffer.popleft()

    def _build_write_request(self, influx_settings):
        """
        Build the URL/kwargs used to POST a line-protocol body to this source's InfluxDB
        target. Independent of any one point's content, so it's computed once per
        ``send_data()`` call and reused for every line posted during that call (any
        flushed backlog plus the new point).

        :param influx_settings: the ``influx`` settings block
        :type influx_settings: dict
        :return: (url, kwargs for requests.Session.post())
        :rtype: tuple
        """
        timeout = influx_settings.get("timeout", 5)
        if influx_settings.get("token"):
            url = (
                f'{influx_settings["url"]}/api/v2/write'
                f'?org={influx_settings["org"]}'
                f'&bucket={self.source_settings.get("bucket", self.source_settings.get("db"))}'
                f"&precision=s"
            )
            headers = {"Authorization": f'Token {influx_settings["token"]}'}
            kwargs = {"headers": headers}
        else:
            url = f'{influx_settings["url"]}/write?db={self.source_settings["db"]}&precision=s'
            kwargs = {"auth": (influx_settings["user"], influx_settings["password"])}

        kwargs["verify"] = not influx_settings.get("insecure", False)
        kwargs["timeout"] = timeout
        return url, kwargs

    def _post_line(self, line, url, kwargs):
        """
        POST a line-protocol body (one point, or several newline-joined) to InfluxDB.

        :param line: line-protocol body to send
        :type line: str
        :param url: destination InfluxDB write URL
        :type url: str
        :param kwargs: extra requests.Session.post() kwargs (auth/headers/verify/timeout)
        :type kwargs: dict
        :return: None
        :raises InfluxWriteError: if the write to InfluxDB fails; carries the response's
            HTTP status code (or None for a connection failure) as ``status_code``
        """
        try:
            with warnings.catch_warnings():
                if not kwargs.get("verify", True):
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.post(url, data=line, **kwargs)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error("Error sending data to InfluxDB - %s", e)
            exc = InfluxWriteError(str(e))
            exc.status_code = getattr(e.response, "status_code", None)
            raise exc from e

    def _buffer_point(self, buffer, line):
        """
        Append a failed point to a source's write buffer as a fresh
        ``[line, rejection_count]`` entry, warning if this evicts the oldest buffered
        point because the buffer was already full. An identical line already in the
        buffer is not added again - some sources (Octopus) re-serve the same reading
        with the same timestamp for many collection cycles, and duplicate copies would
        only waste capacity, since flushing them is an idempotent overwrite anyway.

        :param buffer: the source's buffer (from ``_write_buffers``)
        :type buffer: collections.deque
        :param line: line-protocol point that failed to send
        :type line: str
        :return: None
        """
        if any(entry[0] == line for entry in buffer):
            logging.debug("Point already buffered for worker '%s'; not buffering a duplicate copy", self.worker_label)
            return
        if len(buffer) >= buffer.maxlen:
            logging.warning(
                "InfluxDB write buffer for worker '%s' is full (%d points); dropping the oldest buffered point",
                self.worker_label,
                buffer.maxlen,
            )
        buffer.append([line, 0])
