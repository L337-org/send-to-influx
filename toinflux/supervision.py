"""Watching control processes: one selector loop, one safe state, one backoff.

The parent side of the control subsystem. It starts one process per control, reads their
heartbeats, and decides when one has stopped being a control and become a process that
happens to be running.

**One selector over every pipe, never a thread per pipe.** A grandchild that inherits a
pipe holds its write end open, so a read on that pipe never reaches EOF even after the
direct child has exited - and closing the stream from another thread does not help, because
``close()`` waits on the same lock the blocked read holds. Measured against a grandchild
holding a pipe for ten seconds, ``close()`` took 9.74 seconds to return. A thread-per-pipe
supervisor therefore cannot bound its own shutdown, which is why this one does not have
threads to bound.

**A dead control's devices are the parent's problem.** The child applies its own safe state
on the way out, but a child that was killed, hit an OOM or lost power did not get to. The
parent applies it again after every death it sees: commanding a device off twice is free,
and assuming the child managed it is how a heater stays on.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging
import os
import selectors
import signal
import time
from dataclasses import dataclass

from toinflux.control_process import command_devices
from toinflux.controls import load_control
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.gating import commands_for
from toinflux.process import TimeoutExpired, spawn

#: How many cycles a control may miss before it is killed and restarted. Three, because one
#: missed beat is a slow cycle and two is a bad afternoon, while three in a row is a control
#: that is not coming back on its own.
MISSED_BEATS_BEFORE_KILL = 3

#: The shortest stall threshold to use, whatever a control's cycle says. A control
#: configured with a one-second window would otherwise be killed for a single slow read.
MINIMUM_STALL_SECONDS = 30.0

#: How long to wait for a killed control to actually go before giving up on it.
KILL_GRACE_SECONDS = 5.0


@dataclass
class Child:
    """One running control, and what the parent knows about it.

    Attributes:
        name (str): the control's name.
        process (subprocess.Popen): the running process.
        beats (int or None): the read end of its heartbeat pipe, as a raw descriptor. Raw
            rather than a file object because a selector loop must never block in a read,
            and because one owner closing one descriptor is a rule with no exceptions - a
            buffered file closing it again from a destructor can close a descriptor that a
            later pipe has been given.
        pending (str): the part of a beat that has arrived without its newline yet.
        started_at (float): monotonic reading when it was started.
        last_beat (float): monotonic reading of its most recent heartbeat, or of its start.
        failures (int): consecutive failures, which sets the restart backoff.
        restart_at (float or None): when it may next be started, or None when it is running.
        stall_seconds (float): how long this control may be silent before it is killed,
            derived from its own cycle window.
    """

    name: str
    process: object = None
    beats: "int | None" = None
    pending: str = ""
    started_at: float = 0.0
    last_beat: float = 0.0
    failures: int = 0
    restart_at: "float | None" = None
    stall_seconds: float = MINIMUM_STALL_SECONDS

    @property
    def running(self):
        """Whether this control has a process right now.

        Returns:
            bool: True while it is started
        """
        return self.process is not None


def stall_seconds(document):
    """Return how long a control may be silent before it is considered stalled.

    A control legitimately says nothing while it is spending a cycle window, so the
    threshold has to be a multiple of that window rather than a flat number of seconds -
    the same reasoning the collector's own stall watchdog uses, and for the same reason: a
    flat threshold shorter than the interval flags every slow source on every cycle.

    Args:
        document (dict): the control document

    Returns:
        float: seconds of silence that mean something is wrong
    """
    cycle = float((document.get("output") or {}).get("cycle_seconds", 900))
    return max(MINIMUM_STALL_SECONDS, MISSED_BEATS_BEFORE_KILL * cycle)


@dataclass
class Event:
    """Something the supervisor saw, for a caller that wants to assert on it.

    Attributes:
        kind (str): ``started``, ``beat``, ``died``, ``stalled``, or ``start-failed`` where
            a scheduled restart could not start the process at all.
        name (str): the control it happened to.
        detail (str): what to say about it.
    """

    kind: str
    name: str
    detail: str = ""


class Supervisor:
    """Starts, watches and restarts one process per control.

    Carries ``children`` (control name to :class:`Child`) and ``events`` (what the last
    :meth:`poll` saw).
    """

    def __init__(self, names, settings_file=None, argv_for=None, clock=time.monotonic, backoff=None):
        """Prepare to supervise a set of controls without starting them.

        Args:
            names (iterable): the control names to run
            settings_file (str or None): the settings path the process was started with
            argv_for (callable or None): name -> the argv to start it with, for tests that
                do not want the installed console script
            clock (callable): the monotonic clock, injectable so a backoff is a test rather
                than a wait
            backoff (callable or None): failures -> seconds before a restart

        Raises:
            ConfigError: where a named control cannot be read
        """
        self.settings_file = settings_file
        self._clock = clock
        self._argv_for = argv_for or self._default_argv
        self._backoff = backoff or _default_backoff
        self._selector = selectors.DefaultSelector()
        self.children = {}
        self.events = []
        for name in names:
            # Read now rather than at each start: a control that cannot be read is a
            # configuration fault, and finding that out per restart would turn it into a
            # respawn loop that logs the same message for ever.
            document = load_control(name, settings_file)
            self.children[name] = Child(name=name, stall_seconds=stall_seconds(document))

    def _default_argv(self, name):
        """Return the argv that starts one control through the installed console script.

        Args:
            name (str): the control to start

        Returns:
            list: the command to run
        """
        argv = ["send-to-influx", "--control", name]
        if self.settings_file:
            argv += ["--settings", self.settings_file]
        return argv

    def start(self, name) -> None:
        """Start one control and register its heartbeat pipe.

        Args:
            name (str): the control to start

        Raises:
            ConfigError: where the process could not be started at all
        """
        child = self.children[name]
        read_fd, write_fd = os.pipe()
        try:
            child.process = spawn(
                [*self._argv_for(name), "--heartbeat-fd", str(write_fd)],
                pass_fds=(write_fd,),
            )
        except BaseException:
            # Both ends, because there is no child to own either. A restart that keeps
            # failing would otherwise leak one descriptor per attempt, and a supervisor that
            # runs out of descriptors stops being able to start anything at all - the
            # failure arriving long after the control that caused it.
            os.close(read_fd)
            raise
        finally:
            # The write end goes whatever happened: while the parent holds it open, its own
            # read never reaches EOF, so a child that died would look alive for ever.
            os.close(write_fd)
        child.beats = read_fd
        child.pending = ""
        child.started_at = child.last_beat = self._clock()
        child.restart_at = None
        self._selector.register(child.beats, selectors.EVENT_READ, child)
        self._record("started", name, f"pid {child.process.pid}")

    def start_all(self) -> None:
        """Start every control that is not already running."""
        for name, child in self.children.items():
            if not child.running:
                self.start(name)

    def poll(self, timeout=0.5):
        """Take one pass: read what arrived, notice deaths and stalls, restart what is due.

        Args:
            timeout (float): how long to wait for a pipe to become readable

        Returns:
            list: the events this pass produced
        """
        self.events = []
        for key, _mask in self._selector.select(timeout=timeout):
            self._read(key.data)
        now = self._clock()
        for child in list(self.children.values()):
            if child.running and now - child.last_beat > child.stall_seconds:
                self._kill(child, f"no heartbeat for {now - child.last_beat:.0f}s")
            elif not child.running and child.restart_at is not None and now >= child.restart_at:
                self._restart(child)
        return self.events

    def _restart(self, child) -> None:
        """Start a control again, treating a failure to start as that control's failure.

        A control that cannot be started is one control's problem. Letting it out of here
        would end the supervisor's loop and with it every *other* control's supervision -
        one unstartable control taking the heating down with it.

        Args:
            child (Child): the control to start again
        """
        try:
            self.start(child.name)
        except ConfigError as exc:
            child.failures += 1
            child.restart_at = self._clock() + self._backoff(child.failures)
            logging.error("Control %r could not be restarted: %r. Trying again later", child.name, exc)
            self._record("start-failed", child.name, str(exc))

    def run(self, stop, poll_seconds=0.5) -> None:
        """Poll until asked to stop, then stop every control.

        One thread runs this, and it is the only thread in the design. That is not the same
        as the thread-per-pipe shape the module docstring rules out: what cannot be bounded
        is a thread blocked on a single child's pipe, and this one blocks on a selector over
        all of them with a timeout.

        Args:
            stop (threading.Event): set to end the loop
            poll_seconds (float): how long each pass waits for a pipe to become readable
        """
        self.start_all()
        try:
            while not stop.is_set():
                self.poll(timeout=poll_seconds)
        finally:
            self.stop_all()

    def _read(self, child) -> None:
        """Read whatever one child's pipe has ready, and notice EOF.

        ``os.read`` on the raw descriptor rather than ``readline()`` on a buffered file, and
        the difference matters inside a selector loop. A pipe reported readable need not
        hold a complete line, and ``readline()`` on a blocking descriptor waits for the
        newline - with the whole supervisor behind it, every other control included. A read
        of what is there cannot block, and an empty result is EOF and nothing else.

        A partial beat is kept until the rest of it arrives. Read as a line, it would be a
        heartbeat that never happened, and a control that was killed mid-write would look
        alive until its stall threshold expired.

        Args:
            child (Child): the child whose pipe is readable
        """
        try:
            chunk = os.read(child.beats, 4096)
        except OSError:
            # The descriptor has gone from under us. Same conclusion as EOF: whatever this
            # child is doing, the parent can no longer hear it.
            chunk = b""
        if not chunk:
            # EOF: the write end is closed everywhere, which for a child started with
            # pass_fds means the process itself has gone.
            self._reap(child, "the heartbeat pipe reached EOF")
            return
        child.pending += chunk.decode("utf-8", "replace")
        lines = child.pending.split("\n")
        # Whatever follows the last newline is an incomplete beat, and waits for its rest.
        child.pending = lines.pop()
        for line in lines:
            if not line.strip():
                continue
            child.last_beat = self._clock()
            # A control that is beating again has recovered, so the next failure starts its
            # backoff from the beginning rather than from where the last one left off.
            child.failures = 0
            self._record("beat", child.name, line.strip())

    def _kill(self, child, reason) -> None:
        """End a control that has stopped beating, and treat it as a death.

        Args:
            child (Child): the child to kill
            reason (str): why, for the log line
        """
        logging.warning("Control %r is not responding (%s), killing it", child.name, reason)
        child.process.terminate()
        try:
            child.process.wait(timeout=KILL_GRACE_SECONDS)
        except TimeoutExpired:
            # It ignored SIGTERM, so it does not get a say. A control that will not stop is
            # worse than one that is killed: its devices are energised and nobody is
            # deciding what they should be doing.
            child.process.kill()
            child.process.wait()
        self._reap(child, reason, kind="stalled")

    def _reap(self, child, reason, kind="died") -> None:
        """Record a control's death, make its devices safe, and schedule a restart.

        Args:
            child (Child): the child that has gone
            reason (str): what happened, for the log line
            kind (str): the event kind to record
        """
        status = child.process.poll()
        if status is None:
            status = child.process.wait()
        self._selector.unregister(child.beats)
        os.close(child.beats)
        child.beats = None
        pid = child.process.pid
        child.process = None
        child.failures += 1
        delay = self._backoff(child.failures)
        child.restart_at = self._clock() + delay
        # Name, pid and how it ended, in one line: an operator looking at a journal should
        # not have to correlate two entries to find out which control died and whether
        # something killed it.
        logging.error(
            "Control %r (pid %s) %s: %s. Restarting in %.0fs (failure %s)",
            child.name,
            pid,
            f"was killed by signal {-status}" if status < 0 else f"exited {status}",
            reason,
            delay,
            child.failures,
        )
        self.make_safe(child.name)
        self._record(kind, child.name, reason)

    def make_safe(self, name) -> None:
        """Put a control's devices into its safe state from here.

        The child applies its own on the way out, and this does it again. A child that was
        killed, hit an OOM or lost power did not get to, and commanding a device off twice
        costs nothing next to a heater that stays on because everyone assumed somebody else
        had dealt with it.

        Args:
            name (str): the control whose devices to make safe
        """
        try:
            document = load_control(name, self.settings_file)
            commands = commands_for(document.get("safe_state", "unenergised"), tuple(document.get("devices") or {}))
            if commands is None:
                # leave_unchanged, which is an answer rather than an omission.
                return
            command_devices(document, commands, self.settings_file)
        except (ConfigError, SourceConnectionError) as exc:
            # Logged rather than raised: the supervisor's job is to keep going, and a
            # bridge that cannot be reached now is one the next restart will try again.
            logging.error("Could not make control %r safe after it died: %r", name, exc)

    def stop_all(self) -> None:
        """Stop every running control and leave its devices safe."""
        for child in self.children.values():
            if not child.running:
                continue
            child.process.send_signal(signal.SIGTERM)
        for child in self.children.values():
            if not child.running:
                continue
            try:
                child.process.wait(timeout=KILL_GRACE_SECONDS)
            except TimeoutExpired:
                child.process.kill()
                child.process.wait()
            self._selector.unregister(child.beats)
            os.close(child.beats)
            child.beats = None
            child.process = None
            self.make_safe(child.name)
        self._selector.close()

    def _record(self, kind, name, detail) -> None:
        """Note something that happened, for a caller watching.

        Args:
            kind (str): the event kind
            name (str): the control it happened to
            detail (str): what to say about it
        """
        self.events.append(Event(kind=kind, name=name, detail=detail))


def _default_backoff(failures):
    """Return how long to wait before restarting a control that has failed.

    The collector's own bounded exponential backoff, reused rather than re-derived: a
    supervisor that backed off differently from the one two hundred lines away would be two
    behaviours to explain and one of them would be wrong.

    Args:
        failures (int): consecutive failures

    Returns:
        float: seconds
    """
    from sendtoinflux import get_backoff_delay

    return float(get_backoff_delay(failures))
