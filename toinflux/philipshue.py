"""Functions to get data from a Hue Bridge and format ready for InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

import ipaddress
import logging
import os
import re
import warnings
from collections import namedtuple
import urllib3
import requests
from toinflux.credentials import CANONICAL_SLOT_SUFFIX_RE, PLACEHOLDER_VALUES, SENTINEL_PREFIX
from toinflux.influx import DataHandler, escape_key_or_tag_value
from toinflux.exceptions import ConfigError, SourceConnectionError, ToolParamError

# One configured bridge: its slot number, and the host/token as written in settings.
# ``slot`` is only ever used to name the fields it came from, in messages and in
# bridge_field_names() - it carries no ordering, and slots need not be contiguous.
Bridge = namedtuple("Bridge", "slot host user")

# Slot 1 is the unnumbered ``host``/``user`` pair that every install has always had;
# further bridges are ``host2``/``user2`` ... ``hostN``/``userN``. Deliberately no cap.
# The suffix must be canonical: ``host1`` is rejected rather than silently accepted as
# a synonym for ``host`` (it would be a second way to spell slot 1), and a leading zero
# is rejected rather than folded onto its unpadded twin (``host02`` vs ``host2``).
# Both halves of a slot are matched, not just the host: otherwise a mistyped ``user02``
# would be silently ignored (the slot would report its token as unset while the token sat
# in a key nothing reads), and a token left behind in a slot whose host key was deleted
# outright would never be noticed at all.
#
# The canonical-suffix rule itself lives in toinflux.credentials, because the credential
# side must agree with it about which slots exist (hue-user2 is a credential iff hue.user2
# is a slot field) and a second copy would drift.
_SLOT_FIELD_RE = re.compile(r"^(?:host|user)(?P<suffix>\d*)$")


def bridge_field_names(slot):
    """
    Return the ``(host_field, user_field)`` settings keys for a slot number.

    Slot 1 is the unnumbered pair, so this is the single place that knows the
    numbering convention - callers never build ``f"host{n}"`` themselves.

    :param slot: slot number (1 for the original unnumbered pair)
    :type slot: int
    :return: (host field name, user field name)
    :rtype: tuple
    """
    suffix = "" if slot == 1 else str(slot)
    return (f"host{suffix}", f"user{suffix}")


def _comparable_host(host):
    """
    Normalise a host for *comparison only*, so two spellings of one address are
    recognised as the same bridge.

    ``2001:db8::1`` and ``2001:0db8:0000:0000:0000:0000:0000:0001`` are the same
    address written differently, and ``HUE1.local``/``hue1.local`` differ only in case -
    a raw string comparison catches neither. IP literals are compared as parsed
    addresses (brackets stripped first), hostnames case-folded.

    Never used for the request URL or the InfluxDB tag: those keep the configured
    value verbatim (see ``_url_host`` and ``get_data``).

    :param host: host as configured
    :type host: str
    :return: a value equal for any two spellings of the same host
    :rtype: str
    """
    text = str(host).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        # A hostname (or something unparseable) - case is the only equivalence we can
        # claim without resolving it. A hostname and its own IP are NOT recognised as
        # the same bridge; that's an accepted limitation, documented in README.
        return text.casefold()


def _usable_token(value):
    """
    Whether a configured Hue token is real, as opposed to absent or a stand-in.

    Unusable means: not a string, blank, still the example placeholder, or a
    systemd-creds sentinel that was never substituted (settings.yaml says the value
    lives in the credstore but no credential was found). Each of those would otherwise
    reach the bridge as a doomed authentication attempt, retried with backoff forever,
    instead of failing fast as the configuration error it is.

    :param value: the configured ``user``/``userN`` value
    :return: True if it looks like a real token
    :rtype: bool
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or text.startswith(SENTINEL_PREFIX):
        return False
    return text != PLACEHOLDER_VALUES.get("hue-user")


def _parse_slot_field(field):
    """
    Interpret a settings field name as a bridge slot.

    :param field: a key from the ``hue`` block
    :return: ``(slot, error)`` - ``(None, None)`` when the field doesn't name a slot at
        all, ``(None, message)`` when it looks like one but isn't canonical
    :rtype: tuple
    """
    match = _SLOT_FIELD_RE.match(str(field))
    if not match:
        return (None, None)
    suffix = match.group("suffix")
    if suffix and not CANONICAL_SLOT_SUFFIX_RE.match(suffix):
        return (
            None,
            f"hue.{field} is not a valid bridge slot - the first bridge is hue.host/hue.user, and "
            f"further bridges are numbered from 2 with no leading zeros (hue.host2/hue.user2, "
            f"hue.host3/hue.user3, ...)",
        )
    return (int(suffix) if suffix else 1, None)


