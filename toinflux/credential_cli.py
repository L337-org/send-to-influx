"""send-to-influx-set-credential: the credential CLI for the packaged install.

Manages secrets in systemd-creds for the packaged .deb/systemd install, and makes small
direct edits to settings.yaml alongside them.

Only meaningful on a systemd host with systemd-creds (systemd >= 250) - not a
requirement of the base package, since that would make the whole package
uninstallable on currently-supported platforms whose systemd is just under that
(e.g. Ubuntu 22.04/jammy ships 249). Checked at runtime instead; see
_require_systemd_creds().
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2025 Gavin Lucas"
__license__ = "MIT"

import argparse
import getpass
import logging
import os
import re
import subprocess
import sys
import tempfile
import warnings
import stat as stat_module

import requests
import urllib3
import yaml

from toinflux.credentials import (
    CANONICAL_SLOT_SUFFIX_RE,
    CREDENTIAL_FIELDS,
    SENTINEL_PREFIX,
    credential_field,
    credential_name_for,
    is_credential_name,
    placeholder_for,
    sentinel_for,
    slot_credential_names,
)

DEFAULT_SETTINGS_PATH = "/etc/send-to-influx/settings.yaml"
# The pristine example the package ships; postinst copies it into place on a fresh
# install and --ensure-section below back-fills individual sections from it on an
# upgrade (settings.yaml itself is never rewritten wholesale - see build-deb.sh).
DEFAULT_EXAMPLE_PATH = "/usr/share/send-to-influx/example_settings.yaml"
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
    r"""Parse the leading version number out of `systemd-creds --version` output.

    For example, "systemd 255 (255.4-1ubuntu8.4)\\n+PAM +AUDIT ...".

    Args:
        version_output (str): raw stdout from `systemd-creds --version`

    Returns:
        int or None: the version number, or None if it couldn't be parsed
    """
    match = re.search(r"systemd\s+(\d+)", version_output)
    return int(match.group(1)) if match else None


def _require_systemd_creds():
    """Confirm systemd-creds exists and is new enough.

    Raises CredentialCliError with a specific, actionable message otherwise.

    Raises:
        CredentialCliError: if systemd-creds is missing or older than MIN_SYSTEMD_CREDS_VERSION
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

    Args:
        name (str): credential name, used only in the interactive prompt

    Returns:
        str
    """
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {name}: ")
    return sys.stdin.read().rstrip("\r\n")


def _validate_secret_value(name, value):
    """Reject empty/placeholder/multiline input before anything is touched on disk.

    Raises:
        CredentialCliError: if the value looks invalid
    """
    if not value.strip():
        raise CredentialCliError("Value must not be empty.")
    if value == placeholder_for(name):
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

    Raises:
        CredentialCliError: if name isn't letters/digits/underscore/hyphen
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
    """Rewrite the systemd drop-in from a fresh directory listing of credstore_dir.

    Idempotent and self-healing if a prior run was interrupted, with no separate state
    file needed.

    Args:
        credstore_dir (pathlib.Path or None): directory holding the ``.cred`` files; None uses the
            packaged default.
        dropin_path (pathlib.Path or None): where to write the drop-in; None uses the packaged
            default.
        exclude (str or None): a credential name to treat as absent even if its .cred file still exists on disk - used
            by _cmd_remove so the drop-in never references a file that's about to be deleted, even transiently
            (LoadCredentialEncrypted= referencing a missing path hard-fails unit startup with 243/CREDENTIALS)

    Raises:
        CredentialCliError: the drop-in file cannot be written
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

    Raises:
        CredentialCliError: if credstore_dir can't be created/secured, if systemd-creds encrypt fails, or if the written
            .cred file can't be secured
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

    Raises:
        CredentialCliError: if the credential doesn't exist or decryption fails
    """
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    cred_path = _cred_path(name, credstore_dir)
    if not os.path.isfile(cred_path):
        raise CredentialCliError(f"No stored credential for '{name}' at {cred_path}.")
    try:
        # --name= must be passed explicitly, mirroring _encrypt_credential():
        # without it, systemd-creds derives the expected name from the *input
        # filename* and validates the embedded name against that - and only
        # systemd >= 254 strips the ".cred" suffix when deriving. On 252/253
        # (e.g. Debian/Raspberry Pi OS bookworm) the derived name is
        # "influx-user.cred", the embedded name is "influx-user", and decrypt
        # refuses with "Embedded credential name ... does not match filename".
        # The systemd service itself was never affected (LoadCredentialEncrypted=
        # NAME:PATH supplies the name) - only this CLI-side decrypt path.
        result = subprocess.run(
            ["systemd-creds", "decrypt", f"--name={name}", cred_path, "-"], check=True, capture_output=True
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else str(exc)
        raise CredentialCliError(f"systemd-creds decrypt failed for '{name}': {stderr}") from exc
    try:
        decoded = result.stdout.decode()
    except UnicodeDecodeError as exc:
        raise CredentialCliError(f"decrypted value for '{name}' is not valid UTF-8: {exc}") from exc
    # Only strip a trailing line ending, not all whitespace - a password can
    # legitimately start/end with spaces, and _encrypt_credential() never appends
    # one, but strip defensively in case anything else in the pipeline did.
    return decoded.rstrip("\r\n")


# --------------------------------------------------------------------------- #
# settings.yaml surgical edit
# --------------------------------------------------------------------------- #


def _atomic_write(path, content):
    """Write content to path atomically, preserving the original file's owner and mode.

    Uses a temp file plus os.replace. A naive rewrite would otherwise land owned by
    whoever ran this script instead of send-to-influx:send-to-influx 0600/0644.

    If path is a symlink, writes through to its resolved target instead of
    replacing the symlink itself - some admins manage settings.yaml as a symlink
    into a separately-managed config source (e.g. a checked-out dotfiles repo).
    os.replace() operates on the given path as a directory entry, not through it,
    so writing to the symlink's own path would silently detach it - the symlink
    gets replaced by a plain file, rather than the thing it points to being
    updated - breaking whatever was managing it that way.

    Raises:
        OSError: the write, the ownership fix-up or the rename failed; the temporary file is removed before the original
            error propagates
    """
    target = os.path.realpath(path) if os.path.islink(path) else path
    directory = os.path.dirname(target) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(content)
        try:
            st = os.stat(target)
            os.chown(tmp_path, st.st_uid, st.st_gid)
            os.chmod(tmp_path, stat_module.S_IMODE(st.st_mode))
        except OSError:
            pass
        os.replace(tmp_path, target)
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
    r"""Escape value for safe embedding inside a YAML double-quoted scalar.

    Order matters: backslashes must be doubled first, so the backslashes this
    function itself introduces for the quote/CR/LF escapes below aren't
    re-escaped by a later step. YAML double-quoted scalars support \\r/\\n as
    genuine escape sequences (unlike single-quoted or plain scalars), so a
    literal newline/carriage return in value becomes an escaped, single-line
    representation rather than splitting the quoted scalar across multiple
    lines - which would otherwise write invalid YAML.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def _is_creatable_field(top_key, field):
    """Whether a missing field may be *created* rather than refused.

    Only a recognised bridge slot - ``hue.hostN`` or ``hue.userN`` - qualifies. Creating any
    field on request would destroy this function's other job: the refusal is what catches a
    typo, so ``--set-field hue.hsot2 <address>`` must stay an error rather than quietly
    becoming a key nothing reads, which would leave the bridge uncollected with nothing
    complaining.

    Slot 1's unnumbered ``host``/``user`` are deliberately absent: they ship in
    example_settings.yaml, so a config without them is not a slot to add but a file to look
    at by hand.
    """
    if top_key != "hue":
        return False
    for stem in ("host", "user"):
        if field.startswith(stem) and CANONICAL_SLOT_SUFFIX_RE.match(field[len(stem) :]):
            return True
    return False


