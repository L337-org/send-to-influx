"""Credential shape shared between the systemd-creds runtime substitution
(toinflux/general.py) and the send-to-influx-set-credential CLI
(toinflux/credential_cli.py) - kept in one place so the two can't drift apart.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"

import logging
import os

# Maps a systemd-creds credential name to the (top-level key, field) it overlays
# in the parsed settings dict. influx.user/influx.password are a paired v1 auth
# credential but are two independent dict paths, so two independent entries.
CREDENTIAL_FIELDS = {
    "influx-token": ("influx", "token"),
    "influx-user": ("influx", "user"),
    "influx-password": ("influx", "password"),
    "hue-user": ("hue", "user"),
    "myenergi-apikey": ("myenergi", "apikey"),
    "octopus-api-key": ("octopus", "api_key"),
}

# Matches example_settings.yaml's literal placeholder text for each field.
PLACEHOLDER_VALUES = {
    "influx-token": "your_influx_token",
    "influx-user": "your_influx_user",
    "influx-password": "your_influx_password",
    "hue-user": "your_hue_user",
    "myenergi-apikey": "your_api_key",
    "octopus-api-key": "your_octopus_api_key",
}

SENTINEL_PREFIX = "<stored in systemd-creds"


def sentinel_for(name):
    """Return the cosmetic placeholder written into settings.yaml once a credential
    is migrated to systemd-creds - never read back for real use (the actual value
    comes from apply_credential_substitution()), just informational for a human
    reading the file.

    :param name: systemd-creds credential name, e.g. "influx-token"
    :type name: str
    :return: sentinel string
    :rtype: str
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

    :param settings: parsed settings dictionary, mutated in place and returned
    :type settings: dict
    :return: the same dict, with any found credentials overlaid
    :rtype: dict
    """
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not creds_dir:
        return settings
    for name, (top_key, field) in CREDENTIAL_FIELDS.items():
        cred_path = os.path.join(creds_dir, name)
        if not os.path.isfile(cred_path):
            continue
        try:
            with open(cred_path, encoding="utf8") as f:
                value = f.read().strip()
        except OSError as exc:
            logging.warning("Could not read credential '%s' from %s: %s", name, cred_path, exc)
            continue
        settings.setdefault(top_key, {})[field] = value
    return settings