def _bridge_for_slot(hue_settings, slot):
    """
    Resolve one slot into a bridge, an error, or a warning.

    At most one of ``bridge``/``error``/``warning`` is ever set, and all three are
    ``None`` for a vacant slot, which is a legitimate resting state rather than a fault.

    ``host`` is returned separately and independently of the other three: it is the
    configured host for any slot that has one, *including* a slot whose token is unusable.
    Duplicate-host detection needs it in that case too - two slots naming one bridge is a
    mistake whether or not both have tokens yet (see ``_duplicate_host_errors``).

    :param hue_settings: the ``hue`` settings block
    :type hue_settings: dict
    :param slot: slot number to resolve
    :type slot: int
    :return: ``(bridge, error, warning, host)`` - ``host`` is None only when the slot has
        no usable host at all
    :rtype: tuple
    """
    host_field, user_field = bridge_field_names(slot)
    host = hue_settings.get(host_field)
    user = hue_settings.get(user_field)

    if host is None or (isinstance(host, str) and not host.strip()):
        # Vacant. A token left behind in it is cosmetic - removing a bridge blanks the
        # token first and clears the host as a separate step, so this is a legitimate
        # intermediate state, not a fault.
        if _usable_token(user):
            logging.debug(
                "hue.%s is set but hue.%s is empty - that bridge is not collected; "
                "clear hue.%s too, or set the host to use the slot again",
                user_field,
                host_field,
                user_field,
            )
        return (None, None, None, None)
    if not isinstance(host, str):
        # YAML coerces more than you'd expect: `host: 10.0` is a float, `host: yes` is a
        # bool. Neither can address a bridge, and both would otherwise be str()-ed into a
        # doomed URL.
        return (None, f"hue.{host_field} must be a string (got {host!r})", None, None)
    if any(char in host for char in (chr(10), chr(13))):
        # The host is written verbatim as the `host` tag, and the line protocol has no
        # escape for a newline - it is what separates points, so one here would end the
        # point early and turn the rest into a second point nobody configured.
        # escape_key_or_tag_value() refuses it too, but failing at write time names no
        # settings key; caught here it does. Found by sweeping for the pattern after review
        # raised the same defect against MyEnergi labels, rather than waiting to be told.
        return (
            None,
            f"hue.{host_field} must not contain a newline - it is written as the InfluxDB "
            f"host tag, and a newline there would split the point in two",
            None,
            None,
        )
    if not _usable_token(user):
        return (
            None,
            None,
            f"hue.{user_field} is not set for the bridge at hue.{host_field} ({host.strip()}) - "
            f"that bridge will not be collected. Set it to that bridge's whitelist token, or "
            f"clear hue.{host_field} if that bridge is no longer in use" + _credstore_caveat(),
            host.strip(),
        )
    return (Bridge(slot=slot, host=host.strip(), user=user.strip()), None, None, host.strip())


def _credstore_caveat():
    """
    Return the systemd-creds caveat to append to an unset-token warning, or ``""``.

    A credential migrated with ``send-to-influx-set-credential`` is mounted by systemd for
    the service alone, so from any other context it reads as unset and this warning is a
    false alarm. Before multi-bridge support nothing validated the ``hue`` block, so the
    confusion is new and worth pre-empting.

    Attached to *this* warning rather than reported once per run, so it can only accompany
    the finding it explains: said alongside "no Hue bridge is configured", where the problem
    is an absent host and no credential is involved, it would send the reader to the
    credential store to look for something that was never missing.

    Empty when ``CREDENTIALS_DIRECTORY`` is set, because that means we *are* the service:
    the value really was substituted, so an unset token is genuinely unset and the caveat
    would be misdirection just the same. Being unset does not prove the credential store is
    in use - a source checkout never migrated anything - so the wording stays conditional
    rather than asserting that the value is stored there.

    :return: the caveat, prefixed with a separator, or an empty string
    :rtype: str
    """
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        return ""
    return (
        ". If it is stored with send-to-influx-set-credential, it is not visible outside the "
        "service and reads as unset here - run 'systemctl status send-to-influx' to see what "
        "the service itself reports"
    )


def _duplicate_host_errors(configured):
    """
    Return an error for every slot addressing a bridge an earlier slot already addresses.

    Compared through ``_comparable_host``, so two spellings of one address are caught.

    Deliberately checked over every slot that *has a host*, not only those with a usable
    token. A slot whose token is missing spawns no worker, so nothing is double-collected
    today - but the duplicate is still a mistake, and reporting it only once the missing
    token is filled in would surface it at the least expected moment, long after the
    copy-paste that caused it. Two slots naming one bridge cannot be intended either way.

    :param configured: ``(slot, host)`` for every slot with a non-blank string host
    :type configured: list
    :return: error strings, empty when every host is distinct
    :rtype: list
    """
    errors, seen = [], {}
    for slot, host in configured:
        first_slot, first_host = seen.setdefault(_comparable_host(host), (slot, host))
        if first_slot == slot:
            continue
        first_host_field, _ = bridge_field_names(first_slot)
        host_field, _ = bridge_field_names(slot)
        errors.append(
            f"hue.{host_field} ({host}) is the same bridge as hue.{first_host_field} "
            f"({first_host}) - each slot must address a different bridge. Once both have tokens the "
            f"same devices would be polled twice: into one series if the two values are spelled "
            f"identically (two workers overwriting each other's points), or into two series for one "
            f"physical bridge if they differ, double-counting it in any query"
        )
    return errors


