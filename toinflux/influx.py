"""Parent class for data handlers, and the InfluxDB write and read primitives.

``DataHandler`` is the base every collector subclasses. It owns the write side: line
protocol encoding, the write request, and the per-source buffering that keeps points
alive across an outage rather than dropping them. The polling that drives it lives in
sendtoinflux.py's worker rather than here.

The module also owns reading back - the query builders, the live field and tag
discovery, and the identifier validation and quoting they rest on - so that code with
no interest in the MCP server can ask what is in the database without importing one.
The "Reading back" banner below says why that half lives here and what must never be
routed around.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import re
import time
import logging
import warnings
from collections import deque
from itertools import islice
import urllib3
import requests
from dataclasses import dataclass
from toinflux.exceptions import SourceConnectionError, ToInfluxError, ToolParamError
from toinflux.general import load_settings
from toinflux.exceptions import ConfigError


class InfluxWriteError(ToInfluxError):
    """Raised when a write to InfluxDB fails.

    Attributes:
        status_code (int or None): the HTTP status code of the failed write, or None when no response was received at
            all (connection error/timeout). Defaults to None via a class-level fallback and is set as an instance
            attribute after construction (see _post_line) rather than via a custom __init__, so the exception's
            args/str() stay a plain single message.
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
    """True when a status code means the server received and rejected the *payload*.

    A 4xx other than the transient 408/429, as opposed to a connection failure (None), a
    server-side error (5xx), or a rate-limit or timeout condition that says nothing about
    the point's validity.

    Args:
        status_code (int or None): the HTTP status the write returned, or None if the
            connection failed before one arrived

    Returns:
        bool: True when the payload itself was rejected
    """
    return status_code is not None and 400 <= status_code < 500 and status_code not in TRANSIENT_CLIENT_ERRORS


