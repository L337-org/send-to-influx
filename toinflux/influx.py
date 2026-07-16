"""Parent class for data handlers to send data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import time
import logging
import warnings
from collections import deque
import urllib3
import requests
from toinflux.general import load_settings
from toinflux.exceptions import ConfigError


class InfluxWriteError(Exception):
    """
    Raised when a write to InfluxDB fails.

    :cvar status_code: the HTTP status code of the failed write, or None when no
        response was received at all (connection error/timeout). Set as an instance
        attribute after construction (see _post_line) rather than via a custom
        __init__, so the exception's args/str() stay a plain single message.
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


def _escape_key_or_tag_value(value):
    """
    Escape a value for use as an InfluxDB line protocol key or tag value.

    Per the line protocol spec, commas, equals signs and spaces must be
    backslash-escaped in measurement/tag/field keys and tag values (field
    *values* follow different quoting rules, handled by _format_field_value).

    :param value: key or tag value to escape
    :return: escaped line protocol representation
    :rtype: str
    """
    value = str(value)
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


class DataHandler:
    """Class to send data to InfluxDB"""

    # Bounded per-source buffer of points that failed to write, flushed on the next
    # successful send. Each entry is a mutable [line, rejection_count] pair - the count
    # tracks how many times the server has rejected (4xx) that specific point, so
    # _flush_buffer can give up on it after MAX_POINT_REJECTIONS. Class-level (shared
    # across instances/subclasses, keyed by source name) rather than an instance
    # attribute: the worker loop in sendtoinflux.py discards and reconstructs the
    # DataHandler instance after every failure, so only a buffer that outlives the
    # instance survives to be flushed later. deque(maxlen=...) evicts the oldest
    # buffered point once a source's buffer is full, so a very long outage degrades
    # gracefully instead of growing memory without bound. Buffered lines are flushed to
    # whatever destination the *current* settings resolve to - an accepted limitation:
    # editing influx.url/bucket/db while a backlog exists re-routes that backlog to the
    # new destination.
    _write_buffers: dict = {}

    def __init__(self, source=None, settings_file=None):
        self.settings = load_settings(settings_file)
        self.source = source
        self.influx_header = None
        self.data = None
        self.timestamp = None
        self.session = requests.Session()

        if self.source and self.source in self.settings:
            self.source_settings = self.settings[self.source]
        else:
            raise ConfigError(f"Source {self.source} not found in settings")

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
            logging.warning("No data to send to InfluxDB")
            data_to_send = None
            if not (use_buffer and self._write_buffers.get(self.source)):
                return
        else:
            if timestamp is None:
                timestamp = self.timestamp if self.timestamp is not None else int(time.time())
            data_to_send = (
                self.influx_header
                + ",".join(
                    f"{_escape_key_or_tag_value(key)}={_format_field_value(value)}" for key, value in data.items()
                )
                + f" {timestamp}"
            )

        url, post_kwargs = self._build_write_request(self.settings["influx"])

        if not use_buffer:
            self._post_line(data_to_send, url, post_kwargs)
            return

        buffer = self._write_buffers.setdefault(self.source, deque(maxlen=MAX_BUFFERED_POINTS))
        try:
            self._flush_buffer(buffer, url, post_kwargs)
            if data_to_send is not None:
                self._post_line(data_to_send, url, post_kwargs)
        except InfluxWriteError:
            if data_to_send is not None:
                self._buffer_point(buffer, data_to_send)
            raise

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
            chunk = [buffer[i] for i in range(min(len(buffer), FLUSH_CHUNK_SIZE))]
            if len(chunk) == 1:
                self._flush_head(buffer, url, kwargs)
                continue
            try:
                self._post_line("\n".join(entry[0] for entry in chunk), url, kwargs)
            except InfluxWriteError as exc:
                if not _is_point_rejection(exc.status_code):
                    logging.warning(
                        "Flushing %d buffered point(s) for source '%s' failed; will retry next cycle",
                        len(buffer),
                        self.source,
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
                        "Dropping buffered point for source '%s' after %d server rejections: %s",
                        self.source,
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
            logging.debug("Point already buffered for source '%s'; not buffering a duplicate copy", self.source)
            return
        if len(buffer) >= buffer.maxlen:
            logging.warning(
                "InfluxDB write buffer for source '%s' is full (%d points); dropping the oldest buffered point",
                self.source,
                buffer.maxlen,
            )
        buffer.append([line, 0])
