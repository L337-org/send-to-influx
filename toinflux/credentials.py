"""The credential shape shared by the runtime substitution and the credential CLI.

Used by the systemd-creds runtime substitution (toinflux/general.py) and the
send-to-influx-set-credential CLI (toinflux/credential_cli.py), kept in one place so the
two cannot drift apart.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import logging
import os
import re

# Maps a systemd-creds credential name to the (top-level key, field) it overlays
# in the parsed settings dict. influx.user/influx.password are a paired v1 auth
# credential but are two independent dict paths, so two independent entries.
CREDENTIAL_FIELDS = {
    "influx-token": ("influx", "token"),
    "influx-user": ("influx", "user"),
    "influx-password": ("influx", "password"),
    "hue-user": ("hue", "user"),
    "mqtt-password": ("mqtt", "password"),
    "mcp-password": ("mcp", "password"),
    "myenergi-apikey": ("myenergi", "apikey"),
    "octopus-api-key": ("octopus", "api_key"),
}

# Matches example_settings.yaml's literal placeholder text for each field.
PLACEHOLDER_VALUES = {
    "influx-token": "your_influx_token",
    "influx-user": "your_influx_user",
    "influx-password": "your_influx_password",
    "hue-user": "your_hue_user",
    "mqtt-password": "your_mqtt_password",
    # Deliberately the empty string, unlike every other entry: example_settings.yaml
    # ships mcp.password as "" because empty-means-disabled is the mcp block's whole
    # enablement mechanism. This works with the existing machinery because
    # _validate_secret_value() rejects empty input on its own, before the placeholder
    # comparison, and --remove writing "" back is exactly the disabled state.
    "mcp-password": "",
    "myenergi-apikey": "your_api_key",
    "octopus-api-key": "your_octopus_api_key",
}

SENTINEL_PREFIX = "<stored in systemd-creds"

# A canonical slot suffix: 2 upwards, no leading zeros. Slot 1 is always the *unnumbered*
# field, so "1" is rejected as a second way to spell it, and "02" as a second way to spell 2.
# Defined here rather than in philipshue.py because both the settings side (which slot fields
# exist) and the credential side (which credential names exist) must agree on it, and this
# module sits below both - a second copy would drift.
#
# Note the alternation: a bare [2-9]\d* looks right but rejects 10-19 (and 100-199, ...)
# because they start with a 1.
CANONICAL_SLOT_SUFFIX_RE = re.compile(r"^([2-9]|[1-9]\d+)$")

# Credentials for a source whose targets are numbered slots: hue-user2, hue-user3, ...
# Slot 1 is the unnumbered hue-user, already a static CREDENTIAL_FIELDS entry above. The
# section and field stem are fixed here because Hue is the only such source; a second one
# would become a table rather than a pair of constants.
_SLOT_SECTION = "hue"
_SLOT_FIELD_STEM = "user"
_SLOT_CREDENTIAL_PREFIX = f"{_SLOT_SECTION}-{_SLOT_FIELD_STEM}"


def _slot_suffix(text, prefix):
    """Return the canonical slot suffix of ``text`` after ``prefix``, or None.

    None covers both "does not start with the prefix" and "the suffix is not canonical", so
    a caller cannot accidentally treat ``hue-user1`` or ``hue-user02`` as a valid slot.
    """
    if not isinstance(text, str) or not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :]
    return suffix if CANONICAL_SLOT_SUFFIX_RE.match(suffix) else None


def credential_field(name):
    """Return the ``(section, field)`` a credential name overlays.

    None if it is not a credential name at all.

    Covers both the static table and the numbered slots, so every caller asks one question
    instead of checking a dict and then remembering the slot rule separately. That
    separation is what let ``--set-field hue.user2`` write a real token into settings.yaml in
    plaintext while ``_contains_real_secret`` stayed blind to it.

    Args:
        name: credential name, e.g. "influx-token", "hue-user", "hue-user3"

    Returns:
        tuple or None: (section, field) or None
    """
    if name in CREDENTIAL_FIELDS:
        return CREDENTIAL_FIELDS[name]
    suffix = _slot_suffix(name, _SLOT_CREDENTIAL_PREFIX)
    return (_SLOT_SECTION, f"{_SLOT_FIELD_STEM}{suffix}") if suffix else None


def is_credential_name(name):
    """Whether ``name`` is a credential this tool manages (static or a numbered slot)."""
    return credential_field(name) is not None


def credential_name_for(section, field):
    """Return the credential name that owns a settings path, or None if it holds no secret.

    The inverse of :func:`credential_field`. Used to refuse a plaintext write to a secret
    field and to name the right command in the refusal.

    Args:
        section: top-level settings key, e.g. "hue"
        field: field within it, e.g. "user2"

    Returns:
        str or None: credential name, or None
    """
    for name, path in CREDENTIAL_FIELDS.items():
        if (section, field) == path:
            return name
    if section == _SLOT_SECTION:
        suffix = _slot_suffix(field, _SLOT_FIELD_STEM)
        if suffix:
            return f"{_SLOT_CREDENTIAL_PREFIX}{suffix}"
    return None


def is_credential_field(section, field):
    """Whether a settings path holds a secret - see :func:`credential_name_for`."""
    return credential_name_for(section, field) is not None


def placeholder_for(name):
    """Return the example-file placeholder for a credential, or None if unknown.

    A slot shares slot 1's placeholder: every Hue username field carries the same example
    text. Returns None rather than raising for an unknown name - ``--remove`` indexed
    ``PLACEHOLDER_VALUES`` directly and would have died with a KeyError on a slot.

    Args:
        name: credential name

    Returns:
        str or None: placeholder text, or None
    """
    if name in PLACEHOLDER_VALUES:
        return PLACEHOLDER_VALUES[name]
    if _slot_suffix(name, _SLOT_CREDENTIAL_PREFIX):
        return PLACEHOLDER_VALUES[_SLOT_CREDENTIAL_PREFIX]
    return None


def slot_credential_names(settings):
    """Return the slot credential names a parsed settings dict implies, in slot order.

    Discovered from the settings rather than a fixed table, which is what removes the cap:
    any ``hue.userN`` present is a credential this tool manages. Slot 1's ``hue-user`` is
    excluded - it is already a static entry.

    Args:
        settings (dict): parsed settings dictionary

    Returns:
        list: credential names like ["hue-user2", "hue-user3"]
    """
    section = settings.get(_SLOT_SECTION) if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return []
    found = []
    for field in section:
        suffix = _slot_suffix(str(field), _SLOT_FIELD_STEM)
        if suffix:
            found.append((int(suffix), f"{_SLOT_CREDENTIAL_PREFIX}{suffix}"))
    return [name for _, name in sorted(found)]


def sentinel_for(name):
    """Return the placeholder written into settings.yaml once a credential is migrated.

    Cosmetic only, and never read back for real use - the actual value comes from
    apply_credential_substitution(). It is there to inform a human reading the file.

    Args:
        name (str): systemd-creds credential name, e.g. "influx-token"

    Returns:
        str: sentinel string
    """
    return f"{SENTINEL_PREFIX} - run 'send-to-influx-set-credential {name}' to modify>"


def apply_credential_substitution(settings):
    """Overlay systemd-creds-provided values into a parsed settings dict.

    No-op (returns settings unchanged) when CREDENTIALS_DIRECTORY is unset - this is
    what keeps the source-checkout path and any not-yet-migrated packaged install
    byte-for-byte identical to reading settings.yaml directly. systemd sets
    CREDENTIALS_DIRECTORY only when the unit uses LoadCredential=/SetCredential=/
    LoadCredentialEncrypted=, pointing at a tmpfs populated fresh on every service
    start - so this has to run on every load_settings() call, not just once, since
    that's the only place the decrypted value is ever available.

    Driven by what is actually in that directory rather than by iterating a fixed table:
    slot credentials (hue-user2, hue-user3, ...) are unbounded, so there is no table to
    iterate. Anything present that is not a credential name this tool manages is ignored -
    LoadCredentialEncrypted= is not exclusive to us.

    Args:
        settings (dict): parsed settings dictionary, mutated in place and returned

    Returns:
        dict: the same dict, with any found credentials overlaid
    """
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not creds_dir:
        return settings
    try:
        present = sorted(os.listdir(creds_dir))
    except OSError as exc:
        logging.warning("Could not list credentials directory %s: %s", creds_dir, exc)
        return settings
    for name in present:
        path = credential_field(name)
        if path is None:
            continue
        value = _read_credential(os.path.join(creds_dir, name), name)
        if value is not None:
            _overlay_credential(settings, name, path, value)
    return settings


def _read_credential(cred_path, name):
    """Read one decrypted credential, or None if it is absent or unreadable.

    Never raises: a single bad credential must not bring down load_settings() for every
    source, which is apply_credential_substitution()'s contract.

    UnicodeDecodeError is caught alongside OSError because systemd credentials are arbitrary
    bytes with no guarantee of being valid UTF-8 - this project's own CLI always writes
    UTF-8, but LoadCredentialEncrypted= is not exclusive to it.

    Args:
        cred_path: path to the decrypted credential file
        name: credential name, for the log message

    Returns:
        str or None: the value, or None
    """
    if not os.path.isfile(cred_path):
        return None
    try:
        with open(cred_path, encoding="utf8") as handle:
            # Only strip a trailing line ending, not all whitespace: a password can
            # legitimately start or end with spaces, and _encrypt_credential() never appends
            # one - but strip defensively in case anything else in the pipeline did.
            return handle.read().rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        logging.warning("Could not read credential '%s' from %s: %s", name, cred_path, exc)
        return None


def _overlay_credential(settings, name, path, value):
    """Write one credential value into its settings section, creating the section if absent.

    A malformed section (``influx: []``, ``hue: "oops"``) is logged and skipped rather than
    crashing - this function's caller must survive a bad settings file. ``validate_settings()``
    then reports it as a ``ConfigError`` naming the section and its type, so a bad section is
    skipped here and explained there rather than reaching a collector half-applied.

    Args:
        settings: parsed settings dict, mutated in place
        name: credential name, for the log message
        path: the ``(section, field)`` this credential overlays
        value: the decrypted value
    """
    top_key, field = path
    block = settings.get(top_key)
    if block is None:
        block = settings[top_key] = {}
    elif not isinstance(block, dict):
        logging.warning(
            "settings.yaml's '%s' section is not a mapping (got %s) - cannot apply the "
            "'%s' credential from systemd-creds",
            top_key,
            type(block).__name__,
            name,
        )
        return
    block[field] = value