def _last_scalar_line(node):
    """Return the last source line occupied by any scalar inside ``node``.

    Deliberately *not* ``node.end_mark.line``: a block mapping's end mark sits at the next
    token, which in a comment-dense file is past the blank line and the following section's
    leading comment. Inserting there would put the new field under the wrong section's
    comment - or outside the section entirely.
    """
    if isinstance(node, yaml.ScalarNode):
        return node.end_mark.line
    lines = [node.start_mark.line]
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            lines.append(_last_scalar_line(key_node))
            lines.append(_last_scalar_line(value_node))
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            lines.append(_last_scalar_line(item))
    return max(lines)


def _append_field_to_section(settings_path, text, section_node, field, new_value):
    """Insert ``field: value`` at the end of a section's own block.

    Preserves every existing byte.

    The same property that makes ``_ensure_section`` safe - appending never rewrites what is
    already there - except the insertion point has to be *inside* the section rather than at
    end of file, so it lands after the section's last scalar (see ``_last_scalar_line``) at
    the indentation of an existing sibling key.

    Raises:
        CredentialCliError: the section is empty or flow-style, so there is no sibling key to copy indentation from and
            no safe place to insert
    """
    if not isinstance(section_node, yaml.MappingNode) or not section_node.value:
        raise CredentialCliError(
            f"{settings_path}: cannot add {field} automatically - the section has no fields to "
            "add it alongside; edit it by hand instead"
        )
    lines = text.splitlines(keepends=True)
    first_key = section_node.value[0][0]
    indent = " " * first_key.start_mark.column
    if indent == "":
        raise CredentialCliError(
            f"{settings_path}: cannot add {field} automatically - the section looks flow-style; "
            "edit it by hand instead"
        )
    insert_at = _last_scalar_line(section_node) + 1
    # Double-quoted, exactly as the replace path writes a value: _yaml_double_quoted_escape
    # escapes the *inner* text only, so the quotes are the caller's job. Unquoted would
    # happen to parse for a hostname and then break on a value containing '#' (a comment
    # from there on) or ': ', and would be inconsistent with every value this tool rewrites.
    escaped = _yaml_double_quoted_escape(new_value)
    lines.insert(insert_at, f'{indent}{field}: "{escaped}"\n')
    _atomic_write(settings_path, "".join(lines))


