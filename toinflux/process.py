"""The single entry point for running an external command.

Every process this project starts goes through :func:`run_command`. Invoking a
subprocess anywhere else is a review-blocking defect, not a style preference: the
protections here (no shell, an allow-listed environment, a mandatory timeout, a cap
on captured output) are worth nothing if one call site skips them.

Errors raised here name the command and the failure, and deliberately do not try to
name the *operation*. A caller knows it was storing a credential or launching a
control; this module only knows it ran ``systemd-creds``. Callers wrap what comes
out of here with that context rather than passing it in.

Nothing here logs captured output. A command's stdout can be a decrypted secret
(see ``toinflux.credential_cli``), so the decision to log any of it belongs to the
caller that knows what the bytes are.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from toinflux.exceptions import ConfigError, ToInfluxError

# Environment variables passed through to a child. Everything else is dropped, so a
# child starts from a known environment rather than whatever the operator, the shell
# or a previous process left behind.
#
# Each entry earns its place:
#   PATH                    resolves the binary, and the child's own lookups
#   HOME                    tools that read a per-user config or cache
#   LANG/LC_ALL/LC_CTYPE    the encoding a child writes its output in
#   TZ                      local-time formatting, which controls compare against
#   XDG_RUNTIME_DIR         systemctl talking to a user manager
#   DBUS_SESSION_BUS_ADDRESS  ditto
#   CREDENTIALS_DIRECTORY   systemd's credential drop; a child reads its own secrets
#                           from here rather than being handed them by its parent
#   STATE_DIRECTORY         where a control process finds its configuration
#
# The last two are the reason this list is not shorter. Dropping either produces a
# child that cannot find its credentials or its state and reports it as a
# permissions or missing-file problem, which is a long way from the cause.
INHERITED_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "CREDENTIALS_DIRECTORY",
    "STATE_DIRECTORY",
)

# Ceiling on what is kept from each of stdout and stderr. Generous for a command
# this project runs on purpose, and small enough that a process stuck emitting
# output cannot exhaust memory. Reading continues past it (see _drain) so the cap
# never turns into a deadlock.
MAX_CAPTURED_BYTES = 1024 * 1024

# How long to wait for a reader thread after the process has gone. A grandchild
# holding the inherited pipe open keeps it from reaching EOF, so this bounds the
# wait rather than trusting the pipe to close.
_DRAIN_GRACE_SECONDS = 5.0

_READ_CHUNK = 65536


class ProcessError(ToInfluxError):
    """A command could not be run to completion.

    Raised when there is no exit status to report: the command timed out and was
    killed. A command that ran and failed is *returned* as a
    :class:`CommandResult` with a non-zero ``returncode`` instead, because its
    output is what the caller needs to explain the failure.

    A missing or non-executable binary raises ``ConfigError`` rather than this,
    because waiting does not install a package.
    """


@dataclass(frozen=True)
class CommandResult:
    """What a finished command produced.

    ``stdout`` and ``stderr`` are raw bytes, not text. Callers that need a string
    choose how to decode: :attr:`stdout_text` decodes with replacement for a log
    line or an error message, while a caller holding something that must be exactly
    what the command emitted (a credential) decodes strictly itself and treats
    invalid bytes as the failure they are.

    Attributes:
        argv (list): the resolved argument list, with argv[0] as the absolute path run
        returncode (int): the exit status; negative means killed by that signal
        stdout (bytes): captured standard output, at most ``MAX_CAPTURED_BYTES``
        stderr (bytes): captured standard error, at most ``MAX_CAPTURED_BYTES``
        stdout_truncated (bool): True when stdout exceeded the cap and was cut
        stderr_truncated (bool): True when stderr exceeded the cap and was cut
    """

    argv: list
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self):
        """Whether the command reported success.

        Returns:
            bool: True when the exit status was zero
        """
        return self.returncode == 0

    @property
    def stdout_text(self):
        """Standard output as text, safe to put in a message.

        Returns:
            str: stdout decoded as UTF-8 with undecodable bytes replaced, marked as
                truncated when the cap was hit
        """
        return _as_text(self.stdout, self.stdout_truncated)

    @property
    def stderr_text(self):
        """Standard error as text, safe to put in a message.

        Returns:
            str: stderr decoded as UTF-8 with undecodable bytes replaced, marked as
                truncated when the cap was hit
        """
        return _as_text(self.stderr, self.stderr_truncated)


def _as_text(raw, truncated):
    """Decode captured output for display, saying so when it was cut short.

    Args:
        raw (bytes): the captured bytes
        truncated (bool): whether the cap was reached

    Returns:
        str: the decoded text, with a truncation marker appended where it applies
    """
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += f" [truncated at {MAX_CAPTURED_BYTES} bytes]"
    return text


def _child_environment(env_extra=None):
    """Build the child's environment from the allow-list.

    Args:
        env_extra (dict or None): additional variables to set, overriding an
            inherited value of the same name; None adds nothing

    Returns:
        dict: the environment to hand the child
    """
    env = {key: os.environ[key] for key in INHERITED_ENV_KEYS if key in os.environ}
    if env_extra:
        env.update(env_extra)
    return env


def _resolve_executable(name, search_path):
    """Find the binary to execute, failing with something an operator can act on.

    Args:
        name (str): argv[0]: a bare command name to look up, or a path to use as given
        search_path (str or None): the PATH the child will run with, so that lookup
            and execution cannot disagree; None means the system default

    Returns:
        str: an absolute path to the executable

    Raises:
        ConfigError: the name is not on the path, or the path given is not executable
    """
    if os.path.sep in name:
        # An explicit path, which is how a child gets started from a location no PATH
        # names - a console script inside the packaged venv, say. Checked before the
        # spawn so the failure says which path was wrong rather than surfacing as a
        # bare ENOENT from exec.
        if not os.path.isfile(name) or not os.access(name, os.X_OK):
            raise ConfigError(f"cannot run {name!r}: not an executable file")
        return os.path.abspath(name)
    resolved = shutil.which(name, path=search_path)
    if resolved is None:
        raise ConfigError(
            f"cannot run {name!r}: not found on PATH ({search_path or os.defpath!r}). "
            f"Install it, or put it on the PATH this service runs with"
        )
    return resolved


def _drain(stream, limit, sink) -> None:
    """Read a pipe to EOF, keeping only the first ``limit`` bytes.

    Reading continues after the cap is reached rather than stopping. A child that
    fills the pipe blocks on write, so a reader that stopped early would deadlock
    with a child that never exits, and the cap would have converted a large output
    into a hang.

    Args:
        stream (io.BufferedReader): the pipe to read
        limit (int): how many bytes to keep
        sink (list): appended with a single ``(bytes, truncated)`` tuple
    """
    kept = bytearray()
    truncated = False
    while True:
        chunk = stream.read1(_READ_CHUNK)
        if not chunk:
            break
        room = limit - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > max(room, 0):
            truncated = True
    sink.append((bytes(kept), truncated))


def _feed(stream, data) -> None:
    """Write the child's standard input and close it.

    Args:
        stream (io.BufferedWriter): the child's stdin pipe
        data (bytes or None): what to write; None writes nothing
    """
    try:
        if data:
            stream.write(data)
        stream.close()
    except OSError:
        # A child that exits before reading its input breaks the pipe. That is not a
        # separate failure to report: the exit status already says the command did not
        # do what was asked, and it says it more usefully than "broken pipe" would.
        pass


def _collect(sink):
    """Take a drained stream's result, tolerating a reader that never finished.

    Args:
        sink (list): the list a :func:`_drain` thread appends to

    Returns:
        tuple: ``(bytes, truncated)``, empty and marked truncated if the reader is
            still running
    """
    if sink:
        return sink[0]
    # The reader outlived its grace period, which means something still holds the
    # pipe. Report what that cost - nothing captured - rather than an empty string a
    # caller would read as "the command said nothing".
    return b"", True


def run_command(argv, *, timeout, stdin_bytes=None, env_extra=None, output_limit=MAX_CAPTURED_BYTES):
    """Run an external command and return what it produced.

    Never uses a shell, resolves argv[0] before spawning, gives the child an
    allow-listed environment, and keeps at most ``output_limit`` bytes of each
    output stream.

    A command that runs and exits non-zero is a *returned* result, not an
    exception: its output is usually the only explanation of why it failed, and
    some callers legitimately ignore the status. Check :attr:`CommandResult.ok`.

    Args:
        argv (list): the command and its arguments; argv[0] is a bare name to look
            up on PATH, or a path to an executable to run as given
        timeout (int or float): seconds to allow before the command is killed, sized
            to the work it does; there is no default because no default fits every
            command
        stdin_bytes (bytes or None): written to the child's standard input, which is
            how a secret reaches a command without appearing in its arguments; None
            gives the child an empty stdin
        env_extra (dict or None): variables to add to the allow-listed environment,
            overriding an inherited value of the same name
        output_limit (int): bytes to keep from each of stdout and stderr

    Returns:
        CommandResult: the exit status and captured output, whether or not it succeeded

    Raises:
        ConfigError: argv[0] could not be resolved to an executable, or the child
            could not be spawned
        ProcessError: the command did not finish within ``timeout`` and was killed
    """
    env = _child_environment(env_extra)
    executable = _resolve_executable(argv[0], env.get("PATH"))
    resolved_argv = [executable, *argv[1:]]

    try:
        process = subprocess.Popen(
            resolved_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
        )
    except OSError as exc:
        # Permission denied, an unusable interpreter line, a directory where a binary
        # was expected. None of these resolve by waiting, so they are configuration.
        raise ConfigError(f"could not start {argv[0]!r}: {exc}") from exc

    out_sink, err_sink = [], []
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, output_limit, out_sink), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, output_limit, err_sink), daemon=True),
    ]
    writer = threading.Thread(target=_feed, args=(process.stdin, stdin_bytes), daemon=True)
    for thread in (*readers, writer):
        thread.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()

    for thread in readers:
        thread.join(_DRAIN_GRACE_SECONDS)
    stdout, stdout_truncated = _collect(out_sink)
    stderr, stderr_truncated = _collect(err_sink)

    if timed_out:
        # The captured output goes in the message: a command killed mid-run has
        # usually already said what it was stuck on, and this is the only place that
        # text survives.
        raise ProcessError(
            f"{argv[0]!r} did not finish within {timeout}s and was killed. "
            f"Output so far: {_as_text(stderr, stderr_truncated) or _as_text(stdout, stdout_truncated) or 'none'}"
        )

    logging.debug("ran %s -> exit %s", argv[0], returncode)
    return CommandResult(
        argv=resolved_argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
