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
import selectors
import shutil
import subprocess
import time
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
# output cannot exhaust memory. Reading continues past it (the pump keeps draining
# and discarding) so the cap never turns into a deadlock: a reader that stopped at
# the limit would leave the child blocked writing into a full pipe.
MAX_CAPTURED_BYTES = 1024 * 1024

# How long to keep draining after the child has exited. A grandchild that inherited
# a pipe holds the write end open, so EOF never arrives; this bounds the wait rather
# than trusting a stream nothing is going to close.
_DRAIN_GRACE_SECONDS = 5.0

_READ_CHUNK = 65536

# How often the pump wakes to re-check the deadline and whether the child has gone.
_POLL_SECONDS = 0.1


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
        search_path (str or None): the PATH the child will run with, so that lookup and
            execution cannot disagree; None means the child has none, and the system
            default is substituted here rather than left to the lookup

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
    # Substituted here rather than passed as None: shutil.which(path=None) reads
    # os.environ["PATH"], so the lookup would silently consult the *parent's* PATH while
    # the child runs with the allow-listed one. Those agree today only because the child's
    # PATH is copied from the parent's, which makes it a coincidence rather than the
    # guarantee this function's whole reason for existing claims.
    if search_path is None:
        search_path = os.defpath
    resolved = shutil.which(name, path=search_path)
    if resolved is None:
        # The path actually searched, not a stand-in for it. An empty PATH searches
        # nothing and is a different fault from an absent one, and reporting the default
        # for both sends an operator looking in a directory nothing consulted.
        raise ConfigError(
            f"cannot run {name!r}: not found on PATH ({search_path!r}). "
            f"Install it, or put it on the PATH this service runs with"
        )
    return resolved


class _Capture:
    """One output stream's accumulated bytes and whether the cap was reached.

    Args:
        limit (int): how many bytes to keep
    """

    def __init__(self, limit):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def add(self, chunk) -> None:
        """Keep what fits and remember if anything did not.

        Args:
            chunk (bytes): freshly read bytes
        """
        room = self.limit - len(self.data)
        if room > 0:
            self.data += chunk[:room]
        if len(chunk) > max(room, 0):
            self.truncated = True

    def result(self):
        """The captured bytes and the truncation flag.

        Returns:
            tuple: ``(bytes, truncated)``
        """
        return bytes(self.data), self.truncated


def _register(selector, process, stdin_bytes, captures):
    """Put the child's pipes into non-blocking mode and register them.

    Args:
        selector (selectors.BaseSelector): the selector to register with
        process (subprocess.Popen): the running child
        stdin_bytes (bytes or None): what will be written, or None
        captures (dict): stream name to :class:`_Capture`

    Returns:
        bytes: what still has to be written to standard input
    """
    for name in ("stdout", "stderr"):
        stream = getattr(process, name)
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    if stdin_bytes:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        return stdin_bytes
    # No input to send. Close it now rather than leaving the child waiting on a pipe
    # that will never carry anything.
    process.stdin.close()
    return b""


def _drain_ready(selector, events, captures, pending):
    """Service whichever pipes are ready, once.

    Args:
        selector (selectors.BaseSelector): the selector the pipes are registered with
        events (list): what :meth:`select` returned
        captures (dict): stream name to :class:`_Capture`
        pending (bytes): input still to be written

    Returns:
        bytes: input still to be written after this pass
    """
    for key, _ in events:
        if key.data == "stdin":
            try:
                written = os.write(key.fileobj.fileno(), pending)
                pending = pending[written:]
            except BrokenPipeError:
                # The child exited without reading its input. Its exit status says more
                # about that than a pipe error would, so stop writing and let it stand.
                pending = b""
            if not pending:
                selector.unregister(key.fileobj)
                key.fileobj.close()
            continue
        chunk = os.read(key.fileobj.fileno(), _READ_CHUNK)
        if chunk:
            captures[key.data].add(chunk)
            continue
        selector.unregister(key.fileobj)
    return pending


def _pump(process, stdin_bytes, captures, timeout):
    """Feed standard input and drain both outputs under one deadline.

    Single-threaded on purpose. An earlier version gave each pipe a reader thread, which
    cannot clean up after itself: a grandchild that inherits a pipe holds the write end
    open, so the read never reaches EOF, and closing the stream from another thread waits
    on the same lock the blocked read holds rather than interrupting it (measured: a close
    took as long as the reader stayed blocked). Threads therefore either leaked one per
    call or blocked the caller past its own timeout. With no threads there is nothing to
    leak, and the deadline covers the whole interaction rather than only the wait.

    Args:
        process (subprocess.Popen): the running child
        stdin_bytes (bytes or None): what to write to standard input
        captures (dict): stream name to :class:`_Capture`, filled in place
        timeout (int or float): seconds allowed before the child is deemed overrunning

    Returns:
        bool: True if the deadline passed while the child was still running
    """
    deadline = time.monotonic() + timeout
    abandon_at = None
    selector = selectors.DefaultSelector()
    try:
        pending = _register(selector, process, stdin_bytes, captures)
        while selector.get_map():
            if process.poll() is None:
                if time.monotonic() >= deadline:
                    return True
            elif abandon_at is None:
                # The child is gone but a pipe is still open, which means something that
                # inherited it holds the write end. Give it a moment to flush, then stop
                # waiting on a stream nothing is going to close.
                abandon_at = time.monotonic() + _DRAIN_GRACE_SECONDS
            if abandon_at is not None and time.monotonic() >= abandon_at:
                return False
            pending = _drain_ready(selector, selector.select(timeout=_POLL_SECONDS), captures, pending)
    finally:
        selector.close()
    return False


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
            bufsize=0,
        )
    except OSError as exc:
        # Permission denied, an unusable interpreter line, a directory where a binary
        # was expected. None of these resolve by waiting, so they are configuration.
        raise ConfigError(f"could not start {argv[0]!r}: {exc}") from exc

    captures = {"stdout": _Capture(output_limit), "stderr": _Capture(output_limit)}
    with process:
        timed_out = _pump(process, stdin_bytes, captures, timeout)
        if timed_out:
            process.kill()
        returncode = process.wait()
    stdout, stdout_truncated = captures["stdout"].result()
    stderr, stderr_truncated = captures["stderr"].result()

    if timed_out:
        # Standard error only, never standard output. A command killed mid-run has
        # usually already said on stderr what it was stuck on, and this message is the
        # only place that survives. Standard output is the data channel: `systemd-creds
        # decrypt` writes the plaintext credential there, so a decrypt that hung after
        # emitting part of it would put the secret into an exception message, and from
        # there into the journal and the CLI's own output. Falling back to stdout when
        # stderr was empty is exactly the case where that happens.
        raise ProcessError(
            f"{argv[0]!r} did not finish within {timeout}s and was killed. "
            f"Standard error: {_as_text(stderr, stderr_truncated) or 'none'}"
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