def _locate_rewritable_value(settings_path, text, top_node, top_key, field, new_value):
    """Return the scalar node whose value should be replaced.

    None if the field was *created* instead, in which case the file has already been
    written.

    Split out of _rewrite_settings_field so that function stays within the complexity limit
    and reads as "find the line, then splice it".

    Raises:
        CredentialCliError: the field is absent and not creatable, or is not a plain single-line scalar
    """
    value_node = _find_mapping_value(top_node, field)
    if value_node is not None and value_node.start_mark.line == value_node.end_mark.line:
        return value_node
    if value_node is None and _is_creatable_field(top_key, field):
        # A recognised bridge slot may be created; anything else is refused, because that
        # refusal is what catches a typo (see _is_creatable_field).
        _append_field_to_section(settings_path, text, top_node, field, new_value)
        return None
    raise CredentialCliError(
        f"{settings_path}: could not safely rewrite {top_key}.{field} automatically "
        "(missing, or not a plain single-line value) - edit it by hand instead"
    )


def _rewrite_settings_field(settings_path, top_key, field, new_value):
    """Replace a single scalar field's value in place.

    Preserves every other byte of the file (comments, ordering, blank lines) by locating
    the exact source line via yaml.compose() rather than a full load+dump round trip,
    which would silently strip every comment - example_settings.yaml is comment-dense and
    users are expected to keep reading and editing it.

    Raises:
        CredentialCliError: if the target section/field doesn't exist, or isn't a plain single-line scalar (e.g.
            hand-edited into a block scalar) - refuses rather than corrupting the file; also raised (rather than an
            unhandled OSError escaping main()'s exception handling) if settings_path can't be read or written, e.g.
            missing file or a permissions problem
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
    value_node = _locate_rewritable_value(settings_path, text, top_node, top_key, field, new_value)
    if value_node is None:
        return  # created rather than rewritten

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


def _compose_settings_mapping(settings_path):
    r"""Read settings_path and parse it into a yaml.compose() MappingNode.

    Split out of _load_sources_sequence() so that function stays within the
    complexity limit, and because the empty-file/non-mapping guard belongs with
    the read+parse step rather than with sources:-specific logic.

    Raises:
        CredentialCliError: if the file can't be read, isn't valid YAML, or is syntactically valid YAML with no
            top-level mapping (e.g. an empty file, or a bare sequence/scalar document like "- a\\n- b\\n") - neither is
            a valid settings.yaml, and without this check the caller's own (key, value) iteration would raise a raw
            AttributeError/TypeError instead
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

    if not isinstance(root, yaml.MappingNode):
        raise CredentialCliError(f"{settings_path}: does not contain a top-level mapping - edit it manually first")
    return text, root


