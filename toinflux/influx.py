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

    :cvar bufferable: whether this specific failure is worth buffering/retrying. A
        transient problem (network error, 5xx, no response at all) plausibly succeeds
        later, so it's bufferable (the default). A 400 means InfluxDB rejected this
        exact point as malformed - retrying the identical payload would fail
        identically forever, so it's not: buffering it would permanently block
        flushing every point behind it. Set as an instance attribute after
        construction (see _post_line) rather than via a custom __init__, so the
        exception's args/str() stay a plain single message.
    :vartype bufferable: bool
    """

    bufferable = True


# Bound on how many failed points each source buffers in memory before the oldest is
# dropped to make room for new ones - see DataHandler._write_buffers.
MAX_BUFFERED_POINTS = 500


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

    # Bounded per-source buffer of line-protocol points that failed to write, flushed on
    # the next successful send. Class-level (shared across instances/subclasses, keyed by
    # source name) rather than an instance attribute: the worker loop in sendtoinflux.py
    # discards and reconstructs the DataHandler instance after every failure, so only a
    # buffer that outlives the instance survives to be flushed later. deque(maxlen=...)
    # evicts the oldest buffered point once a source's buffer is full, so a very long
    # outage degrades gracefully instead of growing memory without bound.
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

    def send_data(self, data=None, timestamp=None):
        """
        Sends data to influxDB.

        Before sending the new point, first tries to flush any points buffered from
        earlier failed writes to this source (oldest first) - see ``_write_buffers``.
        If the new point (or a buffered one) fails to send, it's appended to the buffer
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
        :return: None
        :raises InfluxWriteError: if the write to InfluxDB fails
        """
        # if the data is not provided, use the data from the class
        if data is None:
            data = self.data

        if not data or not isinstance(data, dict):
            logging.warning("No data to send to InfluxDB")
            return

        if timestamp is None:
            timestamp = self.timestamp if self.timestamp is not None else int(time.time())

        # format the data to send
        data_to_send = (
            self.influx_header
            + ",".join(f"{_escape_key_or_tag_value(key)}={_format_field_value(value)}" for key, value in data.items())
            + f" {timestamp}"
        )

        influx_settings = self.settings["influx"]
        url, post_kwargs, insecure = self._build_write_request(influx_settings)
        buffer = self._write_buffers.setdefault(self.source, deque(maxlen=MAX_BUFFERED_POINTS))

        try:
            self._flush_buffer(buffer, url, post_kwargs, insecure)
        except InfluxWriteError:
            self._buffer_point(buffer, data_to_send)
            raise

        try:
            self._post_line(data_to_send, url, post_kwargs, insecure)
        except InfluxWriteError as exc:
            if exc.bufferable:
                self._buffer_point(buffer, data_to_send)
            else:
                logging.warning(
                    "Not buffering point for source '%s' - InfluxDB rejected it and it "
                    "would never succeed on retry: %s",
                    self.source,
                    exc,
                )
            raise

    def _flush_buffer(self, buffer, url, kwargs, insecure):
        """
        Flush a source's buffered points, oldest first.

        A bufferable (transient) failure stops the flush and re-raises, leaving the
        point that failed - and everything behind it - in the buffer to retry later.
        A non-bufferable (permanent) failure drops just that one point and continues
        flushing the rest, since the failure says nothing about whether InfluxDB
        itself is reachable - only that this particular point is bad.

        :param buffer: the source's buffer (from ``_write_buffers``)
        :type buffer: collections.deque
        :param url: destination InfluxDB write URL
        :type url: str
        :param kwargs: extra requests.Session.post() kwargs (auth/headers/verify/timeout)
        :type kwargs: dict
        :param insecure: whether to suppress the urllib3 InsecureRequestWarning for this call
        :type insecure: bool
        :return: None
        :raises InfluxWriteError: on the first bufferable (transient) failure
        """
        while buffer:
            try:
                self._post_line(buffer[0], url, kwargs, insecure)
            except InfluxWriteError as exc:
                if exc.bufferable:
                    raise
                logging.warning(
                    "Dropping buffered point for source '%s' - InfluxDB rejected it and it "
                    "would never succeed on retry: %s",
                    self.source,
                    exc,
                )
                buffer.popleft()
                continue
            buffer.popleft()

    def _build_write_request(self, influx_settings):
        """
        Build the URL/kwargs used to POST a line-protocol body to this source's InfluxDB
        target. Independent of any one point's content, so it's computed once per
        ``send_data()`` call and reused for every line posted during that call (any
        flushed backlog plus the new point).

        :param influx_settings: the ``influx`` settings block
        :type influx_settings: dict
        :return: (url, kwargs for requests.Session.post(), whether TLS verification is disabled)
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

        insecure = influx_settings.get("insecure", False)
        kwargs["verify"] = not insecure
        kwargs["timeout"] = timeout
        return url, kwargs, insecure

    def _post_line(self, line, url, kwargs, insecure):
        """
        POST a single line-protocol line to InfluxDB.

        :param line: line-protocol point to send
        :type line: str
        :param url: destination InfluxDB write URL
        :type url: str
        :param kwargs: extra requests.Session.post() kwargs (auth/headers/verify/timeout)
        :type kwargs: dict
        :param insecure: whether to suppress the urllib3 InsecureRequestWarning for this call
        :type insecure: bool
        :return: None
        :raises InfluxWriteError: if the write to InfluxDB fails
        """
        try:
            with warnings.catch_warnings():
                if insecure:
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.post(url, data=line, **kwargs)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logging.error("Error sending data to InfluxDB - %s", e)
            status_code = getattr(e.response, "status_code", None)
            exc = InfluxWriteError(str(e))
            exc.bufferable = status_code != 400
            raise exc from e

    def _buffer_point(self, buffer, line):
        """
        Append a failed point to a source's write buffer, warning if this evicts the
        oldest buffered point because the buffer was already full.

        :param buffer: the source's buffer (from ``_write_buffers``)
        :type buffer: collections.deque
        :param line: line-protocol point that failed to send
        :type line: str
        :return: None
        """
        if len(buffer) >= buffer.maxlen:
            logging.warning(
                "InfluxDB write buffer for source '%s' is full (%d points); dropping the oldest buffered point",
                self.source,
                buffer.maxlen,
            )
        buffer.append(line)