def _slot_numbers(hue_settings):
    """
    Return ``(slots, errors)`` for the slot-shaped fields in a ``hue`` block.

    Slots come back in **numeric** order. The keys are scanned with ``sorted()``, which is
    lexicographic and so puts ``host10`` before ``host2``; slots are numeric identifiers,
    and processing them out of numeric order would make the bridge list, the startup log
    and - most confusingly - the "same bridge as hue.hostN" message name an arbitrary slot
    as the earlier one. Deterministic either way, but only one of the two reads correctly.

    :param hue_settings: the ``hue`` settings block
    :type hue_settings: dict
    :return: (slot numbers in numeric order, errors for malformed slot fields)
    :rtype: tuple
    """
    slots, errors = set(), []
    # key=str because YAML permits non-string mapping keys (`1: x`, `true: y`), and a
    # mixed-type key set makes a bare sorted() raise TypeError - crashing out of
    # validation instead of reporting a clean ConfigError. Same reasoning as
    # validate_settings()'s guard on non-string `sources:` entries.
    for field in sorted(hue_settings, key=str):
        slot, slot_error = _parse_slot_field(field)
        if slot_error:
            errors.append(slot_error)
        if slot is not None:
            # A set: host and user both name the same slot, so each is seen twice.
            slots.add(slot)
    return (sorted(slots), errors)


def enumerate_bridges(hue_settings):
    """
    Enumerate the bridges configured in a ``hue`` settings block.

    The single source of truth for "which bridges are configured", shared by
    ``validate_settings()``, the worker spawner and the CLI modes - if validation and
    the runtime enumerated separately they would eventually disagree about what is
    actually configured.

    Slot 1 is the unnumbered ``host``/``user`` pair; further bridges are ``hostN``/
    ``userN``. A slot counts as configured when its host is a non-blank string. Slot
    numbers carry no ordering and need not be contiguous, and nothing ever renumbers -
    the slot number is the binding between a host and its token, so a vacated slot stays
    vacant rather than shifting the ones above it down onto the wrong credentials.

    Severity is split deliberately, and the line matters:

    - **Errors** are self-contradictory configuration: a field that isn't a valid slot,
      a non-string host, two slots addressing the same bridge. None of these can be a
      not-yet-configured state, so failing fast is safe.
    - **Warnings** are "this bridge isn't usable yet": no host at all, or a host with a
      placeholder/blank/unsubstituted token. These *must not* be fatal, because
      ``example_settings.yaml``'s ``hue:`` block still ships the placeholder host/token
      pair - enabling ``hue`` in ``sources:`` without also filling those in is exactly
      this state, and is also how the packaging suite seeds Hue (a real host that just
      happens to be unreachable, not a placeholder, but validated the same way). Raising
      here would stop **every** collector, taking working sources down with the
      unconfigured one.

    A leftover ``userN`` with no usable ``hostN`` is neither: a token whose host has been
    cleared is the resting state part-way through removing a bridge (the token is blanked
    first, clearing the host is a separate step), so treating it as a fault would report
    the removal procedure as an error mid-way through. Reported at DEBUG only.

    :param hue_settings: the ``hue`` settings block
    :type hue_settings: dict
    :return: (bridges found, error strings, warning strings) - bridges are returned even
        when there are errors, so a caller can report every problem at once rather than
        one per run
    :rtype: tuple
    """
    if not isinstance(hue_settings, dict):
        return ([], [f"hue must be a mapping of settings (got {type(hue_settings).__name__})"], [])

    bridges, warnings_out, configured = [], [], []
    slots, errors = _slot_numbers(hue_settings)
    for slot in slots:
        bridge, error, warning, host = _bridge_for_slot(hue_settings, slot)
        if bridge is not None:
            bridges.append(bridge)
        if error:
            errors.append(error)
        if warning:
            warnings_out.append(warning)
        if host is not None:
            configured.append((slot, host))

    if not bridges and not errors and not warnings_out:
        warnings_out.append("no Hue bridge is configured (hue.host is empty or absent) - hue collects nothing")
    errors.extend(_duplicate_host_errors(configured))
    return (bridges, errors, warnings_out)


def _url_host(host):
    """
    Format a configured host for use in the authority part of a URL.

    A bare IPv6 literal has to be bracketed - ``https://2001:db8::1/...`` is
    ambiguous, because everything from the first colon onward parses as a port,
    so an unbracketed address fails every request. Hostnames and IPv4 addresses
    are returned unchanged, and a value the user already bracketed is left
    alone, which makes this safe to apply unconditionally and idempotent.

    This is a URL-construction concern only. The value written as the InfluxDB
    ``host`` tag is deliberately left exactly as configured (see ``get_data``) -
    normalising it there would change the tag value for existing installs.

    :param host: host as configured in settings.yaml - a hostname, an IPv4
        literal, or an IPv6 literal with or without brackets
    :type host: str
    :return: the host, bracketed if it is a bare IPv6 literal
    :rtype: str
    """
    text = str(host).strip()
    if text.startswith("[") and text.endswith("]"):
        return text
    try:
        # A zone id (fe80::1%eth0) parses here too, and is bracketed like any
        # other IPv6 literal rather than percent-encoded per RFC 6874 - requests
        # does not accept that form anyway, and a link-local bridge address is
        # not a case this has to serve.
        address = ipaddress.ip_address(text)
    except ValueError:
        # A hostname, or anything else not to be second-guessed (a hand-written
        # host:port, say) - passed through untouched.
        return text
    return f"[{text}]" if address.version == 6 else text