def _load_sources_sequence(settings_path):
    """Read and parse settings_path for its top-level `sources:` key and sequence.

    Returns (text, sources_key_node, sources_node).

    `sources_node` is None when `sources:` is empty - either a bare key with nothing
    but comments under it (parses as a null scalar - the shipped default, since a
    fresh install enables nothing until the admin uncomments what they want) or the
    less common but equally valid explicit `sources: []`. Neither has an existing
    item to anchor an append on, so _enable_source() handles that case by rewriting
    the key's own line into a block sequence instead.

    Raises:
        CredentialCliError: see _compose_settings_mapping(), plus if `sources:` is missing entirely, or is a populated
            flow-style sequence (e.g. `sources: [a, b]`) - there's no safe way to turn that into a block sequence by
            inserting a line after it without producing invalid YAML
    """
    text, root = _compose_settings_mapping(settings_path)

    sources_key = next((key_node for key_node, _ in root.value if key_node.value == "sources"), None)
    sources_node = _find_mapping_value(root, "sources")
    if sources_key is None or sources_node is None:
        raise CredentialCliError(f"{settings_path}: no 'sources:' key found - add it manually first")
    if isinstance(sources_node, yaml.ScalarNode) and sources_node.tag == "tag:yaml.org,2002:null":
        # A bare `sources:` with nothing after it (comment-only lines don't count) -
        # the shipped default - or an explicit null spelling (`~`, `null`, `Null`,
        # `NULL`). All of these compose to a ScalarNode whose *raw text* differs
        # (`.value` is `''`, `'~'`, `'null'`... respectively) but whose resolved
        # tag is uniformly YAML's null tag - check that rather than `.value`
        # being empty, or the non-bare spellings are missed entirely.
        return text, sources_key, None
    if not isinstance(sources_node, yaml.SequenceNode):
        raise CredentialCliError(f"{settings_path}: 'sources:' is not a sequence - edit it manually first")
    if sources_node.flow_style:
        if not sources_node.value:
            # `sources: []` - the other empty shape, less common but handled the same way.
            return text, sources_key, None
        # e.g. `sources: ["hue", "zappi"]` on one line - inserting a new block-style
        # `  - "name"` line after it (this function's only insertion strategy for a
        # populated list) would leave a dangling sequence item with no key of its
        # own, invalid YAML. Rare enough in practice that asking the user to add
        # flow-style entries by hand is a fine trade-off.
        raise CredentialCliError(
            f"{settings_path}: 'sources:' uses flow style (e.g. [a, b]) - add the new source manually"
        )
    return text, sources_key, sources_node


