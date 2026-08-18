"""General functions for sending data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

# pylint: disable=import-outside-toplevel
import copy
import ipaddress
import logging
import os
import stat
import sys
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse
import yaml
from toinflux.credentials import (
    CREDENTIAL_FIELDS,
    SENTINEL_PREFIX,
    apply_credential_substitution,
    credential_field,
    placeholder_for,
    slot_credential_names,
)
from toinflux.exceptions import ConfigError

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 3


def configure_logging(
    logfile=None, loglevel="INFO", log_max_bytes=DEFAULT_LOG_MAX_BYTES, log_backup_count=DEFAULT_LOG_BACKUP_COUNT
):
    """Configure root logger with a stderr handler and an optional rotating file handler.

    Diagnostics go to **stderr**; stdout is reserved for the program's own output
    (``--dump``/``--print`` JSON, ``--check-config``'s verdict), so a caller can parse it
    while failures are still reported. Every level goes to stderr, not just errors.

    :param logfile: path to log file; if None, logs to stderr only
    :type logfile: str or None
    :param loglevel: logging level name (e.g. "INFO", "DEBUG"); falls back to INFO if invalid
    :type loglevel: str
    :param log_max_bytes: max size in bytes before the log file is rotated
    :type log_max_bytes: int
    :param log_backup_count: number of rotated log files to keep
    :type log_backup_count: int
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()

    resolved_level = getattr(logging, str(loglevel).upper(), None)
    if not isinstance(resolved_level, int):
        logging.warning("Invalid loglevel '%s'; defaulting to INFO", loglevel)
        resolved_level = logging.INFO
    root.setLevel(resolved_level)

    # Remove any handlers added by a previous call to this function, so repeated
    # calls (e.g. in tests, or if main() is invoked more than once) don't duplicate log lines.
    for handler in list(root.handlers):
        if getattr(handler, "_send_to_influx_handler", False):
            root.removeHandler(handler)
            handler.close()

    # stderr, not stdout: stdout carries the program's *data* - --dump/--print JSON and
    # --check-config's verdict - and a caller has to be able to parse it. Sharing the stream
    # made a partial-failure dump unparseable, since the failure it reports lands in the
    # middle of the payload it still produces. Every level goes here, not just errors:
    # diagnostics are diagnostics, and splitting them across two streams by severity would
    # interleave unpredictably. Under systemd both streams reach the journal (the unit pins
    # neither), and the rsyslog rule matches on programname rather than stream, so
    # journalctl and /var/log/send-to-influx.log are unaffected.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    stderr_handler._send_to_influx_handler = True
    root.addHandler(stderr_handler)

    if logfile:
        try:
            file_handler = RotatingFileHandler(logfile, maxBytes=log_max_bytes, backupCount=log_backup_count)
        except OSError as exc:
            raise ConfigError(
                f"Cannot open logfile '{logfile}' for writing ({exc.strerror or exc}). If this is the "
                "packaged systemd service, only /etc/send-to-influx/ is writable by default - see the "
                "README's 'Running as a systemd service' section for how to log to a file under systemd."
            ) from exc
        file_handler.setFormatter(fmt)
        file_handler._send_to_influx_handler = True
        root.addHandler(file_handler)


def flatten_dict(data, parent_key="", sep="_"):
    """Flatten a nested dictionary into a single-level dictionary.

    Nested keys are joined with ``sep``. Non-dictionary values are copied
    directly to the flattened output.

    :param data: dictionary to flatten
    :type data: dict
    :param parent_key: prefix used during recursion
    :type parent_key: str
    :param sep: separator for nested keys
    :type sep: str
    :return: flattened dictionary
    :rtype: dict
    """
    flattened = {}

    for key, value in data.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else str(key)
        if isinstance(value, dict):
            flattened.update(flatten_dict(value, parent_key=new_key, sep=sep))
        else:
            flattened[new_key] = value

    return flattened


def get_class(source, settings_file=None, instance=None):
    """
    Create and return a class object for the given data source name

    This function modifies the case of the source so that the user can
    input this in any case and it will still work.

    When adding a new data source, import its class inside this function
    and add it to the classes dictionary.

    :param source: data source name
    :type source: str
    :param settings_file: path to the settings file (default: settings.yaml in the project root)
    :type settings_file: str or None
    :param instance: which instance of the source this handler serves, for a source that
        can have several targets behind one settings block (only Hue today, whose instance
        is a bridge host). ``None`` - the default, and what every caller that does not care
        about instances passes - means the source's single target, or for Hue the first
        configured bridge, which is what keeps single-bridge installs and the MCP tools
        behaving exactly as they did before slots existed.
    :return: class object
    :rtype: DataHandler
    """
    return source_class(source)(source.lower(), settings_file=settings_file, instance=instance)


def source_class(source):
    """
    Return the DataHandler subclass for a source name, without constructing it.

    Separated from ``get_class()`` so a caller that only needs the class's static domain
    knowledge - the MCP read layer asking which measurement a source writes to - can get it
    without building a handler, which loads and validates settings and opens a session. One
    mapping serves both, so the two cannot disagree about which class a name means.

    Imports live inside the function for the same reason they do in ``get_class()``: these
    modules import ``influx``, which imports this one, so a module-level import is circular.

    :param source: data source name, any case
    :type source: str
    :return: the DataHandler subclass
    :rtype: type
    :raises ConfigError: the name is not a known source
    """
    classes = _source_classes()
    class_name = next((k for k in classes if k.lower() == source.lower()), source)
    try:
        return classes[class_name]
    except KeyError:
        raise ConfigError(f"Source {class_name} not found") from None


def _source_classes():
    """
    Return the source-name to class mapping - the single registration point.

    Every caller that needs to know what sources exist, or which class one means, reads it
    from here: ``get_class()``, ``source_class()`` and ``known_sources()``. A second copy of
    the names would drift the moment a source was added, which is precisely why the mapping
    *is* the registration rather than being accompanied by a list.

    Imports live inside the function because these modules import ``influx``, which imports
    this one, so a module-level import is circular.

    :return: class name to class
    :rtype: dict
    """
    from toinflux.carbonintensity import CarbonIntensity
    from toinflux.myenergi import MyEnergi, Zappi, Eddi, Harvi
    from toinflux.nuki import Nuki
    from toinflux.octopus import Octopus
    from toinflux.openmeteo import OpenMeteo
    from toinflux.philipshue import Hue
    from toinflux.speedtest import Speedtest

    return {
        "CarbonIntensity": CarbonIntensity,
        "Eddi": Eddi,
        "Harvi": Harvi,
        "Hue": Hue,
        "MyEnergi": MyEnergi,
        "Nuki": Nuki,
        "Octopus": Octopus,
        "OpenMeteo": OpenMeteo,
        "Speedtest": Speedtest,
        "Zappi": Zappi,
    }


def measurement_for(source):
    """
    Return the InfluxDB measurement a source writes to, from its class alone.

    ``MCP_MEASUREMENT`` when the class overrides it, else the source name - the same rule
    ``build_schema()`` applies, kept here so a caller that has no handler can ask.

    :param source: data source name, any case
    :type source: str
    :return: the measurement name
    :rtype: str
    :raises ConfigError: the name is not a known source
    """
    return source_class(source).MCP_MEASUREMENT or source.lower()


def known_sources():
    """
    Return every source name this build knows about, lowercased.

    Read from the one class mapping, so it cannot drift from what ``get_class()`` accepts.

    :return: sorted source names
    :rtype: list
    """
    # Read from the one mapping, never a second list - see _source_classes(). MyEnergi is
    # excluded because it is the abstract parent of the three device types, not a
    # collectable source of its own; get_class() accepts the name but there is nothing to
    # collect under it.
    return sorted(name.lower() for name in _source_classes() if name != "MyEnergi")


def shares_measurement(source):
    """
    Return True when any *other* known source writes to the same measurement.

    Derived from the classes rather than declared on them, and deliberately from every
    *known* source rather than the currently configured ones. Sharing is a property of the
    software, not of one install: a database can still hold eddi history after eddi is
    removed from ``sources:``, so deciding by what happens to be configured today would let
    a tag value discovered in the data be attributed to the wrong type tomorrow. Found by
    testing exactly that case.

    The read layer uses this to know when a discovered tag value cannot be attributed to a
    single source, and must therefore trust the configuration instead. A class flag would
    have said the same thing but could fall out of step; this covers a future shared
    measurement without anyone remembering to mark it.

    :param source: the source in question
    :type source: str
    :return: True when the measurement is shared with another known source
    :rtype: bool
    """
    try:
        measurement = measurement_for(source)
    except ConfigError:
        return False
    for other in known_sources():
        if other == source.lower():
            continue
        try:
            if measurement_for(other) == measurement:
                return True
        except ConfigError:
            continue
    return False


# Sources that collect over MQTT and therefore need the shared top-level mqtt block.
# When adding a new MQTT-based source (a MqttDataHandler child), add its name here so
# validate_settings()/--check-config can catch a missing broker config up front rather
# than letting the collector fail at runtime.
MQTT_SOURCES = frozenset({"nuki"})


def _hue_bridge_hosts(settings):
    """
    Return the host of every usable configured Hue bridge - one per worker.

    Imported inside the function, like ``get_class()`` does: ``philipshue`` imports
    ``influx``, which imports this module, so a module-level import would be circular.

    Errors and warnings from enumeration are deliberately dropped: ``validate_settings()``
    has already reported them (fatally, for the errors), and this function's only job is to
    say what will actually run. A bridge whose token is missing yields no host, so it is
    simply not collected - which is exactly the warning validation already emitted.

    :param settings: parsed settings dictionary
    :type settings: dict
    :return: bridge hosts, empty when none is usable
    :rtype: list
    """
    from toinflux.philipshue import enumerate_bridges

    bridges, _, _ = enumerate_bridges(settings.get("hue"))
    return [bridge.host for bridge in bridges]


# Sources that can have more than one target behind a single settings block, and so run one
# worker per target rather than one per source - mapped to the function that enumerates
# those targets. Only Hue today: one worker per bridge, so that one unreachable bridge
# cannot stop the others.
#
# The mapping *is* the registration: membership and expansion behaviour are the same
# structure, so a source cannot be listed as instanced while still being expanded as a
# single unit. Add a source by adding its enumerator here, and nothing else can be
# forgotten.
_INSTANCE_ENUMERATORS = {"hue": _hue_bridge_hosts}

# Derived, never hand-maintained - see above.
INSTANCED_SOURCES = frozenset(_INSTANCE_ENUMERATORS)


def _source_instances(source, settings):
    """
    Return the instance values a single source expands to.

    ``[None]`` for an ordinary single-target source. For an instanced source, one entry per
    configured target - and an **empty** list when it has none, which means the source is
    simply not collected rather than collected against a broken target.

    :param source: source name, already lowercased
    :type source: str
    :param settings: parsed settings dictionary
    :type settings: dict
    :return: instance values for this source
    :rtype: list
    """
    enumerator = _INSTANCE_ENUMERATORS.get(source)
    return enumerator(settings) if enumerator is not None else [None]


def expand_sources(sources, settings):
    """
    Expand configured source names into the work units the runtime actually runs.

    A work unit is ``(source, instance)`` - the same shape as
    ``DataHandler.worker_key`` - and each one becomes exactly one worker. Most sources
    yield a single ``(name, None)`` unit; Hue yields one per configured bridge, so that a
    bridge that is unreachable backs off on its own without stopping the others.

    The single source of truth for "what runs", used by the multi-source supervisor, the
    single-source path and the one-shot CLI modes alike. If any of those enumerated
    instances for themselves they would eventually disagree with each other and with
    ``validate_settings()`` about what is actually configured.

    A source that expands to nothing (Hue with no usable bridge) is absent from the
    result: it is not collected, validation has already warned why, and every other
    source is unaffected. An empty ``sources`` list expands to no work units at all -
    a valid "nothing configured" state, not an error.

    :param sources: configured source names, already lowercased
    :type sources: list
    :param settings: parsed settings dictionary
    :type settings: dict
    :return: ``[(source, instance), ...]``, one entry per worker
    :rtype: list
    """
    units = []
    for source in sources:
        units.extend((source, instance) for instance in _source_instances(source, settings))
    return units


def mqtt_block_errors(settings, context=""):
    """
    Return a list of error strings for the shared ``mqtt`` settings block itself -
    its own type, ``broker_host`` presence and type, ``username``/``password`` types,
    and ``broker_port`` type and range - independent of which sources happen to need
    it. The type checks matter because YAML coerces silently (``broker_host: 10.0``
    is a float, ``broker_host: yes`` is a bool) and a non-string reaches paho as a
    raw TypeError that the transport's connection-error handling can't catch.

    Shared by ``validate_settings()`` (config-check time) and
    ``MqttDataHandler.collect_mqtt_messages()`` (runtime), deliberately: those are two
    genuinely different entry points, since ``load_settings()`` only validates the
    *configured* sources - a one-off ``--source nuki`` on an install where nuki isn't
    in ``sources:`` reaches the transport without this block ever having been checked.
    Keeping one copy of the rules means the two can't drift.

    :param settings: parsed settings dictionary
    :type settings: dict
    :param context: optional suffix for the broker_host message (e.g. which sources
        required the block), used by validate_settings()
    :type context: str
    :return: error strings, empty when the block is usable
    :rtype: list
    """
    mqtt = settings.get("mqtt")
    if mqtt is None:
        mqtt = {}
    if not isinstance(mqtt, dict):
        return [f"mqtt must be a mapping of broker settings (got {type(mqtt).__name__})"]
    errors = []
    host = mqtt.get("broker_host")
    # "Absent" is None or a blank string only - a falsy *non*-string (broker_host: no
    # is False in YAML, broker_host: 0 is an int) is something the user did write, and
    # deserves the type error rather than being misreported as missing.
    if host is None or (isinstance(host, str) and not host.strip()):
        errors.append(f"mqtt.broker_host is required for MQTT-based sources{context}")
    elif not isinstance(host, str):
        # YAML coerces more than you'd expect - `broker_host: 10.0` is a float and
        # `broker_host: yes` is a bool - and a non-string reaches paho as a raw
        # TypeError the transport's OSError/ValueError handling doesn't catch.
        errors.append(f"mqtt.broker_host must be a string (got {host!r})")
    for field in ("username", "password"):
        value = mqtt.get(field)
        if value is not None and not isinstance(value, str):
            # Same coercion trap: a numeric-looking broker username is plausible,
            # and paho would fail on .encode() rather than anything catchable.
            errors.append(f"mqtt.{field} must be a string (got {value!r})")
    port = mqtt.get("broker_port", 1883)
    # bool is an int subclass, so broker_port: true would otherwise pass as 1
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append(f"mqtt.broker_port must be an integer between 1 and 65535 (got {port!r})")
    return errors


def _validate_hue_bridges(settings, sources):
    """Return ``(errors, warnings)`` for the ``hue`` block's bridge slots, checked only
    when hue is among the sources being validated.

    Delegates to ``toinflux.philipshue.enumerate_bridges()`` rather than re-deriving the
    slot rules here: the runtime enumerates bridges with that same function, and two
    separate implementations would eventually disagree about what is configured.

    Imported inside the function, like ``get_class()`` does: ``philipshue`` imports
    ``influx``, which imports this module, so a module-level import would be circular.

    Only self-contradictory configuration is an error. An unusable bridge - no host, or a
    host with a placeholder/blank token - is a warning instead, because
    ``example_settings.yaml``'s ``hue:`` block still ships the placeholder host/token
    pair, so enabling ``hue`` in ``sources:`` without also filling those in is exactly
    that state: raising would stop every other collector along with the unconfigured
    one.
    Returns ``(errors, warnings)``; the caller decides whether to surface the warnings,
    because ``validate_settings()`` runs inside ``load_settings()`` and therefore on
    every ``DataHandler`` construction - logging from here would repeat the same line per
    source at startup and again on every failure-triggered rebuild.
    """
    # Absent section: _validate_source_block already reports "no configuration section
    # found for source 'hue'", which is both accurate and sufficient. Enumerating a
    # missing block would add a second error saying it "must be a mapping (got NoneType)",
    # which is true but misleading - the problem is that it isn't there, not that it's the
    # wrong type. One cause, one message.
    if "hue" not in sources or "hue" not in settings:
        return ([], [])
    from toinflux.philipshue import enumerate_bridges

    _, errors, bridge_warnings = enumerate_bridges(settings.get("hue"))
    return (errors, bridge_warnings)


def _validate_mqtt_block(settings, sources):
    """Return a list of error strings for the shared mqtt block, which is required
    if (and only if) an MQTT-based source is among the sources being validated."""
    mqtt_sources = sorted(str(src) for src in sources if src in MQTT_SOURCES)
    if not mqtt_sources:
        return []
    return mqtt_block_errors(settings, f" ({', '.join(mqtt_sources)})")


# The MCP server only ever binds a private interface - TLS termination and the
# internet-facing side belong to the deploying user's reverse proxy, so a public
# bind would serve plain-HTTP OAuth (credentials included) to the network. This is
# a refusal, not a warning, and deliberately has no override: there is no valid
# configuration in which send-to-influx itself should listen publicly.
MCP_DISALLOWED_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})
MCP_DEFAULT_BIND_ADDRESS = "127.0.0.1:8420"


