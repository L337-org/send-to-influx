"""General functions for sending data to InfluxDB"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"
__version__ = "1.0"

# pylint: disable=import-outside-toplevel
import copy
import logging
import os
import stat
import sys
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse
import yaml
from toinflux.credentials import CREDENTIAL_FIELDS, PLACEHOLDER_VALUES, SENTINEL_PREFIX, apply_credential_substitution
from toinflux.exceptions import ConfigError

# The source sendtoinflux.py runs when neither sources: nor default_source: is
# configured. Defined here so validate_settings() checks exactly what the runtime
# will actually run - the two previously disagreed (the runtime fell back to "hue"
# while validation checked nothing), so --check-config could report OK on a config
# whose effective source had no settings block at all.
DEFAULT_SOURCE = "hue"

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 3


def configure_logging(
    logfile=None, loglevel="INFO", log_max_bytes=DEFAULT_LOG_MAX_BYTES, log_backup_count=DEFAULT_LOG_BACKUP_COUNT
):
    """Configure root logger with stdout and an optional rotating file handler.

    :param logfile: path to log file; if None, logs to stdout only
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

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler._send_to_influx_handler = True
    root.addHandler(stdout_handler)

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


def get_class(source, settings_file=None):
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
    :return: class object
    :rtype: DataHandler
    """
    from toinflux.carbonintensity import CarbonIntensity
    from toinflux.myenergi import MyEnergi, Zappi, Eddi, Harvi
    from toinflux.nuki import Nuki
    from toinflux.octopus import Octopus
    from toinflux.openmeteo import OpenMeteo
    from toinflux.philipshue import Hue
    from toinflux.speedtest import Speedtest

    classes = {
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

    class_name = next((k for k in classes if k.lower() == source.lower()), source)
    source_name = source.lower()
    try:
        my_class = classes[class_name](source_name, settings_file=settings_file)
    except KeyError:
        raise ConfigError(f"Source {class_name} not found") from None
    return my_class


# Sources that collect over MQTT and therefore need the shared top-level mqtt block.
# When adding a new MQTT-based source (a MqttDataHandler child), add its name here so
# validate_settings()/--check-config can catch a missing broker config up front rather
# than letting the collector fail at runtime.
MQTT_SOURCES = frozenset({"nuki"})


def resolve_default_source(settings):
    """
    Return the source to run when no ``sources:`` list is configured.

    Used by both ``validate_settings()`` and ``sendtoinflux.py`` so the two cannot
    disagree about what actually runs - they previously did, and the result was
    ``--check-config`` reporting OK on a config whose effective source had no
    settings block at all.

    Only an absent or blank ``default_source`` counts as unset. A non-string (YAML
    turns ``default_source: no`` into ``False``) is returned unchanged rather than
    silently replaced, so ``validate_settings()`` reports it as the malformed value
    it is instead of quietly running something the admin never asked for.

    :param settings: parsed settings dictionary
    :type settings: dict
    :return: the configured default source, or DEFAULT_SOURCE when unset
    """
    value = settings.get("default_source")
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_SOURCE
    return value


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
    ``mcp.password`` set to non-blank strings. There is no separate enabled flag;
    credentials-present is the enablement mechanism (validated as coherent by
    ``mcp_block_errors()``).

    :param settings: parsed settings dictionary (after credential substitution)
    :type settings: dict
    :rtype: bool
    """
    mcp = settings.get("mcp")
    if not isinstance(mcp, dict):
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
    if host in MCP_DISALLOWED_BIND_HOSTS:
        raise ConfigError(
            f"mcp.bind_address must not bind a public interface (got {bind_address!r}) - the MCP "
            "server speaks plain HTTP and is meant to sit behind your own TLS-terminating reverse "
            "proxy; bind a loopback or private address instead"
        )
    return host, port


def mcp_block_errors(settings):
    """Return a list of error strings for the optional ``mcp`` settings block.

    An absent block, or one with both ``user`` and ``password`` blank, is a valid
    disabled state. Set together they enable the server, which then requires a
    ``public_url`` (the external HTTPS address the reverse proxy serves - OAuth
    discovery metadata must advertise it, so there is no default to fall back to).
    One of the pair set without the other is incoherent and reported, mirroring
    the MQTT username-without-password check.

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
            "credential file leaves the password blank here."
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
    return errors


def validate_settings(settings, source=None, settings_path="settings.yaml"):
    """Validate required keys in a parsed settings dictionary.

    :param settings: parsed settings dictionary
    :type settings: dict
    :param source: an additional specific source to validate (e.g. the --source CLI
        argument), even if it isn't in the configured sources/default_source - without
        this, --check-config --source <x> could report success while <x>'s own block
        is broken, if <x> isn't part of the normal sources list
    :type source: str or None
    :param settings_path: path to the settings file, used only to label log messages -
        settings can come from a location other than settings.yaml (--settings, or the
        .yml fallback), so this shouldn't be hard-coded in the log output
    :type settings_path: str
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
        # character/key below - report it as the ConfigError it is, then fall back to
        # default_source so the rest of validation still runs sensibly.
        errors.append(f"sources must be a list (got {type(raw_sources).__name__})")
        raw_sources = None
    # An absent or empty sources list means the runtime falls back to
    # default_source, and to DEFAULT_SOURCE if that is absent too - validate exactly
    # what will actually run, or a config whose effective source has no settings
    # block would pass --check-config and then fail at startup.
    sources = raw_sources or [resolve_default_source(settings)]
    # A non-string entry (e.g. a YAML mapping, or an explicit null, from a malformed
    # sources list) would raise a raw TypeError from the dict/set membership tests
    # below - report it as the ConfigError it really is, and validate the remaining
    # string entries.
    invalid = [src for src in sources if not isinstance(src, str)]
    if invalid:
        errors.append("sources entries must be strings (got: " + ", ".join(repr(s) for s in invalid) + ")")
    sources = [src.lower() for src in sources if isinstance(src, str)]
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
    errors.extend(_validate_mqtt_block(settings, sources))
    errors.extend(mcp_block_errors(settings))
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
    for name, (top_key, field) in CREDENTIAL_FIELDS.items():
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
        if value == PLACEHOLDER_VALUES[name]:
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
    for name, (top_key, field) in CREDENTIAL_FIELDS.items():
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