class Hue(DataHandler):
    """Child class of DataHandler to get data from a Hue Bridge"""

    MCP_DESCRIPTION = "Philips Hue: lights and smart plugs (on/off, brightness) and motion/temperature/light sensors."

    # Hue is the one v1 source with a documented, buildable device-write path
    # (PUT /api/{user}/lights/{id}/state on the same session/auth the collector
    # already uses). The MCP write tool is still only registered when the
    # operator sets hue.mcp_read_write: true - see DataHandler.mcp_write_enabled.
    MCP_WRITABLE = True
    # Every point carries host=<the bridge it came from>, which with more than one bridge
    # is what separates them: field names are unprefixed, so two bridges with a light of
    # the same name write the same field key under different host tags. Naming the axis
    # here replaces the bespoke `bridge` parameter with the shared instance mechanism, so
    # there is one scoping path rather than two implementations of the same idea.
    MCP_INSTANCE_TAG = "host"

    # Hue brightness ("bri") is 1-254; the MCP tool speaks 0-100 % and maps here.
    HUE_BRI_MIN = 1
    HUE_BRI_MAX = 254

    # Hue colour temperature ("ct") is in mireds/mirek (= 1e6 / kelvin). The
    # standard range is 153 mirek (6535 K, coolest) to 500 mirek (2000 K,
    # warmest); a light reporting its own capabilities.control.ct range overrides
    # this. The MCP tool speaks kelvin and converts here.
    HUE_CT_MIN = 153
    HUE_CT_MAX = 500

    # Friendly colour names the write tool accepts alongside an "#rrggbb" hex.
    _HUE_COLOR_NAMES = {
        "red": "ff0000",
        "orange": "ff8800",
        "yellow": "ffff00",
        "green": "00ff00",
        "cyan": "00ffff",
        "blue": "0000ff",
        "purple": "8000ff",
        "magenta": "ff00ff",
        "pink": "ff69b4",
        "white": "ffffff",
        "warm white": "ffd6aa",
        "cool white": "f0f8ff",
    }

    def bridge(self):
        """
        Return the bridge this handler collects from.

        ``self.instance`` names the bridge by host. ``None`` means "the first configured
        bridge", which is what keeps a single-bridge install - and every caller that
        constructs a handler without an instance, such as the MCP tools - behaving exactly
        as it did before slots existed.

        Resolved on each use rather than cached: ``self.settings`` is read once at
        construction, so the answer cannot change during the handler's life, and
        enumeration is pure dictionary work with no I/O.

        :return: the resolved bridge
        :rtype: Bridge
        :raises ConfigError: the block is malformed, configures no usable bridge, or does
            not contain the bridge this handler was created for (a worker outliving a
            configuration change). Not retryable - the worker should stop rather than loop
            forever against a bridge that is not there.
        """
        bridges, errors, unusable = enumerate_bridges(self.settings.get("hue"))
        if errors:
            raise ConfigError("; ".join(errors))
        if not bridges:
            # Report what enumeration actually found, rather than a generic "no bridge
            # configured": the same no-usable-bridges state covers both nothing being
            # configured at all *and* a host that is configured but whose token is
            # missing or still a placeholder. Claiming the host is absent when it is
            # sitting right there sends the reader looking in the wrong place, and the
            # warnings already name the slot, the host and what to set.
            raise ConfigError("; ".join(unusable) or "no Hue bridge is configured")
        if self.instance is None:
            return bridges[0]
        for bridge in bridges:
            if bridge.host == self.instance:
                return bridge
        configured = ", ".join(bridge.host for bridge in bridges)
        raise ConfigError(f"no Hue bridge configured at '{self.instance}' (configured: {configured})")

    def get_data(self):
        """
        Get the data from the Hue Bridge

        :return: data
        :rtype: dict
        """
        # The host is escaped here but NOT normalised: the tag keeps whatever was
        # configured, because rewriting it would change the series identity of an existing
        # install. Escaping is separate - send_data() escapes field keys but takes the
        # header verbatim, so a host containing a comma, equals sign or space would
        # otherwise end the tag set early and silently produce a corrupt point.
        self.influx_header = f"hue,host={escape_key_or_tag_value(self.bridge().host)} "
        self.data = self.parse_hue_data()
        return self.data

    def _api_base(self):
        """
        Return the bridge's authenticated API base URL, ``https://<host>/api/<user>``.

        Shared by the read path (``get_data_from_hue_bridge``) and the MCP write
        path (``_put_light_state``) so that both bracket an IPv6 host
        identically - see ``_url_host``. A second copy of this construction is how
        one of the two paths would silently keep the bug.

        :return: API base URL for this bridge
        :rtype: str
        """
        bridge = self.bridge()
        return f"https://{_url_host(bridge.host)}/api/{bridge.user}"

    def mcp_tag_filters(self):
        """Scope reads to this handler's own bridge when it serves one.

        Hue tags every point with the bridge's host, so adding that tag is what turns a
        read of "the hue measurement" into a read of *one bridge*. Without it a query spans
        every bridge, which is right when no bridge was asked for and wrong when one was.

        ``instance`` is None for a handler that was not created for a particular bridge, and
        then the filters stay as the class's - deliberately unscoped, so an unqualified
        query still returns the whole estate.

        The value is the configured host, resolved through ``bridge()`` rather than taken
        from a caller: an unknown bridge raises before it can reach a query.

        :return: tag filters, including this bridge's host when there is one
        :rtype: dict
        """
        filters = dict(self.MCP_TAG_FILTERS)
        if self.instance is not None:
            filters["host"] = self.bridge().host
        return filters

    def _redact(self, message):
        """
        Replace the bridge token with a marker before a message is logged or raised.

        The CLIP v1 API carries the whitelist token in the URL path, and requests
        puts the request URL into its exception messages - both for a connection
        failure ("Max retries exceeded with url: /api/<token>") and for an error
        status ("503 Server Error ... for url: https://host/api/<token>/..."),
        confirmed by reproduction. Without this, one unreachable bridge writes the
        token to the journal and to /var/log/send-to-influx.log, again via the
        worker loop's own "Source '%s' failed" line, and - worst of the three -
        hands it to any connected MCP client, since a SourceConnectionError from a
        read or write tool is returned to the caller as the tool's error.

        Everything but the token is preserved verbatim, so a failure stays
        diagnosable from the log alone: host, status and underlying cause survive.

        Note the wrapped cause (``raise ... from e``) still holds the unredacted
        message. That is deliberate - the cause chain has to be preserved - and is
        only exposed by printing a traceback, which no code path does for these
        errors.

        :param message: message that may contain the token
        :type message: str
        :return: the message with any occurrence of the token replaced
        :rtype: str
        """
        # Every configured bridge's token, not just this worker's: enumeration cannot
        # raise, so this stays safe to call from an exception handler, and redacting the
        # whole set means a message can never carry a token merely because it came from a
        # different slot than expected. Longest first, so a token that is a prefix of
        # another cannot leave a fragment behind. An absent/blank token has nothing to
        # hide, and "".replace() would splice the marker between every character.
        bridges, _, _ = enumerate_bridges(self.settings.get("hue"))
        for token in sorted(
            {bridge.user for bridge in bridges if isinstance(bridge.user, str) and bridge.user},
            key=len,
            reverse=True,
        ):
            message = message.replace(token, "<redacted>")
        return message

    def get_data_from_hue_bridge(self):
        """
        Connect to the Hue bridge and get the sensor data

        :return: hue_data
        :rtype: dict
        """
        # Hue bridges are commonly reached over a self-signed local cert, so verification is
        # skipped by default; set hue.insecure: false in settings.yaml if yours has a valid cert.
        insecure = self.settings["hue"].get("insecure", True)
        try:
            with warnings.catch_warnings():
                if insecure:
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.get(
                    self._api_base(),
                    timeout=self.settings["hue"].get("timeout", 5),
                    verify=not insecure,
                )
            hue_data = response.json()
        except ValueError as e:
            # response.json() raises on a non-JSON body (e.g. an HTML error page).
            # requests' own JSONDecodeError is BOTH a ValueError and a
            # RequestException, so this must be caught before the RequestException
            # handler below - otherwise a parse failure would be misreported as a
            # transport "connection" error. (Guards both the collector read path
            # and the MCP write tools' device discovery, which share this method.)
            logging.error("Hue Bridge returned an unparseable response - %s", self._redact(str(e)))
            raise SourceConnectionError(self._redact(f"Hue Bridge returned an unparseable response: {e}")) from e
        except requests.exceptions.RequestException as e:
            logging.error("Error connecting to Hue Bridge - %s", self._redact(str(e)))
            raise SourceConnectionError(self._redact(str(e))) from e
        # A successful GET returns a dict (sensors/lights); a list only ever comes
        # back on error. Guard the indexing: an empty list, or a list whose first
        # item isn't the documented {"error": {...}} shape, is unexpected and must
        # fail cleanly rather than raise IndexError/KeyError unhandled.
        if isinstance(hue_data, list):
            first = hue_data[0] if hue_data else None
            error = first.get("error") if isinstance(first, dict) else None
            # The "error" value should itself be a dict with a "description"; guard
            # it too, so a malformed error shape still fails cleanly rather than
            # raising AttributeError from .get() on a non-dict.
            if isinstance(error, dict):
                description = error.get("description", str(error))
            else:
                description = f"unexpected list response: {hue_data!r:.200}"
            logging.error("Error connecting to Hue Bridge - %s", description)
            raise SourceConnectionError(description)
        # A successful GET is a dict (sensors/lights). A non-dict, non-list body - a
        # JSON scalar/null, e.g. from a misconfigured proxy - is unexpected; fail
        # cleanly here rather than returning it for a caller (parse_hue_data /
        # _fetch_lights) to crash on with a TypeError/AttributeError.
        if not isinstance(hue_data, dict):
            logging.error("Hue Bridge returned an unexpected response type - %.200r", hue_data)
            raise SourceConnectionError(f"Hue Bridge returned an unexpected response type: {hue_data!r:.200}")
        return hue_data

    def hue_device_name_to_name(self, device_name):
        """
        Converts the device name into a name to be used in InfluxDB

        If no name mapping exists in the settings file, the name in the Hue settings is used.
        Any spaces will be replaced with underscores.

        :param device_name: name of the device in the hue settings
        :type device_name: str
        :return: name
        :rtype: str
        """
        if "sensors" in self.settings["hue"]:
            name = self.settings["hue"]["sensors"].get(device_name, device_name)
        else:
            name = device_name
        return name.replace(" ", "_")

    def parse_hue_data(self):
        """
        Parse the data from the bridge to get the values we want

        :return: data
        :rtype: dict
        """
        data = {}
        hue_data = self.get_data_from_hue_bridge()

        # parse the sensor data
        for device in hue_data["sensors"].values():
            name = self.hue_device_name_to_name(device["name"])
            if device["type"] == "ZLLTemperature":
                # convert temperature to the desired units
                celsius = device["state"]["temperature"] / 100
                if self.settings["hue"].get("temperature_units") == "F":
                    data[name] = round((celsius * 1.8) + 32, 2)
                elif self.settings["hue"].get("temperature_units") == "K":
                    data[name] = round(celsius + 273.15, 2)
                else:
                    data[name] = round(celsius, 2)
            elif device["type"] == "ZLLLightLevel":
                # convert light level to lux
                data[name] = round(float(10 ** ((device["state"]["lightlevel"] - 1) / 10000)), 2)
            elif device["type"] == "ZLLPresence":
                # convert presence to boolean 0 or 1
                data[name] = int(1 if device["state"]["presence"] else 0)

        for device in hue_data["lights"].values():
            name = self.hue_device_name_to_name(device["name"])
            # convert brightness to percentage if the light is dimmable (has a "bri" attribute)
            # otherwise boolean 0 or 1 to cover smart plugs which are also listed as lights
            data[name] = int(device["state"].get("bri", 2.54) / 2.54) if device["state"]["on"] else 0

        return data

    def _fetch_lights(self):
        """Return ``{light_id(str): light_object(dict)}`` for every light/plug the
        bridge exposes, via the collector's own authenticated GET. The light
        objects carry the ``state``/``type``/``capabilities`` used to resolve a
        target and check what it can do.

        :raises SourceConnectionError: if the bridge can't be reached
        """
        hue_data = self.get_data_from_hue_bridge()
        return {str(lid): light for lid, light in hue_data.get("lights", {}).items() if isinstance(light, dict)}

    @staticmethod
    def _names_by_id(lights):
        """Return ``{id: name}`` from a ``{id: light_object}`` map (a missing or
        blank name falls back to the id), for name/id resolution and error text."""
        return {light_id: str(light.get("name") or light_id) for light_id, light in lights.items()}

    @classmethod
    def _light_capabilities(cls, light):
        """Derive what a light can do from its bridge object.

        A Hue install spans (at least) three tiers - on/off-or-dimmable white,
        colour-temperature, and full colour - so brightness, colour temperature
        and colour are three *independent* capabilities, checked separately. They
        are inferred from the light's ``state`` keys (``bri``/``ct``/``xy`` etc.),
        cross-checked against ``capabilities.control`` where the bridge reports it;
        the ``ct`` mired range comes from ``capabilities.control.ct`` when present,
        else the standard range.

        :return: ``{"brightness","color_temp","color": bool, "ct_range": (min,max)|None}``
        :rtype: dict
        """
        state = light.get("state") if isinstance(light.get("state"), dict) else {}
        state = state or {}
        caps = light.get("capabilities")
        control = caps["control"] if isinstance(caps, dict) and isinstance(caps.get("control"), dict) else {}
        supports_brightness = "bri" in state
        supports_color_temp = "ct" in state or "ct" in control
        supports_color = any(k in state for k in ("xy", "hue", "sat")) or bool(control.get("colorgamut"))
        ct_range = None
        if supports_color_temp:
            ctrl_ct = control.get("ct") if isinstance(control.get("ct"), dict) else None
            if ctrl_ct and isinstance(ctrl_ct.get("min"), int) and isinstance(ctrl_ct.get("max"), int):
                ct_range = (ctrl_ct["min"], ctrl_ct["max"])
            else:
                ct_range = (cls.HUE_CT_MIN, cls.HUE_CT_MAX)
        return {
            "brightness": supports_brightness,
            "color_temp": supports_color_temp,
            "color": supports_color,
            "ct_range": ct_range,
        }

    @staticmethod
    def _kelvin_to_mirek(kelvin):
        """Convert a colour temperature in kelvin to Hue's mired/mirek scale."""
        return round(1_000_000 / kelvin)

    @staticmethod
    def _mirek_to_kelvin(mirek):
        """Convert Hue's mired/mirek scale to kelvin (for the capability listing)."""
        return round(1_000_000 / mirek)

    @classmethod
    def _color_to_xy(cls, color):
        """Convert a colour (an ``#rrggbb``/``rrggbb`` hex or a known name) to a
        Hue CIE ``xy`` pair.

        :raises ToolParamError: the value isn't a hex colour or a known name
        """
        if isinstance(color, str) and color.strip():
            hex_str = cls._HUE_COLOR_NAMES.get(color.strip().lower(), color.strip()).lstrip("#").lower()
            if len(hex_str) == 6 and all(c in "0123456789abcdef" for c in hex_str):
                r, g, b = (int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
                return cls._rgb_to_xy(r, g, b)
        raise ToolParamError(
            f"color must be an RGB hex like '#ff8800' or a known colour name (got {color!r}); "
            f"names: {', '.join(sorted(cls._HUE_COLOR_NAMES))}"
        )

    @staticmethod
    def _rgb_to_xy(r, g, b):
        """Convert 0-255 sRGB to a Hue CIE ``[x, y]`` pair (gamma-corrected sRGB ->
        XYZ -> xy chromaticity; the bridge clamps to the light's own gamut)."""

        def _linear(channel):
            c = channel / 255
            return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

        lr, lg, lb = _linear(r), _linear(g), _linear(b)
        x = lr * 0.4124 + lg * 0.3576 + lb * 0.1805
        y = lr * 0.2126 + lg * 0.7152 + lb * 0.0722
        z = lr * 0.0193 + lg * 0.1192 + lb * 0.9505
        total = x + y + z
        if total == 0:
            return [0.0, 0.0]
        return [round(x / total, 4), round(y / total, 4)]

    def mcp_list_writable_devices(self):
        """Return the controllable Hue lights/plugs, each with its id, name and the
        controls it supports - the write allowlist and the model's discovery of
        what each device can actually do (so it doesn't ask a white bulb for a
        colour). Reuses the collector's own authenticated bridge GET.

        :return: list of ``{"id", "name", "controls": [...]}`` (plus
            ``"color_temp_range_k": [min, max]`` for colour-temperature lights),
            sorted by id
        :rtype: list
        :raises SourceConnectionError: if the bridge can't be reached
        """
        lights = self._fetch_lights()
        out = []
        for light_id, light in sorted(lights.items()):
            caps = self._light_capabilities(light)
            controls = ["on_off"]
            for control in ("brightness", "color_temp", "color"):
                if caps[control]:
                    controls.append(control)
            entry = {"id": light_id, "name": str(light.get("name") or light_id), "controls": controls}
            if caps["color_temp"] and caps["ct_range"]:
                lo, hi = caps["ct_range"]
                entry["color_temp_range_k"] = [self._mirek_to_kelvin(hi), self._mirek_to_kelvin(lo)]
            out.append(entry)
        return out

    def _bri_from_percent(self, percent):
        """Map a 0-100 brightness percentage to Hue's 1-254 ``bri`` scale.

        0 % clamps to the minimum on-brightness rather than off - turning a light
        off is expressed with ``on=False``, not brightness 0, so the two controls
        stay independent and unambiguous.
        """
        scaled = round(percent / 100 * self.HUE_BRI_MAX)
        return max(self.HUE_BRI_MIN, min(scaled, self.HUE_BRI_MAX))

    def mcp_set_device_state(self, device, *, on=None, brightness_pct=None, color_temp_k=None, color=None):
        """Set a Hue light/plug's state, the MCP write action for this source.

        Resolves ``device`` (a bridge light id or its exact name) against the live
        device list, then builds and PUTs the Hue state body from the friendly
        parameters. It is *capability-aware per capability*: brightness, colour
        temperature and colour are independent, and asking for one the target light
        doesn't have is rejected (naming the device) rather than silently ignored.
        Setting brightness, colour temperature or colour implies turning the light
        on unless ``on`` is given explicitly, since the bridge ignores those on an
        off light.

        :param device: the target light id or its exact bridge name
        :type device: str
        :param on: turn the device on (True) or off (False); None leaves it
        :type on: bool or None
        :param brightness_pct: target brightness 0-100 %; None leaves it
        :type brightness_pct: int or float or None
        :param color_temp_k: white colour temperature in kelvin (e.g. 2700 warm,
            6500 cool), clamped to the light's supported range; None leaves it
        :type color_temp_k: int or float or None
        :param color: a colour as an ``#rrggbb`` hex or a known name; None leaves it
        :type color: str or None
        :return: a summary dict of what was applied
        :rtype: dict
        :raises ToolParamError: a caller/model input mistake - nothing to set, both
            colour and colour temperature at once, an invalid value, a capability
            the device lacks, or an unknown/ambiguous device (not retryable)
        :raises SourceConnectionError: a bridge/transport failure reaching the
            device list or PUTting the change (retryable)
        """
        if on is None and brightness_pct is None and color_temp_k is None and color is None:
            raise ToolParamError(
                "nothing to set: provide at least one of 'on', 'brightness_pct', 'color_temp_k', 'color'"
            )
        # ct and xy are mutually exclusive on the bridge (a light is in one mode);
        # asking for both is a caller mistake, not something to silently pick from.
        if color_temp_k is not None and color is not None:
            raise ToolParamError("set either 'color_temp_k' or 'color', not both (a light is one or the other)")

        lights = self._fetch_lights()
        names = self._names_by_id(lights)
        light_id = self._resolve_device_id(device, names)
        name = names[light_id]
        caps = self._light_capabilities(lights[light_id])

        state = {}
        if brightness_pct is not None:
            state.update(self._brightness_state(name, caps, brightness_pct))
        if color_temp_k is not None:
            state.update(self._color_temp_state(name, caps, color_temp_k))
        if color is not None:
            state.update(self._color_state(name, caps, color))
        if on is not None:
            if not isinstance(on, bool):
                raise ToolParamError(f"on must be true or false (got {on!r})")
            state["on"] = on
        elif state:
            # Brightness/ct/xy only take effect on a light that's on; default it on
            # unless the caller explicitly asked to turn it off.
            state["on"] = True

        self._put_light_state(light_id, state)
        return {"source": self.source, "device": name, "device_id": light_id, "applied": state}

    def _brightness_state(self, name, caps, brightness_pct):
        """Validate a brightness request against the light and return ``{"bri": ...}``.

        :raises ToolParamError: the light isn't dimmable, or the value is invalid
        """
        if not caps["brightness"]:
            raise ToolParamError(f"device {name!r} does not support brightness (it is on/off only)")
        if not isinstance(brightness_pct, (int, float)) or isinstance(brightness_pct, bool):
            raise ToolParamError(f"brightness_pct must be a number 0-100 (got {brightness_pct!r})")
        if not 0 <= brightness_pct <= 100:
            raise ToolParamError(f"brightness_pct must be between 0 and 100 (got {brightness_pct!r})")
        return {"bri": self._bri_from_percent(brightness_pct)}

    def _color_temp_state(self, name, caps, color_temp_k):
        """Validate a colour-temperature request and return ``{"ct": ...}`` (kelvin
        converted to mirek and clamped to the light's supported range).

        :raises ToolParamError: the light lacks colour temperature, or the value is invalid
        """
        if not caps["color_temp"]:
            raise ToolParamError(f"device {name!r} does not support colour temperature")
        if not isinstance(color_temp_k, (int, float)) or isinstance(color_temp_k, bool) or color_temp_k <= 0:
            raise ToolParamError(f"color_temp_k must be a positive number in kelvin (got {color_temp_k!r})")
        lo, hi = caps["ct_range"]
        return {"ct": max(lo, min(self._kelvin_to_mirek(color_temp_k), hi))}

    def _color_state(self, name, caps, color):
        """Validate a colour request and return ``{"xy": [...]}``.

        :raises ToolParamError: the light lacks colour, or the colour is invalid
        """
        if not caps["color"]:
            raise ToolParamError(f"device {name!r} does not support colour")
        return {"xy": self._color_to_xy(color)}

    @staticmethod
    def _resolve_device_id(device, devices):
        """Resolve a device id or exact name to a bridge light id, or raise.

        An id match wins over a name match. A name that isn't unique is rejected
        rather than guessed at, since actuating the wrong light is not a
        recoverable mistake.

        :raises ToolParamError: the device is empty, unknown, or an ambiguous name
        """
        if not isinstance(device, str) or not device.strip():
            raise ToolParamError(f"device must be a non-empty light id or name (got {device!r})")
        if device in devices:
            return device
        matches = [light_id for light_id, name in devices.items() if name == device]
        if len(matches) == 1:
            return matches[0]
        available = ", ".join(f"{name!r} (id {light_id})" for light_id, name in sorted(devices.items())) or "(none)"
        if len(matches) > 1:
            raise ToolParamError(f"device name {device!r} is ambiguous; use the light id. Devices: {available}")
        raise ToolParamError(f"unknown device {device!r}; available devices: {available}")

    def _put_light_state(self, light_id, state):
        """PUT a state body to a light and surface any bridge-reported error.

        Uses the collector's own session/auth and the same TLS-verification
        policy as the reads (``hue.insecure``, default true for a local
        self-signed bridge cert).

        :raises SourceConnectionError: on a transport failure or a bridge error
            (the CLIP API returns 200 with a list of per-key success/error items)
        """
        insecure = self.settings["hue"].get("insecure", True)
        url = f"{self._api_base()}/lights/{light_id}/state"
        try:
            with warnings.catch_warnings():
                if insecure:
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                response = self.session.put(
                    url,
                    json=state,
                    timeout=self.settings["hue"].get("timeout", 5),
                    verify=not insecure,
                )
            response.raise_for_status()
            result = response.json()
        except ValueError as e:
            # response.json() on a non-JSON body raises requests' JSONDecodeError,
            # which is BOTH a ValueError and a RequestException - catch it before
            # the RequestException handler so a parse failure isn't misreported as
            # a transport error. (raise_for_status()'s HTTPError is a
            # RequestException but not a ValueError, so it still falls through.)
            logging.error("Hue Bridge returned an unparseable response to a write - %s", self._redact(str(e)))
            raise SourceConnectionError(self._redact(f"Hue Bridge returned an unparseable response: {e}")) from e
        except requests.exceptions.RequestException as e:
            logging.error("Error writing to Hue Bridge - %s", self._redact(str(e)))
            raise SourceConnectionError(self._redact(str(e))) from e
        # The CLIP API always answers a state PUT with a JSON *list* of per-key
        # success/error items. A non-list body is unexpected and must fail cleanly
        # rather than being read as success (an empty error list) by the scan below.
        if not isinstance(result, list):
            logging.error("Hue Bridge returned an unexpected response shape to a write - %.200r", result)
            raise SourceConnectionError(f"Hue Bridge returned an unexpected response: {result!r:.200}")
        # Guard item["error"] being a non-dict (a malformed bridge/proxy response):
        # fall back to its string form rather than crashing on .get(), mirroring the
        # read path's defensive handling in get_data_from_hue_bridge().
        errors = [
            (
                item["error"].get("description", str(item["error"]))
                if isinstance(item["error"], dict)
                else str(item["error"])
            )
            for item in result
            if isinstance(item, dict) and "error" in item
        ]
        if errors:
            logging.error("Hue Bridge rejected a write to light %s - %s", light_id, "; ".join(errors))
            raise SourceConnectionError(f"Hue Bridge rejected the write: {'; '.join(errors)}")
        return result