def mcp_enabled(settings):
    """Return True when the MCP server is enabled - both ``mcp.user`` and
    ``mcp.password`` set to non-blank strings, and ``mcp.disabled`` not set to
    ``true``. Credentials-present is the primary enablement mechanism;
    ``mcp.disabled`` is a forced-off override on top of it (see
    ``mcp_block_errors()``) for a source whose password was migrated to
    systemd-creds - blanking the YAML fields alone doesn't disable it there,
    since the credential still gets substituted in at load time, so a plain
    kill switch that doesn't depend on credential state is needed too.

    :param settings: parsed settings dictionary (after credential substitution)
    :type settings: dict
    :rtype: bool
    """
    mcp = settings.get("mcp")
    if not isinstance(mcp, dict):
        return False
    if mcp.get("disabled") is True:
        return False
    user = mcp.get("user")
    password = mcp.get("password")
    return bool(isinstance(user, str) and user.strip() and isinstance(password, str) and password.strip())


def _split_bind_address(value, original):
    """Split a bind-address string into ``(host, port_text)``, handling both
    ``host:port`` and bracketed IPv6 ``[addr]:port``.

    :raises ConfigError: if the shape is not one of those two forms
    """
    if value.startswith("["):
        closing = value.find("]")
        if closing == -1 or not value[closing + 1 :].startswith(":"):
            raise ConfigError(f"mcp.bind_address must be host:port or [ipv6]:port (got {original!r})")
        return value[1:closing], value[closing + 2 :]
    host, sep, port_text = value.rpartition(":")
    if not sep:
        raise ConfigError(f"mcp.bind_address must be host:port (got {original!r})")
    if ":" in host:
        # A colon still in the host portion means an unbracketed IPv6 literal:
        # rpartition would have split "2001:db8::1" into host "2001:db8:", port
        # "1" (a surprising bind), and "::1:8420" would slip through as a host.
        # IPv6 must be bracketed so host and port are unambiguous.
        raise ConfigError(f"mcp.bind_address IPv6 literals must be bracketed as [ipv6]:port (got {original!r})")
    return host, port_text


