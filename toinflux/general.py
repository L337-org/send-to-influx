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
import yaml
from toinflux.credentials import CREDENTIAL_FIELDS, PLACEHOLDER_VALUES, SENTINEL_PREFIX, apply_credential_substitution
from toinflux.exceptions import ConfigError

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


def mqtt_block_errors(settings, context=""):
    """
    Return a list of error strings for the shared ``mqtt`` settings block itself -
    its type, ``broker_host`` presence, and ``broker_port`` range - independent of
    which sources happen to need it.

    Shared by ``validate_settings()`` (config-check time) and
    ``MqttDataHandler.collect_mqtt_messages()` (runtime), deliberately: those are two
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
    if not mqtt.get("broker_host"):
        errors.append(f"mqtt.broker_host is required for MQTT-based sources{context}")
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
    sources = raw_sources or [settings.get("default_source")]
    # A non-string entry (e.g. a YAML mapping from a malformed sources list) would
    # raise a raw TypeError from the dict/set membership tests below - report it as
    # the ConfigError it really is, and validate the remaining string entries.
    invalid = [src for src in sources if src is not None and not isinstance(src, str)]
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
