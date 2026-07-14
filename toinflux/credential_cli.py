"""send-to-influx-set-credential: manage secrets in systemd-creds for the packaged
.deb/systemd install, and make small direct edits to settings.yaml alongside them.

Only meaningful on a systemd host with systemd-creds (systemd >= 250) - not a
requirement of the base package, since that would make the whole package
uninstallable on currently-supported platforms whose systemd is just under that
(e.g. Ubuntu 22.04/jammy ships 249). Checked at runtime instead; see
_require_systemd_creds().
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT License"

import argparse
import getpass
import logging
import os
import re
import subprocess
import sys
import tempfile
import stat as stat_module

import requests
import yaml

from toinflux.credentials import CREDENTIAL_FIELDS, PLACEHOLDER_VALUES, SENTINEL_PREFIX, sentinel_for

DEFAULT_SETTINGS_PATH = "/etc/send-to-influx/settings.yaml"
CREDSTORE_DIR = "/etc/send-to-influx/credstore.encrypted"
DROPIN_DIR = "/etc/systemd/system/send-to-influx.service.d"
DROPIN_PATH = os.path.join(DROPIN_DIR, "50-credentials.conf")
MIN_SYSTEMD_CREDS_VERSION = 250
HTTP_TIMEOUT_SECONDS = 5


class CredentialCliError(Exception):
    """A user-facing error - message is printed to stderr, process exits 1."""


# --------------------------------------------------------------------------- #
# systemd-creds runtime capability check
# --------------------------------------------------------------------------- #


def _parse_systemd_creds_version(version_output):
    """Parse the leading version number out of `systemd-creds --version` output
    (e.g. "systemd 255 (255.4-1ubuntu8.4)\\n+PAM +AUDIT ...").

    :param version_output: raw stdout from `systemd-creds --version`
    :type version_output: str
    :return: the version number, or None if it couldn't be parsed
    :rtype: int or None
    """
    match = re.search(r"systemd\s+(\d+)", version_output)
    return int(match.group(1)) if match else None


def _require_systemd_creds():
    """Confirm systemd-creds exists and is new enough. Raises CredentialCliError with
    a specific, actionable message otherwise.

    :raises CredentialCliError: if systemd-creds is missing or older than
        MIN_SYSTEMD_CREDS_VERSION
    """
    try:
        result = subprocess.run(["systemd-creds", "--version"], capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise CredentialCliError(
            "systemd-creds not found. It requires systemd >= "
            f"{MIN_SYSTEMD_CREDS_VERSION}; credential storage isn't available on this "
            "host - edit settings.yaml directly instead."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CredentialCliError(f"'systemd-creds --version' failed: {exc}") from exc

    version = _parse_systemd_creds_version(result.stdout)
    if version is None or version < MIN_SYSTEMD_CREDS_VERSION:
        found = version if version is not None else "an unrecognised version"
        raise CredentialCliError(
            f"systemd-creds requires systemd >= {MIN_SYSTEMD_CREDS_VERSION}; this host has "
            f"{found} - credential storage isn't available here, edit settings.yaml "
            "directly instead."
        )


# --------------------------------------------------------------------------- #
# Secret input / validation
# --------------------------------------------------------------------------- #


def _read_secret_value(name):
    """Read a secret from stdin if piped, else prompt interactively (masked).

    Passwords/tokens can legitimately contain leading/trailing whitespace, so
    nothing beyond a trailing line ending is trimmed - getpass.getpass() already
    excludes the terminal's own trailing newline, and piped input only has the
    one trailing newline typically appended by e.g. `echo "secret" | ...`
    stripped, not any whitespace that's actually part of the value.
    _validate_secret_value() separately rejects any *embedded* newline.

    :param name: credential name, used only in the interactive prompt
    :type name: str
    :rtype: str
    """
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {name}: ")
    return sys.stdin.read().rstrip("\r\n")


def _validate_secret_value(name, value):
    """Reject empty/placeholder/multiline input before anything is touched on disk.

    :raises CredentialCliError: if the value looks invalid
    """
    if not value.strip():
        raise CredentialCliError("Value must not be empty.")
    if value == PLACEHOLDER_VALUES.get(name):
        raise CredentialCliError(
            "That's still the placeholder value from example_settings.yaml - enter the real secret."
        )
    if "\n" in value:
        raise CredentialCliError("Value must not contain embedded newlines.")


def _validate_storage_name(name):
    """Reject anything that isn't a safe, simple database/bucket name.

    _ensure_influx_storage() interpolates name directly into an InfluxQL
    `CREATE DATABASE "{name}"` query (v1) and into a JSON field (v2) - a name
    containing quotes or control characters could break the query or change what
    actually gets executed. postinst's own hardcoded names (hue_db, zappi_db, ...)
    all satisfy this; this only matters for --ensure-influx-storage's admin-supplied
    argument.

    :raises CredentialCliError: if name isn't letters/digits/underscore/hyphen
    """
    if not re.match(r"^[A-Za-z0-9_-]+$", name):
        raise CredentialCliError(
            f"'{name}' is not a valid database/bucket name - use only letters, digits, underscores, and hyphens."
        )


# --------------------------------------------------------------------------- #
# credstore.encrypted / drop-in management
# --------------------------------------------------------------------------- #


# NOTE: the credstore_dir/dropin_path parameters below default to None and resolve
# to the module-level constant *inside* the function body, rather than
# `def f(x=CREDSTORE_DIR)` - Python binds a default argument's value once, at def
# time, so `def f(x=CREDSTORE_DIR)` would freeze in the value CREDSTORE_DIR had at
# import time and silently ignore any later `monkeypatch.setattr(module,
# "CREDSTORE_DIR", ...)` in tests (or any other reassignment of the module global).
# Resolving inside the body reads the name from the module's global namespace fresh
# on every call, so patching the module attribute actually takes effect.


def _cred_path(name, credstore_dir=None):
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    return os.path.join(credstore_dir, f"{name}.cred")


def _regenerate_dropin(credstore_dir=None, dropin_path=None, exclude=None):
    """Rewrite the systemd drop-in from a fresh directory listing of credstore_dir -
    idempotent and self-healing if a prior run was interrupted, no separate state
    file needed.

    :param exclude: a credential name to treat as absent even if its .cred file still
        exists on disk - used by _cmd_remove so the drop-in never references a file
        that's about to be deleted, even transiently (LoadCredentialEncrypted=
        referencing a missing path hard-fails unit startup with 243/CREDENTIALS)
    :type exclude: str or None
    """
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    if dropin_path is None:
        dropin_path = DROPIN_PATH

    lines = ["[Service]"]
    for name in sorted(CREDENTIAL_FIELDS):
        if name == exclude:
            continue
        cred_path = _cred_path(name, credstore_dir)
        if os.path.isfile(cred_path):
            lines.append(f"LoadCredentialEncrypted={name}:{cred_path}")

    try:
        if len(lines) == 1:
            if os.path.exists(dropin_path):
                os.remove(dropin_path)
            return
        os.makedirs(os.path.dirname(dropin_path), exist_ok=True)
        _atomic_write(dropin_path, "\n".join(lines) + "\n")
    except OSError as exc:
        raise CredentialCliError(f"could not update {dropin_path}: {exc}") from exc


def _reload_systemd():
    if os.path.isdir("/run/systemd/system"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)


def _encrypt_credential(name, value, credstore_dir=None):
    """Encrypt value with systemd-creds and write it to credstore_dir/<name>.cred.

    :raises CredentialCliError: if credstore_dir can't be created/secured, if
        systemd-creds encrypt fails, or if the written .cred file can't be secured
    """
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    # postinst normally pre-creates credstore_dir at 0700, but this must hold even if
    # it's ever missing when the CLI runs standalone - os.makedirs() alone would create
    # it at the process umask's default (commonly 0755), making credential *names*
    # (not contents, which get their own 0600 below) enumerable by other local users.
    # Always re-asserting 0700 here (not just on first creation) is a harmless no-op
    # against postinst's own already-correct directory, and self-healing otherwise.
    try:
        os.makedirs(credstore_dir, exist_ok=True)
        os.chmod(credstore_dir, stat_module.S_IRWXU)
    except OSError as exc:
        raise CredentialCliError(f"could not create/secure {credstore_dir}: {exc}") from exc
    cred_path = _cred_path(name, credstore_dir)
    try:
        subprocess.run(
            ["systemd-creds", "encrypt", f"--name={name}", "-", cred_path],
            input=value.encode(),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
        raise CredentialCliError(f"systemd-creds encrypt failed for '{name}': {stderr}") from exc
    try:
        os.chmod(cred_path, stat_module.S_IRUSR | stat_module.S_IWUSR)
    except OSError as exc:
        raise CredentialCliError(f"could not secure {cred_path}: {exc}") from exc


def _decrypt_credential(name, credstore_dir=None):
    """Decrypt credstore_dir/<name>.cred back to plaintext, held only in memory.

    Works standalone, outside of any running systemd service: this always runs as
    root on the same host that holds the same TPM/host key systemd-creds encrypt
    used, so it can always decrypt what it just encrypted.

    :raises CredentialCliError: if the credential doesn't exist or decryption fails
    """
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    cred_path = _cred_path(name, credstore_dir)
    if not os.path.isfile(cred_path):
        raise CredentialCliError(f"No stored credential for '{name}' at {cred_path}.")
    try:
        result = subprocess.run(["systemd-creds", "decrypt", cred_path, "-"], check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
        raise CredentialCliError(f"systemd-creds decrypt failed for '{name}': {stderr}") from exc
    # Only strip a trailing line ending, not all whitespace - a password can
    # legitimately start/end with spaces, and _encrypt_credential() never appends
    # one, but strip defensively in case anything else in the pipeline did.
    return result.stdout.decode().rstrip("\r\n")


# --------------------------------------------------------------------------- #
# settings.yaml surgical edit
# --------------------------------------------------------------------------- #


def _atomic_write(path, content):
    """Write content to path atomically (temp file + os.replace), preserving the
    original file's owner/mode if it already exists - a naive rewrite would
    otherwise land owned by whoever ran this script instead of
    send-to-influx:send-to-influx 0600/0644.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(content)
        try:
            st = os.stat(path)
            os.chown(tmp_path, st.st_uid, st.st_gid)
            os.chmod(tmp_path, stat_module.S_IMODE(st.st_mode))
        except OSError:
            pass
        os.replace(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def _find_mapping_value(node, key):
    """Walk one level of a yaml.compose() MappingNode looking for a scalar key."""
    if node is None or not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if key_node.value == key:
            return value_node
    return None


def _yaml_double_quoted_escape(value):
    """Escape value for safe embedding inside a YAML double-quoted scalar.

    Order matters: backslashes must be doubled first, so the backslashes this
    function itself introduces for the quote/CR/LF escapes below aren't
    re-escaped by a later step. YAML double-quoted scalars support \\r/\\n as
    genuine escape sequences (unlike single-quoted or plain scalars), so a
    literal newline/carriage return in value becomes an escaped, single-line
    representation rather than splitting the quoted scalar across multiple
    lines - which would otherwise write invalid YAML.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def _rewrite_settings_field(settings_path, top_key, field, new_value):
    """Replace a single scalar field's value in place, preserving every other byte of
    the file (comments, ordering, blank lines) by locating the exact source line via
    yaml.compose() rather than a full load+dump round trip, which would silently
    strip every comment - example_settings.yaml is comment-dense and users are
    expected to keep reading/editing it.

    :raises CredentialCliError: if the target section/field doesn't exist, or isn't a
        plain single-line scalar (e.g. hand-edited into a block scalar) - refuses
        rather than corrupting the file; also raised (rather than an unhandled
        OSError escaping main()'s exception handling) if settings_path can't be read
        or written, e.g. missing file or a permissions problem
    """
    try:
        with open(settings_path, encoding="utf8") as f:
            text = f.read()
    except OSError as exc:
        raise CredentialCliError(f"could not read {settings_path}: {exc}") from exc

    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise CredentialCliError(f"{settings_path}: could not parse YAML: {exc}") from exc

    top_node = _find_mapping_value(root, top_key)
    if top_node is None:
        raise CredentialCliError(f"{settings_path}: no '{top_key}:' section found - add it manually first")
    value_node = _find_mapping_value(top_node, field)
    if value_node is None or value_node.start_mark.line != value_node.end_mark.line:
        raise CredentialCliError(
            f"{settings_path}: could not safely rewrite {top_key}.{field} automatically "
            "(missing, or not a plain single-line value) - edit it by hand instead"
        )

    lines = text.splitlines(keepends=True)
    line_no = value_node.start_mark.line
    line = lines[line_no]
    indent = line[: len(line) - len(line.lstrip())]
    # The splice below assumes the line reads `<indent>field: <value>...` - true for a
    # normal block-style mapping (whitespace before the colon, e.g. `field : value`, is
    # unusual but still valid YAML and still safe here), but not for e.g.
    # `influx: {token: "old", org: "x"}` (a flow-style section), where value_node's own
    # line doesn't start with the field name at all. Verify that assumption before
    # writing rather than after - a flow-style section would otherwise have its
    # `top_key: {` prefix silently overwritten by the naive `indent + field + ": " +
    # value` reconstruction below, producing invalid YAML.
    if not re.match(rf"^{re.escape(field)}\s*:", line[len(indent) :]):
        raise CredentialCliError(
            f"{settings_path}: could not safely rewrite {top_key}.{field} automatically "
            "(unexpected line format, e.g. a flow-style mapping) - edit it by hand instead"
        )
    # Preserve everything around the value verbatim: the prefix (indent, field name,
    # colon, and whatever whitespace separated them in the original - e.g. `field : `
    # is unusual but valid, and reconstructing a hardcoded `field: ` would needlessly
    # reformat it) up to where the old value started, and whatever followed the old
    # value - typically nothing, but could be a trailing inline comment (e.g.
    # `token: "old"  # note`). Only the value itself is replaced, always as a
    # double-quoted scalar regardless of the original's quoting style.
    prefix = line[: value_node.start_mark.column]
    trailing = line[value_node.end_mark.column :].rstrip("\n")
    escaped = _yaml_double_quoted_escape(new_value)
    lines[line_no] = f'{prefix}"{escaped}"{trailing}\n'

    try:
        _atomic_write(settings_path, "".join(lines))
    except OSError as exc:
        raise CredentialCliError(f"could not write {settings_path}: {exc}") from exc


def _load_sources_sequence(settings_path):
    """Read and parse settings_path, returning (text, sources_node) for its
    top-level `sources:` sequence.

    :raises CredentialCliError: if the file can't be read, isn't valid YAML, or
        `sources:` isn't a plain (non-empty) sequence
    """
    try:
        with open(settings_path, encoding="utf8") as f:
            text = f.read()
    except OSError as exc:
        raise CredentialCliError(f"could not read {settings_path}: {exc}") from exc

    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise CredentialCliError(f"{settings_path}: could not parse YAML: {exc}") from exc

    sources_node = _find_mapping_value(root, "sources")
    if sources_node is None or not isinstance(sources_node, yaml.SequenceNode):
        raise CredentialCliError(f"{settings_path}: no 'sources:' sequence found - add it manually first")
    if sources_node.flow_style:
        # e.g. `sources: ["hue", "zappi"]` on one line - inserting a new block-style
        # `  - "name"` line after it (this function's only insertion strategy) would
        # leave a dangling sequence item with no key of its own, invalid YAML. The
        # shipped example_settings.yaml always uses block style; asking the user to
        # add flow-style entries by hand is a fine trade-off for how rare this is.
        raise CredentialCliError(
            f"{settings_path}: 'sources:' uses flow style (e.g. [a, b]) - add the new source manually"
        )
    if not sources_node.value:
        # A block-style `sources:` with nothing under it parses as `sources: null`
        # (a scalar), not an empty sequence - so this only happens for something
        # like the (unusual) explicit flow-style `sources: []`, and there's no safe
        # way to turn that into a populated block sequence by just inserting a line
        # after it without risking invalid YAML. Rare enough in practice (the
        # shipped example_settings.yaml always ships several sources uncommented)
        # that asking the user to add the first entry by hand is a fine trade-off.
        raise CredentialCliError(f"{settings_path}: 'sources:' is empty - add at least one source manually first")

    return text, sources_node


def _enable_source(name, settings_path=None):
    """Idempotently append `name` to settings.yaml's top-level `sources:` sequence,
    preserving the rest of the file untouched - a no-op if already present, so a
    later dpkg-reconfigure re-running this doesn't duplicate entries.

    Used instead of _rewrite_settings_field(), which only handles a single-line
    scalar value - `sources:` is a YAML sequence, a structurally different edit.

    :return: True if the file was actually changed, False if `name` was already
        present (so callers - e.g. the CLI - can report an accurate message
        instead of always claiming "enabled")
    :rtype: bool
    :raises CredentialCliError: see _load_sources_sequence(), plus if settings_path
        can't be written back
    """
    if settings_path is None:
        settings_path = DEFAULT_SETTINGS_PATH
    text, sources_node = _load_sources_sequence(settings_path)

    existing = [item.value for item in sources_node.value if isinstance(item, yaml.ScalarNode)]
    if name in existing:
        return False

    lines = text.splitlines(keepends=True)
    last_item = sources_node.value[-1]
    item_line = lines[last_item.start_mark.line]
    indent = item_line[: len(item_line) - len(item_line.lstrip())]
    insert_at = last_item.end_mark.line + 1

    lines.insert(insert_at, f'{indent}- "{_yaml_double_quoted_escape(name)}"\n')

    try:
        _atomic_write(settings_path, "".join(lines))
    except OSError as exc:
        raise CredentialCliError(f"could not write {settings_path}: {exc}") from exc
    return True


# --------------------------------------------------------------------------- #
# InfluxDB version detection / storage creation (used by Part 2's debconf postinst)
# --------------------------------------------------------------------------- #


def _detect_influx_version(url):
    """Probe url to determine whether it's an InfluxDB v1 or v2 instance, without
    needing any credential - both /health (v2) and /ping (v1, and v2 for backward
    compat) are unauthenticated health-check endpoints on real InfluxDB servers.

    :return: "v1", "v2", or "unknown" (unreachable/ambiguous - never raises)
    :rtype: str
    """
    try:
        resp = requests.get(f"{url.rstrip('/')}/health", timeout=HTTP_TIMEOUT_SECONDS)
        if resp.status_code == 200:
            data = resp.json()
            if str(data.get("version", "")).startswith("2."):
                return "v2"
    except (requests.RequestException, ValueError):
        pass

    try:
        resp = requests.get(f"{url.rstrip('/')}/ping", timeout=HTTP_TIMEOUT_SECONDS)
        version = resp.headers.get("X-Influxdb-Version", "")
        if version.startswith("1."):
            return "v1"
        if version.startswith("2."):
            return "v2"
    except requests.RequestException:
        pass

    return "unknown"


def _resolve_credential_value(name, influx, credstore_dir):
    """Return the real value for one of the influx.* credential fields, whether or
    not it's been migrated to systemd-creds - both are legitimate, since migration
    is opt-in and per-field (see toinflux.credentials). If the plain settings.yaml
    value is the systemd-creds sentinel, decrypt the real value instead; otherwise
    the plain value already *is* the real value (never migrated).

    :param influx: the parsed `influx:` settings block
    :type influx: dict
    """
    _, field = CREDENTIAL_FIELDS[name]
    plain_value = influx.get(field, "")
    if isinstance(plain_value, str) and plain_value.startswith(SENTINEL_PREFIX):
        return _decrypt_credential(name, credstore_dir)
    return plain_value


def _ensure_influx_storage(name, settings_path=None, credstore_dir=None):
    """Best-effort create the InfluxDB database (v1) or bucket (v2) named `name`.
    Never raises on failure (permissions/auth/unreachable) - logs and returns, since
    install/auto-enable must not be blocked by this.

    Authenticates by reading url/org straight from settings.yaml (never secrets) and
    resolving user/password/token via _resolve_credential_value() - each is read
    plain if never migrated to systemd-creds, or decrypted if it has been (opt-in,
    per-field, so a real install could have any mix of the two). Any decrypted value
    is held only in memory for this one call and never written back to disk.
    """
    if settings_path is None:
        settings_path = DEFAULT_SETTINGS_PATH
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR

    # Everything below is best-effort by contract (see docstring) - install/auto-enable
    # must not be blocked by this failing, so catch broadly rather than enumerating
    # every specific exception type a missing/unreadable/malformed settings.yaml,
    # a network call, or a decrypt could raise (OSError, yaml.YAMLError, AttributeError
    # on a non-mapping parse result, requests.RequestException, CredentialCliError, ...).
    try:
        with open(settings_path, encoding="utf8") as f:
            settings = yaml.safe_load(f)
        influx = (settings or {}).get("influx") or {}
        url = influx.get("url", "").rstrip("/")
        if not url:
            logging.warning("send-to-influx-set-credential: no influx.url configured, skipping storage creation")
            return

        # A token configures v2 whether it's plain or already migrated to
        # systemd-creds (a migrated field's plain settings.yaml value is the
        # sentinel text, still non-empty/truthy) - matches
        # toinflux.general._validate_influx_block's own `is_v2 = bool(token)` check.
        # Checking for a `.cred` file's existence instead (as an earlier version of
        # this function did) gets this wrong for a token that's never been migrated.
        is_v2 = bool(influx.get("token"))
        if is_v2:
            token = _resolve_credential_value("influx-token", influx, credstore_dir)
            org = influx.get("org", "")
            headers = {"Authorization": f"Token {token}"}
            resp = requests.get(
                f"{url}/api/v2/buckets", params={"org": org}, headers=headers, timeout=HTTP_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            existing = {b.get("name") for b in resp.json().get("buckets", [])}
            if name in existing:
                logging.info("InfluxDB bucket '%s' already exists", name)
                return
            resp = requests.post(
                f"{url}/api/v2/buckets",
                headers=headers,
                json={"name": name, "orgID": _resolve_org_id(url, headers, org)},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            logging.info("Created InfluxDB v2 bucket '%s'", name)
        else:
            user = _resolve_credential_value("influx-user", influx, credstore_dir)
            password = _resolve_credential_value("influx-password", influx, credstore_dir)
            resp = requests.post(
                f"{url}/query",
                params={"q": f'CREATE DATABASE "{name}"'},
                auth=(user, password),
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            logging.info("Ensured InfluxDB v1 database '%s' exists", name)
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning(
            "Could not create InfluxDB storage '%s' automatically (%s) - create it yourself if needed.",
            name,
            exc,
        )


def _resolve_org_id(url, headers, org_name):
    """Look up the org ID for org_name - the v2 bucket-create API needs orgID, not
    just the org name."""
    resp = requests.get(f"{url}/api/v2/orgs", params={"org": org_name}, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    orgs = resp.json().get("orgs", [])
    if not orgs:
        raise CredentialCliError(f"could not resolve org id for org '{org_name}'")
    return orgs[0]["id"]


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def _cmd_set(name, settings_path):
    _require_systemd_creds()
    value = _read_secret_value(name)
    _validate_secret_value(name, value)
    _encrypt_credential(name, value)
    _regenerate_dropin()
    _reload_systemd()
    top_key, field = CREDENTIAL_FIELDS[name]
    try:
        _rewrite_settings_field(settings_path, top_key, field, sentinel_for(name))
    except CredentialCliError as exc:
        # The secret is already safely encrypted in systemd-creds at this point -
        # don't roll that back (discarding a successful encryption to "fix" a
        # settings.yaml formatting problem would be worse, not better). But the
        # plaintext copy in settings.yaml is still sitting there unremoved, and the
        # generic "edit it by hand instead" message from _rewrite_settings_field
        # alone wouldn't tell the user that - make it explicit here instead.
        raise CredentialCliError(
            f"'{name}' was encrypted and stored in systemd-creds, but {settings_path} "
            f"could not be updated to match ({exc}) - the plaintext value is still "
            f"there and should be removed by hand."
        ) from exc
    print(f"Stored '{name}' in systemd-creds and updated {settings_path}.")


def _cmd_remove(name, settings_path):
    # Order matters, in two different ways:
    #
    # 1. settings.yaml is rewritten *first*, before anything else is touched. If
    #    that fails (e.g. a hand-edited flow-style section _rewrite_settings_field
    #    refuses to touch), nothing else has happened yet - the credential is still
    #    fully intact and the service is unaffected, rather than ending up with the
    #    drop-in/`.cred` file already gone but settings.yaml still holding the old
    #    systemd-creds sentinel. That sentinel isn't valid placeholder text, so a
    #    later load_settings() would blank it via
    #    _clear_unsubstituted_credential_sentinels() and fail validate_settings()
    #    with a ConfigError - a broken, unrecoverable service (the actual secret
    #    is gone from systemd-creds too) for a failure that should have been a
    #    clean no-op.
    # 2. Once settings.yaml is safely reverted, regenerate the drop-in (dropping
    #    this credential's line) before deleting the .cred file, never after -
    #    LoadCredentialEncrypted= referencing a missing path hard-fails unit
    #    startup, so the drop-in must never be left pointing at a file that's
    #    already gone, even transiently if this is interrupted mid-way.
    top_key, field = CREDENTIAL_FIELDS[name]
    _rewrite_settings_field(settings_path, top_key, field, PLACEHOLDER_VALUES[name])
    _regenerate_dropin(exclude=name)
    _reload_systemd()
    cred_path = _cred_path(name)
    was_stored = os.path.isfile(cred_path)
    if was_stored:
        try:
            os.remove(cred_path)
        except OSError as exc:
            raise CredentialCliError(f"could not remove {cred_path}: {exc}") from exc
        print(f"Removed '{name}' from systemd-creds and reverted {settings_path} to the placeholder value.")
    else:
        print(f"'{name}' was not stored in systemd-creds - reverted {settings_path} to the placeholder value.")


def _cmd_list(credstore_dir=None):
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    for name in sorted(CREDENTIAL_FIELDS):
        status = "configured" if os.path.isfile(_cred_path(name, credstore_dir)) else "not set"
        print(f"{name}: {status}")


def _cmd_set_field(dotted_path, value, settings_path):
    top_key, _, field = dotted_path.partition(".")
    if not field:
        raise CredentialCliError(f"'{dotted_path}' must be in the form <section>.<field>, e.g. hue.host")
    _rewrite_settings_field(settings_path, top_key, field, value)
    print(f"Updated {top_key}.{field} in {settings_path}.")


def _cmd_detect_influx_version(url):
    print(_detect_influx_version(url))


def _cmd_ensure_influx_storage(name, settings_path):
    # Validated here, before the best-effort/never-raises _ensure_influx_storage(),
    # so a bad name from --ensure-influx-storage's admin-supplied argument gets an
    # immediate, actionable CredentialCliError instead of a swallowed warning log
    # line - postinst's own calls always pass a hardcoded, already-valid name.
    _validate_storage_name(name)
    _ensure_influx_storage(name, settings_path=settings_path)


def _cmd_enable_source(name, settings_path):
    if _enable_source(name, settings_path=settings_path):
        print(f"Enabled '{name}' in {settings_path}.")
    else:
        print(f"'{name}' was already enabled in {settings_path} - nothing to do.")


# --------------------------------------------------------------------------- #
# argparse entry point
# --------------------------------------------------------------------------- #


def _build_parser():
    parser = argparse.ArgumentParser(prog="send-to-influx-set-credential")
    parser.add_argument(
        "--settings", default=DEFAULT_SETTINGS_PATH, help="settings.yaml to update (default: %(default)s)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("name", nargs="?", choices=sorted(CREDENTIAL_FIELDS), help="credential name to set/remove")
    group.add_argument("--list", action="store_true", help="list which credentials are configured")
    group.add_argument("--set-field", nargs=2, metavar=("PATH", "VALUE"), help="write a plain, non-secret YAML field")
    group.add_argument("--detect-influx-version", metavar="URL", help="probe URL and print v1/v2/unknown")
    group.add_argument("--ensure-influx-storage", metavar="NAME", help="best-effort create a v1 database/v2 bucket")
    group.add_argument("--enable-source", metavar="NAME", help="add NAME to settings.yaml's sources: list")
    parser.add_argument("--remove", action="store_true", help="remove the named credential instead of setting it")
    return parser


def _require_root():
    if os.geteuid() != 0:
        raise CredentialCliError("must be run as root (sudo) - it writes /etc/send-to-influx and systemd unit config")


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        # --detect-influx-version is the one truly read-only subcommand - a network
        # probe, unrelated to any local file - so it's the only one checked before
        # _require_root().
        if args.detect_influx_version is not None:
            _cmd_detect_influx_version(args.detect_influx_version)
            return 0

        # Everything else writes to /etc/send-to-influx and/or systemd unit config,
        # or (--list) reads credstore_dir - which is 0700 root:root, so a non-root
        # caller wouldn't get a PermissionError here, just os.path.isfile() silently
        # returning False for every credential and --list misreporting everything
        # as "not set". Require root consistently across all of them.
        _require_root()

        if args.list:
            _cmd_list()
            return 0
        if args.set_field is not None:
            _cmd_set_field(args.set_field[0], args.set_field[1], args.settings)
            return 0
        if args.ensure_influx_storage is not None:
            _cmd_ensure_influx_storage(args.ensure_influx_storage, args.settings)
            return 0
        if args.enable_source is not None:
            _cmd_enable_source(args.enable_source, args.settings)
            return 0

        if args.remove:
            _cmd_remove(args.name, args.settings)
        else:
            _cmd_set(args.name, args.settings)
        return 0
    except CredentialCliError as exc:
        print(f"send-to-influx-set-credential: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