def parse_mcp_bind_address(bind_address):
    """Split an ``mcp.bind_address`` value into ``(host, port)``.

    Accepts ``host:port`` and bracketed IPv6 ``[addr]:port``. Raises ConfigError
    rather than returning a partial result - shared by ``mcp_block_errors()``
    (config-check time) and the server startup path (runtime), so the two cannot
    disagree about what parses.

    :param bind_address: the configured value, or None/"" for the default
    :type bind_address: str or None
    :return: (host, port) tuple
    :rtype: tuple
    :raises ConfigError: if the value is not a usable host:port pair
    """
    if bind_address is None or (isinstance(bind_address, str) and not bind_address.strip()):
        bind_address = MCP_DEFAULT_BIND_ADDRESS
    if not isinstance(bind_address, str):
        raise ConfigError(f"mcp.bind_address must be a string (got {bind_address!r})")
    host, port_text = _split_bind_address(bind_address.strip(), bind_address)
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(f"mcp.bind_address port must be an integer (got {bind_address!r})") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"mcp.bind_address port must be between 1 and 65535 (got {bind_address!r})")
    if not host:
        raise ConfigError(f"mcp.bind_address host must not be empty (got {bind_address!r})")
    _reject_public_bind_host(host, bind_address)
    return host, port


def _reject_public_bind_host(host, bind_address):
    """Refuse a bind host that would expose the plain-HTTP MCP server publicly.

    Refuses the any-interface wildcards (0.0.0.0/::) and any globally-routable IP
    literal - binding plain-HTTP OAuth/login there would put it on the network in
    cleartext. Loopback and private/LAN addresses are allowed (a reverse proxy on
    another host legitimately reaches the app on a private IP). A non-IP hostname
    can't be classified without a DNS lookup (fragile, and it may resolve
    differently at bind time), so it is allowed with a warning.

    :raises ConfigError: for an any-interface or globally-routable bind host
    """
    if host in MCP_DISALLOWED_BIND_HOSTS:
        raise ConfigError(
            f"mcp.bind_address must not bind a public interface (got {bind_address!r}) - the MCP "
            "server speaks plain HTTP and is meant to sit behind your own TLS-terminating reverse "
            "proxy; bind a loopback or private address instead"
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        logging.warning(
            "mcp.bind_address host %r is not an IP literal and can't be checked for public "
            "exposure; make sure it resolves to a loopback or private address - the MCP server "
            "speaks plain HTTP and must sit behind your own TLS-terminating reverse proxy",
            host,
        )
        return
    if ip.is_global:
        raise ConfigError(
            f"mcp.bind_address must not bind a public interface (got {bind_address!r}): {host} is "
            "globally routable and the MCP server speaks plain HTTP. Bind a loopback or private "
            "address and put your own TLS-terminating reverse proxy in front of it instead"
        )


def mcp_block_errors(settings):
    """Return a list of error strings for the optional ``mcp`` settings block.

    An absent block, or one with both ``user`` and ``password`` blank, is a valid
    disabled state. Set together they enable the server, which then requires a
    ``public_url`` (the external HTTPS address the reverse proxy serves - OAuth
    discovery metadata must advertise it, so there is no default to fall back to).
    One of the pair set without the other is incoherent and reported, mirroring
    the MQTT username-without-password check - *unless* ``mcp.disabled`` is
    explicitly ``true``, checked first and short-circuiting every other check:
    a forced-off override for a source whose password lives in systemd-creds,
    where blanking the YAML fields alone can't reach a coherent disabled state
    without also removing the stored credential. This also doubles as a quick
    kill switch during troubleshooting, independent of credential state.

    :param settings: parsed settings dictionary (after credential substitution)
    :type settings: dict
    :return: error strings, empty when the block is valid
    :rtype: list
    """
    mcp = settings.get("mcp")
    if mcp is None:
        return []
    if not isinstance(mcp, dict):
        return [f"mcp must be a mapping of MCP server settings (got {type(mcp).__name__})"]
    disabled = mcp.get("disabled")
    if disabled is not None and not isinstance(disabled, bool):
        return [f"mcp.disabled must be true or false (got {disabled!r})"]
    if disabled is True:
        return []
    errors = []
    for field in ("user", "password", "public_url", "bind_address", "state_file"):
        value = mcp.get(field)
        if value is not None and not isinstance(value, str):
            # Same YAML-coercion trap as the mqtt block: an unquoted numeric or
            # yes/no value arrives as int/float/bool, not the string the code needs.
            errors.append(f"mcp.{field} must be a string (got {value!r})")
    user = mcp.get("user")
    password = mcp.get("password")
    user_set = isinstance(user, str) and user.strip()
    password_set = isinstance(password, str) and password.strip()
    if bool(user_set) != bool(password_set):
        errors.append(
            "mcp.user and mcp.password must be set together to enable the MCP server "
            "(one without the other is never valid). If the password was migrated to "
            "systemd-creds, check 'send-to-influx-set-credential --list' - a missing "
            "credential file leaves the password blank here. Alternatively, set "
            "mcp.disabled: true to force the server off regardless of credential state."
        )
    if user_set and password_set:
        errors.extend(_mcp_enabled_block_errors(mcp))
    return errors


def _mcp_enabled_block_errors(mcp):
    """Return the error strings that only apply once the MCP server is enabled:
    a usable public_url and a parseable, non-public bind_address."""
    errors = []
    public_url = mcp.get("public_url")
    if not (isinstance(public_url, str) and public_url.strip()):
        errors.append(
            "mcp.public_url is required when the MCP server is enabled - the external "
            "https:// URL your reverse proxy serves, e.g. https://mcp.example.org"
        )
    elif not public_url.strip().startswith("https://"):
        errors.append(
            f"mcp.public_url must be an https:// URL (got {public_url!r}) - the public side "
            "of the MCP server is always TLS, terminated by your reverse proxy"
        )
    else:
        # More than scheme + host[:port] silently breaks things downstream: the
        # OAuth routes are mounted at the root of this address, so a path would
        # advertise endpoints that 404, and userinfo/query/fragment would leak
        # into the issuer and the Host/Origin allowlists. Reject at config time.
        parsed = urlparse(public_url.strip())
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.path.rstrip("/")
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            errors.append(
                f"mcp.public_url must be just https://host[:port] with no path, credentials, "
                f"query or fragment (got {public_url!r}) - the OAuth endpoints are served at "
                "the root of that address"
            )
    try:
        parse_mcp_bind_address(mcp.get("bind_address"))
    except ConfigError as exc:
        errors.append(str(exc))
    return errors


def _validate_influx_block(influx):
    """Return a list of error strings for the influx configuration block."""
    errors = []
    if not influx.get("url"):
        errors.append("influx.url is required")
    if influx.get("token"):
        if not influx.get("org"):
            errors.append("influx.org is required when using token authentication (v2)")
    elif not (influx.get("user") and influx.get("password")):
        errors.append("influx requires either token+org (v2) or user+password (v1)")
    return errors


def _validate_source_block(source, settings, is_v2):
    """Return a list of error strings for a single source configuration section.

    :param is_v2: whether the influx block is configured for v2 (token) auth - v2's
        send_data() accepts either db or bucket (falling back from bucket to db), but
        v1's send_data() reads source_settings["db"] directly with no fallback, so a
        v1 config needs db specifically, not just "db or bucket"
    :type is_v2: bool
    """
    if not source:
        return []
    if source not in settings:
        return [f"no configuration section found for source '{source}'"]
    errors = []
    source_cfg = settings[source]
    if "interval" not in source_cfg:
        errors.append(f"{source}.interval is required")
    if is_v2:
        if "db" not in source_cfg and "bucket" not in source_cfg:
            errors.append(f"{source}.db (or {source}.bucket for InfluxDB v2) is required")
    elif "db" not in source_cfg:
        errors.append(f"{source}.db is required when using InfluxDB v1 (user/password) authentication")
    # mcp_read_write gates the MCP device-write tools and is checked with a strict
    # `is True`, so a mistyped `mcp_read_write: "true"` (string) would silently
    # leave writes off. Fail loud instead - a user who set it meant to enable it.
    if "mcp_read_write" in source_cfg and not isinstance(source_cfg["mcp_read_write"], bool):
        errors.append(f"{source}.mcp_read_write must be true or false (got {source_cfg['mcp_read_write']!r})")
    return errors


def _log_config_warnings(warnings_found, settings_path, warn):
    """Log non-fatal configuration warnings, but only when the caller asked for them.

    Opt-in because ``validate_settings()`` runs inside ``load_settings()`` and therefore
    on every ``DataHandler`` construction: logging unconditionally would repeat the same
    line once per source at startup and again on every failure-triggered rebuild, which
    the logging policy's bounded-volume rule rules out. ``--check-config`` opts in, since
    reporting on the configuration is its entire job; at collection time the effective
    bridge list is reported once by the worker spawner instead.

    :param warnings_found: warning messages collected during validation
    :type warnings_found: list
    :param settings_path: settings file path, used to label the message
    :type settings_path: str
    :param warn: whether to emit them at all
    :type warn: bool
    """
    if not warn or not warnings_found:
        return
    for warning in warnings_found:
        logging.warning("%s: %s", settings_path, warning)


def validate_settings(settings, source=None, settings_path="settings.yaml", warn=False):
    """Validate required keys in a parsed settings dictionary.

    :param settings: parsed settings dictionary
    :type settings: dict
    :param source: an additional specific source to validate (e.g. the --source CLI
        argument), even if it isn't in the configured sources list - without
        this, --check-config --source <x> could report success while <x>'s own block
        is broken, if <x> isn't part of sources:
    :type source: str or None
    :param settings_path: path to the settings file, used only to label log messages -
        settings can come from a location other than settings.yaml (--settings, or the
        .yml fallback), so this shouldn't be hard-coded in the log output
    :type settings_path: str
    :param warn: whether to log non-fatal configuration warnings (e.g. a Hue bridge whose
        token isn't set, so it won't be collected). Off by default because this function
        runs inside ``load_settings()``, which every ``DataHandler`` construction calls -
        only ``--check-config`` opts in, so the same line isn't repeated per source and
        per retry
    :type warn: bool
    :raises ConfigError: if any required settings are missing or invalid
    """
    influx = settings.get("influx", {})
    errors = _validate_influx_block(influx)
    is_v2 = bool(influx.get("token"))
    # Normalise case to match the runtime path: get_class()/--source are explicitly
    # case-insensitive (source_name is lowercased before instantiation), so validation
    # must be too - otherwise --check-config --source Hue fails while --source Hue
    # runs fine. Also makes the duplicate check catch case variants (['Hue', 'hue']).
    raw_sources = settings.get("sources")
    if raw_sources is not None and not isinstance(raw_sources, list):
        # A scalar (sources: hue) or mapping would otherwise be iterated by
        # character/key below - report it as the ConfigError it is, then treat it as
        # absent so the rest of validation still runs sensibly.
        errors.append(f"sources must be a list (got {type(raw_sources).__name__})")
        raw_sources = None
    # An absent or empty sources list is a valid "nothing configured" state - there is
    # nothing to validate here; sendtoinflux.py logs it plainly and exits rather than
    # starting a worker (see _exit_if_nothing_to_collect()).
    sources = raw_sources or []
    # A non-string entry (e.g. a YAML mapping, or an explicit null, from a malformed
    # sources list) would raise a raw TypeError from the dict/set membership tests
    # below - report it as the ConfigError it really is, and validate the remaining
    # string entries.
    invalid = [src for src in sources if not isinstance(src, str)]
    if invalid:
        errors.append("sources entries must be strings (got: " + ", ".join(repr(s) for s in invalid) + ")")
    # A blank/whitespace-only entry (e.g. sources: [""]) is a string, so it survives
    # the check above, but _validate_source_block() returns early for a falsy source
    # name - meaning it would otherwise validate cleanly and then expand into a real
    # work unit at runtime with an empty name (a confusing "workers=" startup log and
    # an eventual "unknown source" failure from get_class(), rather than a clear
    # config-time error). Reject it the same way as a non-string entry.
    blank = [src for src in sources if isinstance(src, str) and not src.strip()]
    if blank:
        errors.append(f"sources entries must not be blank (got {len(blank)} blank entry/entries)")
    sources = [src.lower() for src in sources if isinstance(src, str) and src.strip()]
    if source:
        source = source.lower()
    duplicates = sorted({str(src) for src in sources if sources.count(src) > 1})
    if duplicates:
        # A duplicated entry would spawn two worker threads sharing one source name -
        # and, since the write buffer is keyed by source name, sharing one buffer
        # without a lock. There's never a reason to list a source twice (both entries
        # would read the same settings block), so fail fast rather than race.
        errors.append(f"sources contains duplicate entries: {', '.join(duplicates)}")
    if source and source not in sources:
        sources = [*sources, source]
    for src in sources:
        errors.extend(_validate_source_block(src, settings, is_v2))
    hue_errors, hue_warnings = _validate_hue_bridges(settings, sources)
    errors.extend(hue_errors)
    errors.extend(_validate_mqtt_block(settings, sources))
    errors.extend(mcp_block_errors(settings))
    _log_config_warnings(hue_warnings, settings_path, warn)
    if errors:
        for error in errors:
            logging.critical("%s: %s", settings_path, error)
        raise ConfigError("; ".join(errors))


def _contains_real_secret(settings):
    """Return True if any known credential field holds something that looks like a
    real, user-entered secret - not empty, not a placeholder, not a systemd-creds
    sentinel.

    :param settings: settings dictionary to inspect
    :type settings: dict
    :rtype: bool
    """
    # Slot credentials are included, not just the static table: a real token hand-written
    # into hue.user2 must count, or the group/other-readable check below would pass a file
    # that does contain a secret.
    for name in [*CREDENTIAL_FIELDS, *slot_credential_names(settings)]:
        top_key, field = credential_field(name)
        block = settings.get(top_key)
        if not isinstance(block, dict):
            continue
        value = block.get(field)
        # `not value` would also skip a falsy-but-real value (e.g. an unquoted `0`
        # in YAML) - check emptiness explicitly instead, so anything that isn't
        # genuinely absent is treated as a potential real secret.
        if value is None or value == "":
            continue
        # Compare against *this* field's own placeholder, not any placeholder in
        # the whole set - otherwise a real secret that happens to equal a
        # *different* field's placeholder text (e.g. influx.user == "your_api_key")
        # would be wrongly treated as empty/placeholder and skip the warning.
        if value == placeholder_for(name):
            continue
        if isinstance(value, str) and value.startswith(SENTINEL_PREFIX):
            continue
        return True
    return False


def _enforce_settings_file_permissions(settings_path, raw_settings):
    """Warn (always) and optionally refuse (if enforce_permissions is true) when
    settings_path is group/other readable and actually contains a real credential.

    Takes an explicit snapshot of the raw, pre-substitution settings dict as a
    parameter rather than depending on being called before
    apply_credential_substitution() (which mutates its input in place) - this is what
    makes the function genuinely callable independently/at any time, not just
    correct-by-accident from sitting earlier in one particular call sequence.
    Checking the raw on-disk content (not whatever ends up injected in-memory from
    the properly-protected /run/credentials/... tmpfs) matters because that
    substituted value would make a file that's actually clean (sentinel only) look
    like it contains a real secret, if this were ever run against the mutated dict.

    :param settings_path: path to the settings file, used only for the log/error message
    :type settings_path: str
    :param raw_settings: settings dict as parsed from YAML, before any substitution
    :type raw_settings: dict
    :raises ConfigError: if the file is group/other readable, contains a real
        credential, and enforce_permissions is true
    """
    try:
        mode = os.stat(settings_path).st_mode
    except OSError:
        return
    if not (mode & (stat.S_IRGRP | stat.S_IROTH)):
        return
    if not _contains_real_secret(raw_settings):
        return
    # Strict `is True` rather than truthiness: enforce_permissions gates a refusal to
    # start, so a mistakenly-quoted "false" string (truthy in Python, but clearly not
    # what the user meant) must not be treated as enforcement being enabled.
    enforce = raw_settings.get("enforce_permissions", False) is True
    logging.warning(
        "%s is readable by group/other (mode %s) and contains what looks like a real credential. "
        "Run 'chmod 600 %s' to restrict access.%s",
        settings_path,
        oct(mode & 0o777),
        settings_path,
        " Refusing to start because enforce_permissions: true is set." if enforce else "",
    )
    if enforce:
        raise ConfigError(
            f"{settings_path} is group/other readable and contains a credential, and "
            f"enforce_permissions is true; refusing to start. Run: chmod 600 {settings_path}"
        )


def _clear_unsubstituted_credential_sentinels(settings):
    """Blank any credential field that still holds the literal sentinel text after
    apply_credential_substitution() ran - i.e. settings.yaml was migrated to
    systemd-creds but the matching credential file wasn't found (drop-in removed,
    service run outside systemd, etc). Left unhandled, a non-empty sentinel string
    passes validate_settings()'s existing truthiness checks, and the daemon starts
    "successfully" then fails auth forever as a retried SourceConnectionError instead
    of failing fast as the ConfigError it actually is - this reuses
    validate_settings()'s existing required-field logic for free, for every
    credential field except influx-token (raised directly instead - see below).

    :param settings: settings dict, mutated in place and returned
    :type settings: dict
    :return: the same dict
    :rtype: dict
    :raises ConfigError: if influx-token specifically is still a sentinel - see the
        note below on why this one field can't just be blanked like the others
    """
    # Slot credentials included for the same reason as the static ones: a hue.user3 left
    # holding sentinel text with no credential behind it would otherwise pass validation's
    # truthiness checks and then fail authentication forever.
    for name in [*CREDENTIAL_FIELDS, *slot_credential_names(settings)]:
        top_key, field = credential_field(name)
        block = settings.get(top_key)
        if not isinstance(block, dict):
            continue
        value = block.get(field)
        if not (isinstance(value, str) and value.startswith(SENTINEL_PREFIX)):
            continue
        if name == "influx-token":
            # Blanking this one specifically (unlike every other credential field)
            # would corrupt a *different* check downstream: validate_settings()'s
            # is_v2 = bool(influx.get("token")) would then see an empty string and
            # misclassify a broken v2 config as v1 - producing a confusing
            # "<source>.db is required when using InfluxDB v1" error (or a bucket-
            # only source rejected) instead of the real problem, for a source that
            # was never using v1 at all. Raise directly here, before that
            # misclassification can happen, with a message that actually points at
            # the credential.
            raise ConfigError(
                "influx.token was migrated to systemd-creds but could not be loaded in "
                "this execution context (drop-in removed? not running under systemd?) - "
                "run 'send-to-influx-set-credential --list' to check its status, or run "
                "this under the packaged systemd service."
            )
        block[field] = ""
    return settings


def load_settings(settings_file=None):
    """Load settings from a YAML file and return as a dictionary.

    When the resolved path does not exist and ends with ``.yaml``, the function
    falls back to the ``.yml`` equivalent for backwards compatibility.

    :param settings_file: path to the settings file (absolute, or relative to the project
        root); defaults to ``settings.yaml`` in the project root when omitted
    :type settings_file: str or None
    :return: parsed settings dictionary
    :rtype: dict
    """
    if not settings_file:
        settings_file = "settings.yaml"
    base_dir = os.path.abspath(os.path.dirname(__file__) + "/..")
    settings_path = os.path.join(base_dir, settings_file)

    if not os.path.exists(settings_path) and settings_path.endswith(".yaml"):
        fallback_path = settings_path[:-5] + ".yml"
        if os.path.exists(fallback_path):
            settings_path = fallback_path

    try:
        with open(settings_path, encoding="utf8") as f:
            settings = yaml.safe_load(f)

        if not isinstance(settings, dict) or not settings:
            logging.critical("Invalid or empty configuration in %s. Please check %s.", settings_path, settings_path)
            raise ConfigError(f"Invalid or empty configuration in {settings_path}")

        raw_settings_snapshot = copy.deepcopy(settings)
        _enforce_settings_file_permissions(settings_path, raw_settings_snapshot)
        settings = apply_credential_substitution(settings)
        settings = _clear_unsubstituted_credential_sentinels(settings)

        validate_settings(settings, settings_path=settings_path)
        return settings
    except FileNotFoundError:
        logging.critical(
            "%s not found. Make sure you copy example_settings.yaml to %s and edit it.", settings_path, settings_path
        )
        raise ConfigError(f"{settings_path} not found") from None
    except yaml.YAMLError as e:
        logging.critical("Error in %s - %s", settings_path, e)
        raise ConfigError(f"Error in {settings_path} - {e}") from e