def _enable_source(name, settings_path=None):
    """Idempotently append `name` to settings.yaml's top-level `sources:` sequence.

    Preserves the rest of the file untouched, and is a no-op if already present, so a
    later dpkg-reconfigure re-running this does not duplicate entries.

    Used instead of _rewrite_settings_field(), which only handles a single-line
    scalar value - `sources:` is a YAML sequence, a structurally different edit.

    Returns:
        bool: True if the file was actually changed, False if `name` was already present (so callers - e.g. the CLI -
            can report an accurate message instead of always claiming "enabled")

    Raises:
        CredentialCliError: see _load_sources_sequence(), plus if settings_path can't be written back
    """
    if settings_path is None:
        settings_path = DEFAULT_SETTINGS_PATH
    text, sources_key, sources_node = _load_sources_sequence(settings_path)
    escaped = _yaml_double_quoted_escape(name)

    if sources_node is None:
        # Empty `sources:` - a bare key, an explicit null spelling (`~`, `null`,
        # `Null`, `NULL`), or `[]` with any internal spacing (`sources:  []` is
        # just as valid as `sources: []` - though `sources:[]` with *no* space at
        # all isn't reliably valid YAML once another key follows in the same file,
        # per direct testing; the regex below still matches it since it's harmless
        # dead code there - yaml.compose() in _load_sources_sequence() would
        # already have raised a parse error before this point for that case) - no
        # existing item to append after. All of these fit entirely on the key's
        # own line (see _load_sources_sequence), so replacing that one line with
        # the key plus a single block item is safe - but only when there's
        # nothing else trailing on it (e.g. an inline comment), which would
        # otherwise be silently dropped.
        lines = text.splitlines(keepends=True)
        key_line = lines[sources_key.start_mark.line]
        indent = key_line[: len(key_line) - len(key_line.lstrip())]
        if not re.fullmatch(r"sources:\s*(\[\s*\]|~|null|Null|NULL)?", key_line.strip()):
            raise CredentialCliError(
                f"{settings_path}: could not safely rewrite the empty 'sources:' line automatically "
                "(unexpected trailing content, e.g. a comment) - edit it by hand instead"
            )
        lines[sources_key.start_mark.line] = f'{indent}sources:\n{indent}  - "{escaped}"\n'
        try:
            _atomic_write(settings_path, "".join(lines))
        except OSError as exc:
            raise CredentialCliError(f"could not write {settings_path}: {exc}") from exc
        return True

    existing = [item.value for item in sources_node.value if isinstance(item, yaml.ScalarNode)]
    if name in existing:
        return False

    lines = text.splitlines(keepends=True)
    last_item = sources_node.value[-1]
    item_line = lines[last_item.start_mark.line]
    indent = item_line[: len(item_line) - len(item_line.lstrip())]
    insert_at = last_item.end_mark.line + 1

    lines.insert(insert_at, f'{indent}- "{escaped}"\n')

    try:
        _atomic_write(settings_path, "".join(lines))
    except OSError as exc:
        raise CredentialCliError(f"could not write {settings_path}: {exc}") from exc
    return True


# --------------------------------------------------------------------------- #
# InfluxDB version detection / storage creation (used by Part 2's debconf postinst)
# --------------------------------------------------------------------------- #