def _format_field_value(value):
    """Format a value as an InfluxDB line protocol field value.

    Booleans become ``true``/``false`` and strings are quoted with internal
    backslashes/quotes escaped. Numbers (including ints) are left as bare,
    unsuffixed values so they're always written as InfluxDB's float field
    type - deliberately not using the ``i`` integer suffix, since a field's
    type is fixed by its first write and existing databases already have
    these fields established as float.

    Args:
        value (bool, str, int or float): field value to format

    Returns:
        str: line protocol representation of the value
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def escape_key_or_tag_value(value):
    r"""Escape a value for use as an InfluxDB line protocol key or tag value.

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

    Args:
        value (str): key or tag value to escape

    Returns:
        str: escaped line protocol representation

    Raises:
        InfluxWriteError: the value contains a character that cannot appear in a tag
    """
    value = str(value)
    if any(char in value for char in "\n\r"):
        raise InfluxWriteError(
            f"a line protocol key or tag value cannot contain a newline (got {value!r}); "
            f"it would split the point in two"
        )
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def worker_label(source, instance=None):
    """Format a worker's identity for log messages: ``source`` or ``source@instance``.

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

    Args:
        source (str): source name
        instance (str or None): the worker's instance, or None for a single-target source

    Returns:
        str: this handler's label for log output
    """
    if instance is None or instance == source:
        return f"{source}"
    return f"{source}@{instance}"


class DataHandler:
    """Class to send data to InfluxDB.

    The base every collector subclasses. Each attribute below is a default a subclass
    overrides only where it differs; the comment beside each declaration says why it exists,
    and these entries say what it means and what the default is.

    Attributes:
        STREAMING (bool): False - whether this source is event-driven over a held-open
            connection rather than polled on a timer. A property of the transport.
        MCP_MEASUREMENT (str or None): None, meaning the measurement is the source's own
            name. Set where they differ, or where sources share one measurement.
        MCP_TAG_FILTERS (dict): empty - tag key/value filters that pick this source out of a
            measurement several sources write to.
        MCP_FIELD_METADATA (dict): empty - per-field unit, coded values, aggregation kind and
            description for the read tools. See the comment below for the accepted keys.
        MCP_DESCRIPTION (str): empty - the one-line description this source advertises to an
            MCP client.
        MCP_LIVE_STATE (bool): True - whether a current-state read may call ``get_data()``
            live. False where a live read is expensive or no fresher than InfluxDB.
        MCP_WRITABLE (bool): False - whether this source offers a write action at all. The
            operator still has to opt in per source; see ``mcp_write_enabled()``.
        MCP_INSTANCE_TAG (str or None): None for a single-target source - the tag naming which
            instance produced a point, where one source has several.
        MCP_LIVE_STATE_COVERS_ALL_INSTANCES (bool): False - whether one live read returns every
            instance, rather than needing one read per instance.
    """

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
    # Field annotation for the read tools: maps a field key - or a _-delimited
    # suffix, for collectors with dynamic prefixes (Nuki's per-lock fields) - to
    # any of:
    #   "unit"        display unit, e.g. "W", "kWh", "°C". Omitted where a field
    #                 genuinely has none (a flag, a text label, a status code).
    #   "codes"       {int: str} meanings for a numeric-coded field, so a state
    #                 reads back as its label rather than a bare number.
    #   "kind"        how the value may be aggregated - one of mcp_read's
    #                 FIELD_KINDS ("gauge"/"interval"/"counter"/"state"). The one
    #                 that is not derivable from the value: nothing else
    #                 distinguishes a cumulative total, whose mean is a
    #                 plausible-looking number that means nothing, from an
    #                 instantaneous reading.
    #   "description" what the field is, *only* where its name, unit and coded
    #                 values do not already say. One that restates the name costs
    #                 context on every detailed call and conveys nothing, so a
    #                 self-describing field (openmeteo's temperature_2m,
    #                 speedtest's download) deliberately has none.
    # Every declared entry carries at least one of unit/codes/description, and a
    # kind; tests/test_field_metadata.py enforces both, and that the units and
    # coded values agree with UNITS.md. Kept here rather than in a parallel schema
    # so the read tools, the generated reference and the resources cannot drift.
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

        Returns:
            dict: tag key/value filters for this handler's reads
        """
        return dict(self.MCP_TAG_FILTERS)

    def mcp_field_metadata(self):
        """Return this source's field metadata, keyed by field key.

        The declared ``MCP_FIELD_METADATA`` for every source but one. It is a method
        rather than a bare attribute read so a source whose field keys cannot be
        tabulated in advance can resolve them per install - Hue's are the operator's
        own device names, so no static table can cover them.

        **An override must be best-effort and must never raise.** Callers treat metadata
        as an annotation: a field with none is listed with no unit, which is a smaller
        failure than a schema call that errors. Hue's override therefore degrades to the
        static table if its lookup fails.

        Called by the read layer's schema path (``build_schema``), not by
        ``build_documentation`` - the generated reference promises to need no InfluxDB
        round trip, so it keeps reading the class attribute directly and a source with
        only per-install metadata is simply absent from it.

        Returns:
            dict: {field key: {"unit"/"codes"/"kind"/"description"}}
        """
        return dict(self.MCP_FIELD_METADATA)

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

        Returns:
            dict: extra tag key/value pairs, empty for a single-writer source
        """
        if self.instance is not None:
            return {"host": self.instance}
        return {}

    def mcp_write_enabled(self):
        """Return True only when this source is writable and the operator has opted in.

        Opting in means ``<source>.mcp_read_write: true``, tested with a strict ``is
        True`` so a stray truthy string like ``"true"`` does not silently enable device
        control. The default is off - writes are opt-in per source.

        Returns:
            bool: True when this source is writable and the operator has opted in
        """
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
        """Build a handler for one source, and optionally one instance of it.

        Args:
            source (str or None): The source name, or None for the base handler.
            settings_file (str or None): Path to settings.yaml, or None for the default location.
            instance (str or None): Which instance of the source this serves, where a source can have
                more than one - a Hue bridge host, say. None for a single-target source.

        Raises:
            ConfigError: ``source`` names no section in the loaded settings
        """
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
        """Identity of the worker this handler belongs to, as ``(source, instance)``.

        Used to key anything that is per *worker* rather than per source: the write
        buffer here, and the supervisor's activity/stopped/stalled bookkeeping in
        ``sendtoinflux.py``. A tuple rather than a joined string on purpose - an
        instance may be an IPv6 literal, so any ``source@host`` form would be
        ambiguous to a later split on ``:``, and callers that need the settings-block
        name (e.g. reading ``settings[source]["interval"]``) must be able to take it
        back out without re-parsing.

        Returns:
            tuple: (source name, instance or None)
        """
        return (self.source, self.instance)

    @property
    def worker_label(self):
        """Human-readable identity for log messages: ``source`` or ``source@instance``.

        Display only - never parsed, and never used as a dict key or an emitted tag
        value (see ``worker_key``). Delegates to the module-level ``worker_label()`` so
        that the supervisor in ``sendtoinflux.py``, which has to label a worker before its
        handler exists, formats it identically.

        Returns:
            str: this worker's label for log output
        """
        return worker_label(self.source, self.instance)

    def send_data(self, data=None, timestamp=None, use_buffer=True, flush=True) -> None:
        """Sends data to influxDB.

        Before sending the new point, first tries to flush any points buffered from
        earlier failed writes to this source (oldest first) - see ``_write_buffers``.
        The flush happens even when this call has no data of its own, so a recovered
        source with a legitimately-empty reading still delivers its backlog. If the
        new point (or a buffered one) fails to send, it's appended to the buffer
        instead of being dropped, so a brief InfluxDB outage delays data rather than
        losing it. Either way, a failure still raises ``InfluxWriteError`` so the
        existing worker backoff/retry behaviour is unaffected.

        Args:
            data (dict or None): data to send to InfluxDB
            timestamp (int or None): unix epoch seconds to write the point at (matching the ``precision=s`` write
                parameter below). Defaults to ``self.timestamp`` (set by some handlers' ``get_data()`` to the time of
                collection, e.g. a reading's own interval start) and falls back to the current time.
            use_buffer (bool): when False, skip the backlog flush and don't buffer this point on failure - just POST it
                and raise if that fails. Used for fire-and-forget writes with no replay value (the collector_status
                heartbeat), which would otherwise consume buffer capacity that belongs to real measurements.
            flush (bool): when False, post this point without first flushing the backlog. Exists for a source that
                writes several points per collection cycle through this method - Nuki writes one per lock - because the
                write buffer is per *worker*, not per point: flushing on every point charged the head buffered point one
                rejection per point, so a five-lock install burned all of ``MAX_POINT_REJECTIONS`` in a single cycle and
                discarded the backlog after one, instead of surviving five. Such a caller flushes on its first point and
                passes False for the rest. Ignored when ``use_buffer`` is False, which skips the buffer entirely.

        Raises:
            InfluxWriteError: if the write to InfluxDB fails
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

        self._send_buffered(data_to_send, url, post_kwargs, flush)

    def _send_buffered(self, data_to_send, url, post_kwargs, flush):
        """Flush the backlog then post this point, buffering it if either fails.

        Split out of ``send_data`` only to keep that method within the project's cyclomatic
        complexity limit; the behaviour is unchanged.

        Args:
            data_to_send (str or None): the line protocol point, or None for a flush-only call
            url (str): the write URL from _build_write_request
            post_kwargs (dict): the request kwargs from _build_write_request
            flush (bool): whether to flush the backlog first - see send_data

        Raises:
            InfluxWriteError: the flush or the post failed
        """
        buffer = self._write_buffers.setdefault(self.worker_key, deque(maxlen=MAX_BUFFERED_POINTS))
        try:
            if flush:
                self._flush_buffer(buffer, url, post_kwargs)
            if data_to_send is not None:
                self._post_line(data_to_send, url, post_kwargs)
        except InfluxWriteError:
            if data_to_send is not None:
                self._buffer_point(buffer, data_to_send)
            raise

    def _log_missing_data(self, data, use_buffer):
        """Log appropriately for a send_data() call with no usable data of its own.

        A truthy non-dict isn't an empty reading, it's a handler bug - it gets its own
        explicit warning rather than hiding behind the no-data messages. An empty
        reading only warrants a warning when there's also no backlog to flush; a cycle
        that exists purely to drain the backlog logs at DEBUG.

        Args:
            data (dict or None): whatever the caller supplied (or self.data resolved to)
            use_buffer (bool): the send_data() call's use_buffer flag

        Returns:
            bool: True when a backlog flush should still proceed, False when there is nothing at all for this call to do
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
        """Flush a source's buffered points, oldest first.

        Sends newline-joined chunks of FLUSH_CHUNK_SIZE per HTTP request. InfluxDB's write
        endpoints accept multi-point bodies natively, so a large backlog costs a handful
        of requests rather than one each.

        A connection failure or 5xx stops the flush and re-raises, leaving everything
        in the buffer to retry next cycle - those failures say nothing about the points
        themselves, so they never count against them. A 4xx (the server received the
        chunk and rejected it) triggers a per-point pass over that chunk to isolate the
        offender(s): each rejected point's rejection count is incremented, and a point
        is only dropped - with a warning - once the server has rejected it
        MAX_POINT_REJECTIONS separate times, so neither a transiently-misbehaving
        middlebox answering 4xx for a down InfluxDB nor one bad point can cause
        unbounded loss or unbounded head-of-line blocking.

        Args:
            buffer (collections.deque): the source's buffer (from ``_write_buffers``)
            url (str): destination InfluxDB write URL
            kwargs (dict): extra requests.Session.post() kwargs (auth/headers/verify/timeout)

        Raises:
            InfluxWriteError: on a connection/5xx failure, or on a 4xx-rejected point that hasn't yet reached
                MAX_POINT_REJECTIONS
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

    def _flush_head(self, buffer, url, kwargs) -> None:
        """POST the single point at the head of the buffer.

        Removes it on success, or drops it with a warning after MAX_POINT_REJECTIONS
        separate server rejections. Any other failure re-raises with the point left in
        place.

        Args:
            buffer (collections.deque): the source's buffer (from ``_write_buffers``)
            url (str): destination InfluxDB write URL
            kwargs (dict): extra requests.Session.post() kwargs (auth/headers/verify/timeout)

        Raises:
            InfluxWriteError: on a connection/5xx failure, or a 4xx rejection below the MAX_POINT_REJECTIONS cap
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
        """Build the URL and kwargs for POSTing line protocol to this source's InfluxDB.

        Independent of any one point's content, so it is computed once per ``send_data()``
        call and reused for every line posted during that call - any flushed backlog plus
        the new point.

        Args:
            influx_settings (dict): the ``influx`` settings block

        Returns:
            tuple: (url, kwargs for requests.Session.post())
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
        """POST a line-protocol body (one point, or several newline-joined) to InfluxDB.

        Args:
            line (str): line-protocol body to send
            url (str): destination InfluxDB write URL
            kwargs (dict): extra requests.Session.post() kwargs (auth/headers/verify/timeout)

        Raises:
            InfluxWriteError: if the write to InfluxDB fails; carries the response's HTTP status code (or None for a
                connection failure) as ``status_code``
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

    def _buffer_point(self, buffer, line) -> None:
        """Append a failed point to a source's write buffer.

        Added as a fresh ``[line, rejection_count]`` entry, warning if this evicts the
        oldest buffered point because the buffer was already full. An identical line
        already in the buffer is not added again - some sources (Octopus) re-serve the
        same reading with the same timestamp for many collection cycles, and duplicate
        copies would only waste capacity, since flushing them is an idempotent overwrite
        anyway.

        Args:
            buffer (collections.deque): the source's buffer (from ``_write_buffers``)
            line (str): line-protocol point that failed to send
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


# --------------------------------------------------------------------------- #
# Reading back
#
# Everything below answers "what is in the database", as distinct from the write
# path above. It lived in toinflux/mcp_read.py until the control work needed it:
# a control process reads its inputs from InfluxDB and is meant to run with the
# MCP server absent entirely, so importing that module would have pulled the MCP
# SDK - mcp, anyio, pydantic, starlette and uvicorn - into every control just to
# ask what the last temperature reading was.
#
# Reading from InfluxDB was never an MCP concern; it was simply needed there
# first. Its tests came with it in name only: they are still in
# tests/test_mcp_read.py, not tests/test_influx.py where the write path's are.
#
# The injection defence is split, and knowing which half is here matters. What
# lives here: a measurement and its tags come from static schema, a field must
# match a live-discovered key, and every identifier is charset-validated and
# quoted before it reaches a query string. What did not move: time bounds are
# parsed and re-emitted as RFC3339 by mcp_read.parse_time_bound, and the
# aggregation map is there too, because both describe what the MCP tools accept
# rather than how a query is built.
#
# Never add a query path that goes around the half that is here.
# --------------------------------------------------------------------------- #

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


def resolve_db(source_settings, influx_settings):
    """Return the database or bucket name the collector actually writes to.

    Matches ``DataHandler._build_write_request()`` exactly: v2 (``influx.token`` set) uses
    ``bucket`` falling back to ``db``; v1 uses ``db`` only, ignoring ``bucket``.

    Mirroring the write path matters because a config can carry both keys - e.g.
    a stale ``bucket`` left after switching v2->v1 - and picking ``bucket`` in v1
    mode would send reads to a different database than the collectors write to.

    Args:
        source_settings (dict): the source's own settings block
        influx_settings (dict): the ``influx`` block (its ``token`` selects the mode)

    Returns:
        str or None: the db or bucket name, or None when neither is set
    """
    if influx_settings.get("token"):
        return source_settings.get("bucket", source_settings.get("db"))
    return source_settings.get("db")


def _validate_identifier(value, kind):
    """Return ``value`` if it is a safe InfluxDB identifier, else raise.

    Args:
        value (str): candidate identifier
        kind (str): what it is, for the error message (e.g. "field")

    Returns:
        str: the same value, once accepted

    Raises:
        ToolParamError: if the value isn't a safe identifier
    """
    if not isinstance(value, str) or not value or _CONTROL_CHAR_RE.search(value):
        raise ToolParamError(f"invalid {kind} name: {value!r}")
    return value


def _quote_identifier(value):
    """Double-quote an InfluxDB identifier, escaping backslashes and quotes.

    Args:
        value (str): the identifier to quote

    Returns:
        str: the value, double-quoted and escaped for InfluxQL
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote_string_literal(value):
    """Single-quote an InfluxQL string literal (used for tag values).

    Args:
        value (str): the literal to quote

    Returns:
        str: the value, single-quoted and escaped for InfluxQL
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _build_single_point_query(measurement, tag_filters, fields, order, group_by_tag=None):
    """Build an InfluxQL SELECT for one point at either end of a measurement.

    Shared by :func:`build_latest_query` and :func:`build_edge_time_query` - kept as one
    implementation so the measurement/tag validation and quoting below cannot drift between
    the value read and the timestamp-only reads.

    Selects each field explicitly (not ``*``) so tag columns are excluded, and applies the
    source's static tag filters. ``fields=None`` selects ``*`` instead, for a caller that
    reads only the timestamp - excluding tag columns protects a *value* read, and no value
    is read there. Measurement, field and tag keys are charset-validated and
    double-quoted, tag values quoted string literals - the same layered defence as
    build_query. Fields come from key discovery (the live allowlist), never model input.

    Args:
        measurement (str): the InfluxDB measurement name
        tag_filters (dict): static tag key/value filters (may be empty)
        fields (set or None): the field keys to select, or None to select ``*`` for a
            timestamp-only read. Sorted here, so the iteration order of the caller's
            collection does not reach the query.
        order (str): ``"DESC"`` for the newest point, ``"ASC"`` for the oldest
        group_by_tag (str or None): a tag key to return one point per value of, or None for a
            single point across the whole measurement

    Returns:
        str: the InfluxQL query

    Raises:
        ValueError: ``order`` is neither ``"ASC"`` nor ``"DESC"``
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
    if group_by_tag:
        # LIMIT 1 becomes one row *per series* once grouped, which is exactly what a
        # per-producer "latest" or "oldest" needs - verified against a real InfluxDB
        # 1.8, where this returned the newest point for each host in one round trip.
        _validate_identifier(group_by_tag, "tag")
        query += f" GROUP BY {_quote_identifier(group_by_tag)}"
    return query + f" ORDER BY time {order} LIMIT 1"


def build_latest_query(measurement, tag_filters, fields, group_by_tag=None):
    """Build an InfluxQL SELECT for the single most recent point of a measurement.

    The current-state read for a non-live source (see MCP_LIVE_STATE).

    Args:
        measurement (str): the InfluxDB measurement name
        tag_filters (dict): static tag key/value filters (may be empty)
        fields (set): the field keys to select, non-empty
        group_by_tag (str or None): a tag key to return one point per value of, or None for a
            single point across the whole measurement

    Returns:
        str: the InfluxQL query
    """
    return _build_single_point_query(measurement, tag_filters, fields, "DESC", group_by_tag)


def build_edge_time_query(measurement, tag_filters, order, group_by_tag=None):
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

    Args:
        measurement (str): the InfluxDB measurement name
        tag_filters (dict): static tag key/value filters (may be empty)
        order (str): ``"ASC"`` for the oldest point, ``"DESC"`` for the newest
        group_by_tag (str or None): a tag key to return one point per value of, or None for a
            single point across the whole measurement

    Returns:
        str: the InfluxQL query
    """
    return _build_single_point_query(measurement, tag_filters, None, order, group_by_tag)


def _influx_read_request(influx_settings, db, query):
    """Build (url, kwargs) for a GET /query.

    Mirrors _build_write_request's v1/v2 branch: token and org via the v2 /query
    compatibility endpoint (Token header), else v1 /query with HTTP basic auth.
    ``epoch=s`` returns numeric unix timestamps rather than RFC3339 strings.

    Args:
        influx_settings (dict): the ``influx`` settings block
        db (str): the database/bucket name to query
        query (str): the InfluxQL query string

    Returns:
        tuple: ``(url, requests kwargs)``
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
    """Issue a GET and return parsed JSON.

    Maps failures to SourceConnectionError with a message naming what was attempted.

    Args:
        session (requests.Session): the handler's session
        url (str): the read endpoint to call
        kwargs (dict): extra requests kwargs (auth, headers, verify, timeout)
        description (str): what was being read, for the error message

    Returns:
        dict: the parsed JSON response

    Raises:
        SourceConnectionError: the InfluxDB query could not be issued or returned unusable JSON
    """
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
        logging.error("InfluxDB read returned non-JSON (%s): %r", description, exc)
        raise SourceConnectionError(f"InfluxDB read returned an unparseable response ({description})") from exc
    except requests.exceptions.RequestException as exc:
        logging.error("InfluxDB read failed (%s): %r", description, exc)
        raise SourceConnectionError(f"InfluxDB read failed ({description}): {exc!r}") from exc


@dataclass(frozen=True)
class MeasurementKeys:
    """What a measurement currently holds.

    Its field keys with their InfluxDB types, and its tag keys.

    ``field_types`` maps a field key to ``"float"``/``"integer"``/``"string"``/
    ``"boolean"`` as ``SHOW FIELD KEYS`` reports it, **or to None** where the
    response carried no ``fieldType`` column to read: the field is still listed,
    because dropping it would remove it from the query allowlist, but nothing
    honest can be said about its type. Every discovered key is therefore present;
    its value may be None, so treat None as "unknown" and never as a type. The type
    is not a nicety: it is what tells a caller that a text or coded field wants a
    state-timeline rendering rather than a line, and it arrives in the same response
    as the key names, so keeping it costs nothing where discarding it cost a guess.

    ``tag_keys`` is every dimension the measurement can be grouped by. Only the
    one tag a source declares as its instance axis was reachable before; the rest
    (a MyEnergi ``device``, a Nuki lock) existed in the data and nowhere in the
    schema a caller could see.

    Attributes:
        field_types (dict): field key to InfluxDB type, as reported by SHOW FIELD KEYS.
        tag_keys (frozenset): the measurement's tag keys.
    """

    field_types: dict
    tag_keys: frozenset

    @property
    def field_names(self):
        """The field keys as a set - the injection allowlist."""
        return set(self.field_types)


def _statement_results(payload, description):
    """Split a multi-statement InfluxQL response into ``{statement_id: [series]}``.

    A per-result error (wrong db, auth, a rejected statement) arrives inside a 200
    body, so it is raised here rather than left to look like an empty answer - the
    same reasoning as :func:`run_query`, and it matters more with several statements
    in flight: verified against InfluxDB 1.8 and 2.7's v1-compatibility endpoint that
    an unusable database answers with statement 0 carrying ``"error": "not
    executed"`` and *no result at all* for the statements after it, so a caller that
    ignored the error would read the missing statement as "this measurement has no
    tags".

    ``statement_id`` is present on both versions; positional order is the fallback
    so a response without it degrades to the same reading rather than to nothing.

    Args:
        payload (dict): the parsed response body
        description (str): what was being discovered, for the error message

    Returns:
        dict: statement id to its list of series dicts

    Raises:
        SourceConnectionError: if any statement reported an error
    """
    out = {}
    for index, result in enumerate(payload.get("results", [])):
        if result.get("error"):
            raise SourceConnectionError(f"InfluxDB rejected the {description}: {result['error']!r}")
        out[result.get("statement_id", index)] = result.get("series", [])
    return out


def _key_column(all_series, column):  # noqa: DOC403 - a generator, but unannotated
    """Yield each row's value from a named column across a statement's series.

    Skips a series that has no such column, with a warning, rather than falling
    back to a positional guess: a wrong key list would put fields in the tag list
    or invent dimensions that cannot be grouped by, and both read as authoritative.

    Args:
        all_series (list): the series list for one statement
        column (str): the column name to read (e.g. "fieldKey")

    Yields:
        tuple: (series, row, value) triples, the value always a string, so a caller can read a second column of the
            same row
    """
    for series in all_series:
        columns = series.get("columns", [])
        if column not in columns:
            logging.warning("Key discovery returned a series with no %r column (%s); ignoring it", column, columns)
            continue
        index = columns.index(column)
        for row in series.get("values", []):
            if len(row) > index and isinstance(row[index], str):
                yield series, row, row[index]


def discover_measurement_keys(session, influx_settings, db, measurement):
    """Return a measurement's field keys (with their types) and tag keys.

    One request carrying two statements, not two requests: InfluxQL accepts
    semicolon-separated statements and returns a result per statement, so the tag
    keys cost no extra round trip on a call that was already making one. Verified
    against InfluxDB 1.8 and 2.7's v1-compatibility endpoint, whose responses here
    are byte-identical.

    The field set this returns is the live allowlist a queried field is checked
    against. The measurement is charset-validated (it comes from the source class's
    static schema, but validating is cheap) before interpolation.

    Args:
        session (requests.Session): the handler's session
        influx_settings (dict): the shared ``influx`` settings block
        db (str): the database or bucket to query
        measurement (str): the measurement whose keys are wanted

    Returns:
        MeasurementKeys: the field types and tag keys, either half possibly empty

    Raises:
        SourceConnectionError: on a transport/parse failure, or a statement the server rejected
    """
    _validate_identifier(measurement, "measurement")
    quoted = _quote_identifier(measurement)
    query = f"SHOW FIELD KEYS FROM {quoted}; SHOW TAG KEYS FROM {quoted}"
    url, kwargs = _influx_read_request(influx_settings, db, query)
    payload = _get(session, url, kwargs, f"discover keys for {measurement}")
    results = _statement_results(payload, f"key discovery for {measurement}")
    field_types = {}
    for series, row, name in _key_column(results.get(0, []), "fieldKey"):
        columns = series.get("columns", [])
        type_index = columns.index("fieldType") if "fieldType" in columns else None
        # No fieldType column means no honest answer about the type, so the field is
        # still listed and simply carries none.
        field_types[name] = row[type_index] if type_index is not None and len(row) > type_index else None
    tag_keys = {name for _, _, name in _key_column(results.get(1, []), "tagKey")}
    return MeasurementKeys(field_types=field_types, tag_keys=frozenset(tag_keys))


@dataclass(frozen=True)
class QuerySeries:
    """One series from an InfluxQL result: its tag set, columns and rows.

    ``tags`` is empty for an ungrouped query. A ``GROUP BY`` on a tag returns one
    of these per tag value, which is what makes a per-instance answer possible.

    Attributes:
        tags (dict): the tag values identifying this series, empty when not grouped.
        columns (list): the column names, in the order the values use.
        values (list): one list per row.
    """

    tags: dict
    columns: list
    values: list


def discover_tag_values(session, influx_settings, db, measurement, tag):
    """Return the set of values a tag actually holds in a measurement.

    The exact analogue of :func:`discover_measurement_keys`, and it carries the same role: the
    live allowlist an ``instance`` argument is validated against, so a value that was
    never written is refused rather than producing a confidently empty answer. Being
    discovered rather than configured also means a collector host that started
    reporting yesterday is queryable today with no config change.

    Verified against real InfluxDB 1.8 and 2.7 (the latter through its
    v1-compatibility ``/query`` endpoint, whose response is identical): one series
    with ``columns: ["key", "value"]`` and one row per value. Worth having checked
    rather than assumed - that same endpoint reports a bucket's retention as ``0s``,
    so its answers are not interchangeable with v1's by default.

    Args:
        session (requests.Session): the requests session to query through; the caller owns its lifetime.
        influx_settings (dict): the parsed ``influx:`` block, for the URL and credentials.
        db (str): the database or bucket to query.
        measurement (str): the measurement whose tag values to enumerate.
        tag (str): the tag key to enumerate (from the source class, never model input)

    Returns:
        set: the tag-value strings, possibly empty

    Raises:
        SourceConnectionError: on a transport/parse failure
    """
    _validate_identifier(measurement, "measurement")
    _validate_identifier(tag, "tag")
    query = f"SHOW TAG VALUES FROM {_quote_identifier(measurement)} WITH KEY = {_quote_identifier(tag)}"
    url, kwargs = _influx_read_request(influx_settings, db, query)
    payload = _get(session, url, kwargs, f"discover {tag} values for {measurement}")
    values = set()
    for result in payload.get("results", []):
        # Same reasoning as discover_measurement_keys: a per-result error arrives in a 200 body,
        # and swallowing it would make a broken query look like "no instances".
        if result.get("error"):
            raise SourceConnectionError(f"InfluxDB rejected the tag-value discovery: {result['error']!r}")
        for series in result.get("series", []):
            columns = series.get("columns", [])
            if "value" not in columns:
                # A -1 fallback would read each row's *last* cell, which happens to be the
                # right one for today's ["key", "value"] shape and would silently invent
                # tag values if that ever changed. Skipping is the honest answer: a wrong
                # allowlist would refuse real producers and accept ones that do not exist.
                logging.warning(
                    "Tag-value discovery for %s returned a series with no 'value' column (%s); ignoring it",
                    measurement,
                    columns,
                )
                continue
            index = columns.index("value")
            for row in series.get("values", []):
                # Same guard as _key_column: nothing guarantees a row is as long as the
                # column list, and a bare row[index] turns a malformed response into an
                # IndexError escaping a read the callers expect to raise SourceConnectionError.
                if len(row) > index and isinstance(row[index], str):
                    values.add(row[index])
    return values


def run_query(session, influx_settings, db, query):
    """Execute an InfluxQL query and return **every** series it produced.

    A ``GROUP BY`` on a tag yields one series per tag value, each carrying its own
    ``tags`` map (verified against InfluxDB 1.8 and 2.7's v1-compatibility
    endpoint, whose responses are identical here). An earlier version returned only
    the first series, which silently discarded every producer but one - invisible
    while every query happened to be ungrouped, and wrong the moment one is not.
    Callers that genuinely cannot produce more than one series use
    :func:`single_series` to say so explicitly.

    Args:
        session (requests.Session): the handler's session
        influx_settings (dict): the shared ``influx`` settings block
        db (str): the database or bucket to query
        query (str): the InfluxQL to run

    Returns:
        list: QuerySeries, empty when the query matched nothing

    Raises:
        SourceConnectionError: on a transport/parse failure
    """
    url, kwargs = _influx_read_request(influx_settings, db, query)
    payload = _get(session, url, kwargs, "query")
    found = []
    for result in payload.get("results", []):
        if result.get("error"):
            raise SourceConnectionError(f"InfluxDB rejected the query: {result['error']!r}")
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

    Args:
        series (list): list of QuerySeries from run_query

    Returns:
        tuple: ``(columns, values)``, or ``([], [])`` when there is no series

    Raises:
        ValueError: if given more than one series
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