def _detect_influx_version(url):
    """Probe url to determine whether it is an InfluxDB v1 or v2 instance.

    Needs no credential: both /health (v2) and /ping (v1, and v2 for backward compat) are
    unauthenticated health-check endpoints on real InfluxDB servers.

    Always skips TLS verification, unconditionally - unlike _ensure_influx_storage(),
    this never transmits a credential (no auth header, no auth tuple) and the result
    only picks which prompt fields get routed to (v1 user/password vs v2 org/token),
    not a trust decision that could be meaningfully downgraded by a MITM'd response.
    influx.insecure also isn't necessarily known yet when this runs - postinst's
    debconf-driven flow calls this before that field could even be collected (it's
    never asked by debconf, only ever hand-edited into settings.yaml afterwards).

    Returns:
        str: "v1", "v2", or "unknown" (unreachable/ambiguous - never raises)
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        try:
            resp = requests.get(f"{url.rstrip('/')}/health", verify=False, timeout=HTTP_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                data = resp.json()
                if str(data.get("version", "")).startswith("2."):
                    return "v2"
        except (requests.RequestException, ValueError):
            pass

        try:
            resp = requests.get(f"{url.rstrip('/')}/ping", verify=False, timeout=HTTP_TIMEOUT_SECONDS)
            version = resp.headers.get("X-Influxdb-Version", "")
            if version.startswith("1."):
                return "v1"
            if version.startswith("2."):
                return "v2"
        except requests.RequestException:
            pass

    return "unknown"


def _resolve_credential_value(name, influx, credstore_dir):
    """Return the real value for one of the influx.* credential fields.

    Works whether or not it has been migrated to systemd-creds - both are legitimate,
    since migration is opt-in and per-field (see toinflux.credentials). If the plain
    settings.yaml value is the systemd-creds sentinel, decrypt the real value instead;
    otherwise the plain value already *is* the real value, never migrated.

    Args:
        name (str): the credential field name, e.g. ``token`` or ``password``.
        influx (dict): the parsed `influx:` settings block
        credstore_dir (pathlib.Path or None): directory holding the ``.cred`` files; None uses the
            packaged default.
    """
    # Only ever called for the influx credentials, which are static - but routed through the
    # shared mapping regardless, so no credential lookup in this file can be the one that
    # forgets about slots.
    _, field = credential_field(name)
    plain_value = influx.get(field, "")
    if isinstance(plain_value, str) and plain_value.startswith(SENTINEL_PREFIX):
        return _decrypt_credential(name, credstore_dir)
    return plain_value


def _ensure_influx_storage(name, settings_path=None, credstore_dir=None):
    """Best-effort create the InfluxDB database (v1) or bucket (v2) named `name`.

    Never raises on failure (permissions, auth, unreachable) - logs and returns, since
    install and auto-enable must not be blocked by this.

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
        # Must match toinflux/influx.py's own verify=not insecure - otherwise this
        # would fail (falls into the broad except below, logged as an opaque TLS
        # error) against exactly the self-signed-certificate setups insecure: true
        # exists to support, even though the normal sender path works fine.
        insecure = bool(influx.get("insecure", False))
        verify = not insecure

        # A token configures v2 whether it's plain or already migrated to
        # systemd-creds (a migrated field's plain settings.yaml value is the
        # sentinel text, still non-empty/truthy) - matches
        # toinflux.general._validate_influx_block's own `is_v2 = bool(token)` check.
        # Checking for a `.cred` file's existence instead (as an earlier version of
        # this function did) gets this wrong for a token that's never been migrated.
        is_v2 = bool(influx.get("token"))
        with warnings.catch_warnings():
            if insecure:
                warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            if is_v2:
                token = _resolve_credential_value("influx-token", influx, credstore_dir)
                org = influx.get("org", "")
                headers = {"Authorization": f"Token {token}"}
                resp = requests.get(
                    f"{url}/api/v2/buckets",
                    params={"org": org},
                    headers=headers,
                    verify=verify,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
                resp.raise_for_status()
                existing = {b.get("name") for b in resp.json().get("buckets", [])}
                if name in existing:
                    logging.info("InfluxDB bucket '%s' already exists", name)
                    return
                resp = requests.post(
                    f"{url}/api/v2/buckets",
                    headers=headers,
                    json={"name": name, "orgID": _resolve_org_id(url, headers, org, verify)},
                    verify=verify,
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
                    verify=verify,
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


def _resolve_org_id(url, headers, org_name, verify=True):
    """Look up the org ID for org_name.

    The v2 bucket-create API needs orgID, not just the org name.

    Raises:
        CredentialCliError: the org is unknown, or the API rejected the lookup
    """
    resp = requests.get(
        f"{url}/api/v2/orgs", params={"org": org_name}, headers=headers, verify=verify, timeout=HTTP_TIMEOUT_SECONDS
    )
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
    top_key, field = credential_field(name)
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
    top_key, field = credential_field(name)
    _rewrite_settings_field(settings_path, top_key, field, placeholder_for(name))
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


def _cmd_list(credstore_dir=None, settings_path=None):
    """Print each credential and whether it is stored in systemd-creds.

    The static credentials always appear, configured or not, so the list doubles as "what
    can be set". Slot credentials cannot be enumerated that way - there is no upper bound -
    so they are *discovered*: any ``hue.userN`` in settings.yaml, plus any slot credential
    already in the credstore. The second half is what surfaces an orphan, a credential left
    behind after its bridge was removed; it is reported rather than cleaned up, since removing
    a stored secret is not something to do as a side effect of a listing.
    """
    if credstore_dir is None:
        credstore_dir = CREDSTORE_DIR
    static = sorted(CREDENTIAL_FIELDS)
    from_settings = slot_credential_names(_read_settings_or_empty(settings_path or DEFAULT_SETTINGS_PATH))
    for name in [*static, *from_settings]:
        status = "configured" if os.path.isfile(_cred_path(name, credstore_dir)) else "not set"
        print(f"{name}: {status}")
    known = {*static, *from_settings}
    for name in _stored_credential_names(credstore_dir):
        if name not in known:
            print(f"{name}: configured, but no matching field in {settings_path or DEFAULT_SETTINGS_PATH}")


def _read_settings_or_empty(settings_path):
    """Parse settings.yaml for read-only inspection, returning {} on any problem.

    Used only to discover which slot credentials a config implies. A missing or malformed
    file must not stop ``--list`` from reporting the static credentials, which is exactly
    what someone diagnosing a broken config needs to see.
    """
    try:
        with open(settings_path, encoding="utf8") as handle:
            settings = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _stored_credential_names(credstore_dir):
    """Return the credential names actually present in the credstore, in name order.

    Anything in the directory that is not a credential name this tool manages is ignored -
    ``LoadCredentialEncrypted=`` is not exclusive to us.
    """
    try:
        entries = sorted(os.listdir(credstore_dir))
    except OSError:
        return []
    suffix = ".cred"
    names = [entry[: -len(suffix)] for entry in entries if entry.endswith(suffix)]
    return [name for name in names if is_credential_name(name)]


def _extract_section(text, name):
    """Return the source lines of top-level section ``name`` from a settings file.

    Includes the comment block immediately above it and any trailing blank line, or None
    if the section is not there.

    Deliberately textual rather than a YAML round trip: the point is to copy the
    shipped example's *documentation* (its comments explain every field) into the
    user's file verbatim, which a load+dump would discard.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\s*:", line):
            start = i
            break
    if start is None:
        return None
    # Walk back over the section's own comment block (contiguous comment lines
    # immediately above it), so the copied section keeps its explanatory header.
    first = start
    # Not lstrip()'ed deliberately: a top-level section's own comment block starts
    # in column 0, whereas an indented comment belongs to the *previous* section's
    # body and must not be dragged along with this one.
    while first > 0 and lines[first - 1].startswith("#"):
        first -= 1
    # A top-level section ends at the next line that starts in column 0 and isn't
    # blank - i.e. the next section or its comment block.
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line[0].isspace():
            break
        end += 1
    # Trim trailing blank lines; the caller re-adds a single separator.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[first:end])


def _require_mapping_document(root, settings_path):
    r"""Refuse a settings file whose top-level YAML document isn't a mapping.

    An empty file (``root is None``) is fine to append to - the appended section
    simply becomes the whole document. Anything else must already be a mapping:
    appending ``name:\n  ...`` to a scalar or a list produces a file that is not
    valid YAML at all, turning a settings file that was merely wrong into one the
    service cannot load. Refusing leaves the damage where the admin left it.

    Args:
        root: composed YAML root node, or None for an empty document
        settings_path (str): path, for the error message

    Raises:
        CredentialCliError: if the document exists and isn't a mapping
    """
    if root is None or isinstance(root, yaml.MappingNode):
        return
    kind = type(root).__name__.replace("Node", "").lower()
    raise CredentialCliError(
        f"{settings_path}: the top-level YAML document is a {kind}, not a mapping of "
        "settings sections - fix the file by hand before adding sections to it"
    )


def _ensure_section(settings_path, name, example_path):
    """Append top-level section ``name`` to settings.yaml, copied from the shipped example.

    A no-op if the file already has it. Returns True if it was added.

    This exists because settings.yaml is created once at install time and then
    never rewritten by an upgrade (a deliberate Debian-policy choice - see
    build-deb.sh). Any section introduced by a *later* release therefore doesn't
    exist for already-installed users, so anything that assumes it does -
    --set-field, and enabling a source whose block is new - fails on exactly the
    installs that have been running longest. Appending is safe in a way that
    rewriting is not: every existing byte is preserved.

    Raises:
        CredentialCliError: if either file can't be read/written, or the example doesn't contain the requested section
    """
    try:
        with open(settings_path, encoding="utf8") as f:
            current = f.read()
    except OSError as exc:
        raise CredentialCliError(f"could not read {settings_path}: {exc}") from exc

    try:
        root = yaml.compose(current)
    except yaml.YAMLError as exc:
        raise CredentialCliError(f"{settings_path}: could not parse YAML: {exc}") from exc
    _require_mapping_document(root, settings_path)
    if root is not None and _find_mapping_value(root, name) is not None:
        return False

    try:
        with open(example_path, encoding="utf8") as f:
            example = f.read()
    except OSError as exc:
        raise CredentialCliError(f"could not read {example_path}: {exc}") from exc

    section = _extract_section(example, name)
    if section is None:
        raise CredentialCliError(f"{example_path}: no '{name}:' section to copy from")

    separator = "" if current.endswith("\n\n") else ("\n" if current.endswith("\n") else "\n\n")
    _atomic_write(settings_path, current + separator + section)
    return True


def _cmd_ensure_section(name, settings_path, example_path):
    if _ensure_section(settings_path, name, example_path):
        print(f"Added the '{name}:' section to {settings_path} from {example_path}.")
    else:
        print(f"'{name}:' already present in {settings_path} - nothing to do.")


def _cmd_set_field(dotted_path, value, settings_path):
    top_key, _, field = dotted_path.partition(".")
    if not field:
        raise CredentialCliError(f"'{dotted_path}' must be in the form <section>.<field>, e.g. hue.host")
    # Covers numbered slots as well as the static table - hue.user2 is every bit as much a
    # secret as hue.user, and this refusal is what stops a token being written back into
    # settings.yaml in plaintext.
    credential_name = credential_name_for(top_key, field)
    if credential_name is not None:
        raise CredentialCliError(
            f"'{dotted_path}' is a credential field - --set-field only writes plain, "
            f"non-secret values, and would put it back into {settings_path} in plaintext. "
            f"Use 'send-to-influx-set-credential {credential_name}' instead."
        )
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


def _credential_name_arg(value):
    """An argparse type for the credential-name positional.

    Replaces a fixed ``choices=`` list, which cannot express unbounded slot credentials.
    Rejection is just as firm - a typo is refused, not silently accepted as a new credential -
    but the acceptable set is described rather than enumerated.

    Raises:
        argparse.ArgumentTypeError: the name is neither a known credential field nor a
            numbered Hue bridge username
    """
    if is_credential_name(value):
        return value
    raise argparse.ArgumentTypeError(
        f"unknown credential {value!r}. Valid names: {', '.join(sorted(CREDENTIAL_FIELDS))}, "
        f"or a numbered Hue bridge username - hue-user2, hue-user3, ... (hue-user is the first bridge)"
    )


def _build_parser():
    parser = argparse.ArgumentParser(prog="send-to-influx-set-credential")
    parser.add_argument(
        "--settings", default=DEFAULT_SETTINGS_PATH, help="settings.yaml to update (default: %(default)s)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    # A validator rather than choices=: slot credentials (hue-user2, hue-user3, ...) are
    # unbounded, so there is no list to enumerate. The validator still refuses anything that
    # is not a credential this tool manages, and names what is acceptable - so a typo is
    # rejected as firmly as choices= rejected it, just without the cap.
    group.add_argument(
        "name",
        nargs="?",
        type=_credential_name_arg,
        help="credential name to set/remove (e.g. influx-token, hue-user, hue-user2)",
    )
    group.add_argument("--list", action="store_true", help="list which credentials are configured")
    group.add_argument("--set-field", nargs=2, metavar=("PATH", "VALUE"), help="write a plain, non-secret YAML field")
    group.add_argument("--detect-influx-version", metavar="URL", help="probe URL and print v1/v2/unknown")
    group.add_argument("--ensure-influx-storage", metavar="NAME", help="best-effort create a v1 database/v2 bucket")
    group.add_argument("--enable-source", metavar="NAME", help="add NAME to settings.yaml's sources: list")
    group.add_argument(
        "--ensure-section",
        metavar="NAME",
        help="append top-level section NAME from the shipped example if settings.yaml lacks it",
    )
    parser.add_argument(
        "--example", default=DEFAULT_EXAMPLE_PATH, help="example settings to copy sections from (default: %(default)s)"
    )
    parser.add_argument("--remove", action="store_true", help="remove the named credential instead of setting it")
    return parser


def _require_root():
    if os.geteuid() != 0:
        raise CredentialCliError("must be run as root (sudo) - it writes /etc/send-to-influx and systemd unit config")


def main(argv=None):
    """Run the credential CLI.

    Args:
        argv (list or None): Argument vector to parse, or None to read sys.argv.

    Returns:
        int: A process exit status.
    """
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
            _cmd_list(settings_path=args.settings)
            return 0
        if args.set_field is not None:
            _cmd_set_field(args.set_field[0], args.set_field[1], args.settings)
            return 0
        if args.ensure_influx_storage is not None:
            _cmd_ensure_influx_storage(args.ensure_influx_storage, args.settings)
            return 0
        if args.ensure_section is not None:
            _cmd_ensure_section(args.ensure_section, args.settings, args.example)
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
