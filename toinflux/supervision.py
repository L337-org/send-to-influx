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

**An edited document is a restart, not a re-read.** A control whose document changes is sent
the signal it already handles, and started again from the new one. Re-reading in place was
the alternative and is not cheaper: a handler that sets a flag and returns does not shorten
a control's cycle sleep at all, because Python retries an interrupted sleep with the time
remaining (PEP 475), so nothing would take effect until the window ended - a quarter of an
hour, by default. A handler that raises instead is already unwinding the loop, and from
there starting a fresh process costs one exec and buys back the startup safe-state
assertion. The integral goes, which a control rebuilds in a few cycles and which every
restart already costs.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import logging
import math
import os
import queue
import selectors
import signal
import sys
import threading
import time
from dataclasses import dataclass

from toinflux.control_process import command_devices
from toinflux.controls import (
    DEFAULT_CYCLE_SECONDS,
    actuators_may_be_one,
    actuators_owned,
    control_is_enabled,
    control_path,
    load_control,
    validate_control,
)
from toinflux.exceptions import ConfigError, SourceConnectionError
from toinflux.gating import commands_for
from toinflux.general import load_settings
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

# How long each pass waits for a pipe to become readable. Named rather than left as a
# default argument because the collector's shutdown has to wait at least this long for the
# loop to notice it has been asked to stop, and a number copied there would drift.
DEFAULT_POLL_SECONDS = 0.5


@dataclass
class Child:
    """One running control, and what the parent knows about it.

    Attributes:
        name (str): the control's name.
        document (dict or None): the validated document this process was started from. Kept
            rather than re-read on demand because it names the devices *this* process may
            have energised, and after a delete it is the only description of them left.
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
    document: "dict | None" = None
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


def _usable_control(name, settings_file):
    """Read one control document and refuse it unless it is structurally sound.

    Checked here rather than defended against at each use. A document that parses as YAML
    can still be any shape at all - ``output`` a string, ``devices`` a list - and each of
    those produces its own AttributeError or TypeError somewhere downstream, in a process
    that is also running the collector. One check at the point of reading turns the whole
    class into a ConfigError that names what is wrong, and the code after it can rely on the
    shapes the store guarantees.

    Args:
        name (str): the control to read
        settings_file (str or None): the settings path the process was started with

    Returns:
        dict: the validated document

    Raises:
        ConfigError: where it cannot be read or is not structurally valid
    """
    document = load_control(name, settings_file)
    errors = validate_control(name, document, load_settings(settings_file))
    if errors:
        raise ConfigError(f"control {name!r} is not valid:\n  " + "\n  ".join(errors))
    return document


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

    Raises:
        ConfigError: where the control's cycle window is not a positive number of seconds
    """
    output = document.get("output")
    if output is not None and not isinstance(output, dict):
        # A validated document cannot reach here with this shape, but this is public and
        # somebody will call it with a document that came from somewhere else.
        raise ConfigError(f"output must be a mapping, got {type(output).__name__}")
    cycle = (output or {}).get("cycle_seconds", DEFAULT_CYCLE_SECONDS)
    # Checked rather than converted. A bare float() on a stored document raises ValueError
    # or TypeError, and this runs while the supervisor is being built in the collector's own
    # main process - so one corrupt control would take the collector down with it rather
    # than failing that control. A bool is refused for the usual reason: it is an int, and
    # `cycle_seconds: true` would become a one-second window.
    if isinstance(cycle, bool) or not isinstance(cycle, (int, float)) or not math.isfinite(cycle) or cycle <= 0:
        raise ConfigError(f"output.cycle_seconds must be a positive number of seconds, got {cycle!r}")
    return max(MINIMUM_STALL_SECONDS, MISSED_BEATS_BEFORE_KILL * float(cycle))


@dataclass(frozen=True)
class ControlStatus:
    """What the supervisor can say about one control, for a reader on another thread.

    A snapshot rather than a view of the :class:`Child`: the child is mutated by the
    supervisor's own thread between one attribute read and the next, and a reader
    assembling a report out of it would describe a moment that never existed.

    Attributes:
        name (str): the control's name.
        running (bool): whether it has a process right now.
        pid (int or None): that process's pid, or None where it is not running.
        failures (int): consecutive failures, which is what sets its restart backoff.
        silent_for (float or None): seconds since its last heartbeat, or None where it has
            not started. Seconds rather than a timestamp because the reader cannot know
            which clock the number came from.
        restart_in (float or None): seconds until it may next be started, or None where it
            is running or has no restart scheduled.
    """

    name: str
    running: bool
    pid: "int | None"
    failures: int
    silent_for: "float | None"
    restart_in: "float | None"


def _snapshot(child, now):
    """Describe one child as it is at a single instant.

    **Every field this describes is read once, before any of it is used.** The supervisor's
    own thread rewrites a child as it dies and starts again, and two reads of the same
    attribute are not one value seen twice: ``child.process`` can pass a None check and be
    None by the time ``.pid`` is asked for, which is an AttributeError on the MCP thread and
    one control's restart breaking every caller's listing.

    The same applies to two *different* attributes that together decide one answer.
    ``started_at`` says whether a control has ever been started and ``last_beat`` says when
    it last spoke; ``start()`` sets both, so reading one before that and the other after
    would describe a control that was simultaneously never started and beating. Neither of
    those orderings produces a wrong number today - the branch not taken does not use the
    other value, and the arithmetic is clamped - but an invariant that holds only while
    nobody reorders the expression is not an invariant.

    Reading into locals first is enough on its own - this function takes no lock, and does
    not need one: a single attribute read cannot see a half-written value, so it is only
    ever the combination that needs pinning to one moment. The lock :meth:`Supervisor.status`
    holds while it collects the children is a different concern, and covers only the mapping
    being resized underneath it.

    Args:
        child (Child): the control to describe
        now (float): the monotonic reading to measure ages against

    Returns:
        ControlStatus: what was true at the moment each field was read
    """
    # `name` is fixed when the child is built and never rewritten, so pinning it changes
    # no outcome. It is pinned anyway because "every field, before any is used" is a rule
    # somebody can keep, and "every field except the one that happens to be immutable" is
    # one they have to re-derive - and would get wrong the day a rename makes it mutable.
    name = child.name
    process = child.process
    restart_at = child.restart_at
    started_at = child.started_at
    last_beat = child.last_beat
    failures = child.failures
    return ControlStatus(
        name=name,
        # From the same local as the pid, so the two cannot contradict each other.
        running=process is not None,
        pid=None if process is None else process.pid,
        failures=failures,
        silent_for=None if started_at == 0.0 else max(0.0, now - last_beat),
        restart_in=None if restart_at is None else max(0.0, restart_at - now),
    )


@dataclass
class Event:
    """Something the supervisor saw, for a caller that wants to assert on it.

    Attributes:
        kind (str): ``started``, ``beat``, ``died``, ``stalled``, ``start-failed`` where a
            scheduled restart could not start the process at all, ``reloaded`` where a
            control was stopped deliberately to pick up its changed document, ``dropped``
            where its document has gone and it is no longer supervised, or
            ``reload-failed`` where the changed document could not be used and whatever was
            running was left alone.
        name (str): the control it happened to.
        detail (str): what to say about it. The only field that can carry a value from
            outside, because the failure kinds put an exception here - and it holds
            ``repr(exc)`` rather than ``str(exc)``, the same form the log lines beside it
            use. A YAML parser quotes the offending line from the document in its own
            message, so the bare string carries newlines and whatever was in the file, and
            this is meant to be surfaced to a caller.
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
            ConfigError: never for one unusable control - that one is logged and skipped -
                but the signature keeps the type for a caller that passes nothing readable
        """
        self.settings_file = settings_file
        self._clock = clock
        self._argv_for = argv_for or self._default_argv
        self._backoff = backoff or _default_backoff
        self._selector = selectors.DefaultSelector()
        self._reload_requests = queue.SimpleQueue()
        # Held only where the children mapping gains or loses a key, and while a status
        # snapshot is taken. It is not a lock on a control's state: the supervisor's own
        # thread rewrites a child's process and counters constantly, and a reader that
        # sees one of those a moment late is reading a report, not making a decision. What
        # it cannot survive is the mapping being resized underneath it, which is a
        # RuntimeError rather than a stale number.
        self._children_lock = threading.Lock()
        self._stopped = False
        self._stop_lock = threading.Lock()
        self.children = {}
        self.events = []
        # No lock around this one: the supervisor is not reachable from another thread
        # until its constructor has returned.
        # Sorted so that when two enabled controls claim one actuator, the same one is kept
        # on every machine and every start. Which of the pair wins is arbitrary; that it is
        # the *same* one each time is not, because an operator watching a heater needs the
        # answer to hold still while they fix it.
        claimed: list = []
        for name in sorted(names):
            # Read now rather than at each start: a control that cannot be read is a
            # configuration fault, and finding that out per restart would turn it into a
            # respawn loop that logs the same message for ever.
            try:
                document = _usable_control(name, settings_file)
            except ConfigError as exc:
                # That control's problem, not everybody's. Refusing to supervise anything
                # because one stored document is corrupt would stop the heating over a file
                # nobody is using, and this runs inside the collector's main process.
                logging.error("Control %r cannot be supervised and is being skipped: %r", name, exc)
                continue
            # **The last line of the one-enabled-control-per-actuator rule.** The MCP tools
            # refuse to create or enable a second one and `--check-config` reports a pair, but
            # neither runs when somebody edits two files by hand and restarts - and this is
            # the only place left before two loops start fighting over a heater, each with
            # its own PID and its own safe state, neither able to detect the other.
            if not control_is_enabled(document):
                # **Not started at all, rather than started and gated.** A control process
                # asserts its safe state before its first cycle, and the gate that reads
                # `enabled` does not run until after that - so starting a disabled document
                # commanded its devices off. Harmless for a control nobody else shares an
                # actuator with, and not harmless at all for the case the store deliberately
                # creates: `save_control` stores a document that clashes with a running
                # control *disabled* rather than refusing it, so a disabled replacement
                # naming a live control's heater switched that heater off on arrival.
                #
                # Nothing is lost by leaving it alone. Enabling it asks for a reload, and the
                # reload path already takes on a control that was not a child before.
                logging.info("Control %r is not enabled, so no process is started for it", name)
                continue
            # Pairwise rather than a set intersection: an absent instance means "the
            # first configured target", so it is ambiguous against an explicit one rather
            # than distinct from it, and a set would have let the two spellings past.
            mine = sorted(actuators_owned(document), key=repr)
            clash = next(
                ((who, held, ours) for ours in mine for who, held in claimed if actuators_may_be_one(ours, held)),
                None,
            )
            if clash is not None:
                owner, _held, actuator = clash
                logging.error(
                    "Control %r is not being started: %r is already enabled and commands %r - "
                    "two enabled controls must not share an actuator, so disable one",
                    name,
                    owner,
                    actuator[2],
                )
                continue
            claimed.extend((name, identity) for identity in mine)
            self.children[name] = Child(name=name, document=document, stall_seconds=stall_seconds(document))

    def _default_argv(self, name):
        """Return the argv that starts one control through the installed console script.

        Looked for **next to the running interpreter** before the PATH, because on the
        packaged install it is not on the PATH at all: the script lives in
        ``/opt/send-to-influx/venv/bin`` and only ``send-to-influx-set-credential`` is
        symlinked into ``/usr/sbin``, while the unit sets no ``Environment=PATH``. Resolving
        by name alone would mean no control ever started on a packaged install, and every
        test here passes because a checkout has it on the PATH.

        The interpreter's own directory is the right place to look in every case this runs
        in - a packaged venv, a development venv, a source checkout - because that is where
        the console script for *this* interpreter is installed.

        Args:
            name (str): the control to start

        Returns:
            list: the command to run
        """
        beside_interpreter = os.path.join(os.path.dirname(sys.executable), "send-to-influx")
        command = beside_interpreter if os.path.isfile(beside_interpreter) else "send-to-influx"
        argv = [command, "--control", name]
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
        # Before the spawn, because the child reads the same file for itself and the two
        # must agree from the first beat.
        self._refresh(child)
        try:
            read_fd, write_fd = os.pipe()
        except OSError as exc:
            # Out of descriptors, most likely. Translated rather than left raw because the
            # restart path handles this project's own type: an OSError from here would go
            # straight past it and end the loop, which is one control's exhaustion becoming
            # every control's outage.
            raise ConfigError(f"could not make a heartbeat pipe for control {name!r}: {exc}") from exc
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

    def _refresh(self, child) -> None:
        """Re-read the document a control is about to be started from.

        The child reads its own document at startup, so anything the parent derived from an
        older copy describes a process that no longer exists. The stall window is the one
        that bites: a control whose ``cycle_seconds`` has been lengthened would be killed
        for silence it is entitled to, on a threshold computed from the document before the
        edit. A reload refreshes this too, but a control can be edited by hand and then die
        on its own, and the restart after that goes through here and nowhere else.

        A document that will not read keeps the previous copy rather than refusing to start.
        The child reads the same file and will fail on it in its own process, where it is
        one control's failure and the restart path already handles it; raising here would
        put it on the path ``start_all`` takes, which runs before the loop that would clean
        up after it.

        There may be no previous copy to keep. A control taken on by a reload is built with
        none, and its document was readable a moment earlier when the reload decided to
        start it - so a failure here means the file changed again in between. The two cases
        are said differently because they are differently bad: one leaves the parent an
        older description of the control's devices, and the other leaves it none at all.

        Args:
            child (Child): the control about to be started
        """
        try:
            document = _usable_control(child.name, self.settings_file)
            window = stall_seconds(document)
        except ConfigError as exc:
            logging.warning(
                "Control %r could not be re-read before starting it, so %s: %r",
                child.name,
                (
                    "the parent is still working from the document it last read"
                    if child.document is not None
                    else "the parent has no description of this control at all and cannot make its devices safe"
                ),
                exc,
            )
            return
        child.document = document
        child.stall_seconds = window

    def start_all(self) -> None:
        """Start every control that is not already running.

        **One that cannot be started does not take the others with it.** ``start`` raises
        ``ConfigError`` where a pipe or a spawn fails, and this runs before ``run``'s own try
        block - so a failure on the third control propagated out of the supervisor thread and
        killed it, leaving the first two spawned, actuating, and watched by nothing: no
        heartbeat read, no restart, no safe state on death. The collector carried on, so from
        the outside it looked like a working install.

        Handled the same way :meth:`_restart` already handles it, which is where the rule was
        already written down: the failure is counted against that control, it waits out a
        backoff, and the rest start normally.
        """
        for child in list(self.children.values()):
            if not child.running:
                self._restart(child)

    def poll(self, timeout=0.5):
        """Take one pass: reload what changed, read what arrived, notice deaths, restart.

        Args:
            timeout (float): how long to wait for a pipe to become readable

        Returns:
            list: the events this pass produced
        """
        self.events = []
        # First, so that a control stopped by a reload has already left the selector before
        # this pass waits on it, and so that the one it was replaced by is registered in
        # time to be read.
        self._apply_reload_requests()
        for key, _mask in self._selector.select(timeout=timeout):
            self._read(key.data)
        now = self._clock()
        for child in list(self.children.values()):
            if child.running and now - child.last_beat > child.stall_seconds:
                self._kill(child, f"no heartbeat for {now - child.last_beat:.0f}s")
            elif not child.running and child.restart_at is not None and now >= child.restart_at:
                self._restart(child)
        return self.events

    def status(self):
        """Return what is being supervised right now, safe to read from another thread.

        The MCP server runs in a thread of this process and answers "what is this install
        controlling, and is it running" from here.

        Returns:
            tuple: one :class:`ControlStatus` per supervised control, in name order
        """
        now = self._clock()
        with self._children_lock:
            children = list(self.children.values())
        return tuple(_snapshot(child, now) for child in sorted(children, key=lambda one: one.name))

    def request_reload(self, name) -> None:
        """Ask for a control to be reconciled with its stored document.

        Called from whichever thread wrote the document, which is not this one: the MCP
        server runs in a thread of the collector and the supervisor loop in another. So this
        queues a name and nothing else. Every touch of a child, a process or the selector
        happens on the supervisor's own thread in :meth:`poll`, because registering a
        descriptor while another thread is blocked in ``select`` is not safe.

        The request says only that the document changed. What that means is worked out when
        it is drained, from the document itself.

        Args:
            name (str): the control whose document changed
        """
        if self._stopped:
            # Shutting down. Starting a control here would leave a process behind with
            # nothing watching it, which is the one outcome worse than ignoring the request.
            logging.debug("Ignoring a reload of control %r: the supervisor is stopping", name)
            return
        self._reload_requests.put(name)

    def _apply_reload_requests(self) -> None:
        """Act on everything queued since the last pass, once per control.

        De-duplicated deliberately. A client saving three edits in a row would otherwise
        stop and start a heater three times, and the document on disk now is the only one
        any of those requests was asking for.
        """
        names: dict = {}
        while True:
            try:
                # A dict rather than a set: acting in the order the requests arrived makes
                # the journal read the way it happened.
                names[self._reload_requests.get_nowait()] = None
            except queue.Empty:
                break
        for name in names:
            self._reload(name)

    def _reload(self, name) -> None:
        """Make one control's running state match its stored document.

        The document on disk decides which of three things this is. Gone means the control
        has been deleted: stop it, make its devices safe, forget it. Unreadable or invalid
        means change nothing, because killing a control that is holding a room at
        temperature over a file somebody is halfway through editing is the worst of the
        three outcomes, and the next reload gets another go. Anything else is a restart.

        Args:
            name (str): the control to reconcile
        """
        child = self.children.get(name)
        try:
            path = control_path(name, self.settings_file)
        except ConfigError as exc:
            # The name itself is unusable, so there is no file to look for.
            logging.error("Control %r cannot be reloaded: %r", name, exc)
            self._record("reload-failed", name, repr(exc))
            return
        if not os.path.exists(path):
            self._drop(child, name)
            return
        try:
            # Read here both to decide whether to stop what is running and to see whether it
            # is still enabled. An unusable document must not cost a working control its
            # process.
            document = _usable_control(name, self.settings_file)
        except ConfigError as exc:
            # Two different situations, and telling an operator the wrong one sends them
            # looking in the wrong place. Where a control *is* running, refusing to kill it
            # over a half-written file is the design working, so this is a WARNING: nothing
            # failed, but the edit did not take effect. Where nothing is running - the
            # document was already invalid when the supervisor started, and this reload is
            # the attempt to fix it - saying it "carries on with the document it started
            # with" describes a process that does not exist, and the control is still not
            # running, which is an ERROR.
            if child is not None and child.running:
                logging.warning(
                    "Control %r was not reloaded and is still running the document it started with: %r", name, exc
                )
            else:
                logging.error("Control %r is still not running: the stored document is not valid: %r", name, exc)
            self._record("reload-failed", name, repr(exc))
            return
        if not control_is_enabled(document):
            # The other half of not starting disabled documents. A control disabled while it
            # is running must let go of its devices - that is what disabling one means - and
            # `_stop` makes them safe on the way out. One that was already stopped has
            # nothing to do, and must not be started to be stopped again.
            self._disable(child, name)
            return
        known = child is not None
        if not known:
            # A control that did not exist when the supervisor started. Taking it on here is
            # what lets a newly stored control run without restarting the collector.
            child = Child(name=name)
            with self._children_lock:
                self.children[name] = child
        elif child.running:
            self._stop(child, "its document changed")
        # The snapshot itself is refreshed by start(), which is the only place a process is
        # created and so the only place it can be made to agree with one.
        #
        # Not a failure, so the new document does not wait out a backoff earned by the old
        # one - and an edit is the usual way a failing control gets fixed, so its history
        # starts again here.
        child.failures = 0
        child.restart_at = self._clock()
        self._record("reloaded", name, "its document changed" if known else "it is newly stored")
        self._restart(child)

    def _disable(self, child, name) -> None:
        """Stop supervising a control whose document is no longer enabled.

        Distinct from :meth:`_drop`, which is for a document that has been deleted: this one
        still exists and may be enabled again, so the name stays in the store and only the
        process goes.

        Args:
            child (Child or None): the control, where one was running
            name (str): its name, for the log line and the event
        """
        if child is None:
            self._record("disabled", name, "it was not being supervised")
            return
        if child.running:
            # Makes the devices safe on the way out, which is the point: a heater held on by
            # a control that has just been disabled must not stay on.
            self._stop(child, "its document is no longer enabled")
        else:
            self.make_safe(name)
        with self._children_lock:
            del self.children[name]
        logging.info("Control %r is no longer enabled, so it is not being run", name)
        self._record("disabled", name, "its document is no longer enabled")

    def _drop(self, child, name) -> None:
        """Stop supervising a control whose document has been deleted.

        Args:
            child (Child or None): the control, where there was one
            name (str): its name, for the log line and the event
        """
        if child is None:
            # Nothing was supervising it, so there is nothing to stop. Recorded anyway, so a
            # caller waiting on the event does not wait out its timeout because the delete
            # arrived twice.
            self._record("dropped", name, "it was not being supervised")
            return
        if child.running:
            self._stop(child, "its document has been deleted")
        else:
            self.make_safe(name)
        with self._children_lock:
            del self.children[name]
        logging.info("Control %r has been deleted, so it is no longer supervised", name)
        self._record("dropped", name, "its document has been deleted")

    def _stop(self, child, reason) -> None:
        """End a control the parent meant to end, and do not hold it against it.

        The same signal a stalled control gets and the same teardown, with none of the
        bookkeeping: no failure counted, no backoff, and an INFO line rather than an ERROR.
        A control that exited because it was asked to did nothing wrong, and treating it as
        a death would make the new document wait out a growing delay while the journal said
        a working control keeps failing.

        Args:
            child (Child): the control to stop
            reason (str): why, for the log line
        """
        self._terminate(child)
        pid, status = self._release(child)
        logging.info(
            "Control %r (pid %s) was stopped because %s, and %s",
            child.name,
            pid,
            reason,
            f"was killed by signal {-status}" if status < 0 else f"exited {status}",
        )
        self.make_safe(child.name)

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
            self._record("start-failed", child.name, repr(exc))

    def run(self, stop, poll_seconds=DEFAULT_POLL_SECONDS) -> None:
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
        self._terminate(child)
        self._reap(child, reason, kind="stalled")

    def _terminate(self, child) -> None:
        """End a child's process, escalating if it will not go.

        Args:
            child (Child): the control whose process to end
        """
        child.process.terminate()
        try:
            child.process.wait(timeout=KILL_GRACE_SECONDS)
        except TimeoutExpired:
            # It ignored SIGTERM, so it does not get a say. A control that will not stop is
            # worse than one that is killed: its devices are energised and nobody is
            # deciding what they should be doing.
            child.process.kill()
            child.process.wait()

    def _release(self, child):
        """Let go of the parent's side of a child that has ended.

        Args:
            child (Child): the control that has gone

        Returns:
            tuple: the pid it had, and the status it ended with
        """
        status = child.process.poll()
        if status is None:
            status = child.process.wait()
        self._selector.unregister(child.beats)
        os.close(child.beats)
        child.beats = None
        pid = child.process.pid
        child.process = None
        return pid, status

    def _reap(self, child, reason, kind="died") -> None:
        """Record a control's death, make its devices safe, and schedule a restart.

        Args:
            child (Child): the child that has gone
            reason (str): what happened, for the log line
            kind (str): the event kind to record
        """
        pid, status = self._release(child)
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
        had dealt with it. It runs after every exit for the same reason, deliberate or not:
        a control stopped to pick up a changed document has exited exactly as thoroughly as
        one that crashed.

        Args:
            name (str): the control whose devices to make safe
        """
        for document in self._documents_for(name):
            try:
                commands = commands_for(document.get("safe_state", "unenergised"), tuple(document.get("devices") or {}))
                if commands is None:
                    # leave_unchanged, which is an answer rather than an omission.
                    continue
                # No log passed, so one is opened for this control and closed again. The
                # child owning the other one is already dead by the time this runs - that is
                # what "make its devices safe" is for - so the two never write at once.
                command_devices(name, document, commands, self.settings_file, forced=True)
            except (ConfigError, SourceConnectionError) as exc:
                # Logged rather than raised: the supervisor's job is to keep going, and a
                # bridge that cannot be reached now is one the next restart will try again.
                logging.error("Could not make control %r safe: %r", name, exc)
            except Exception as exc:
                # The one deliberately broad catch in this subsystem, and it is what makes
                # "one control's failure is never every control's" true by construction
                # rather than by having fixed each specific case.
                #
                # This runs on the supervisor's own thread, inside its poll loop, and it
                # calls into a per-source handler and whatever library that handler uses.
                # Anything those raise that is not one of the two types above - an
                # AttributeError from a capability that turned out not to be there, a
                # vendor client's own exception class - escapes make_safe, then _reap, then
                # poll, and ends the thread. Every other control is then running with
                # nothing watching it: no heartbeat, no restart, no safe state on death.
                # That is a far worse outcome than one control's devices staying as they
                # are, which is what tolerating this costs.
                #
                # Not a substitute for the specific fixes: the case that prompted it is
                # refused at validation now. It is the floor under them.
                #
                # `exception` rather than `error`, so the traceback comes with it. This branch
                # exists for a failure nobody predicted, from a library this project does not
                # control, on a thread that must not die - without the stack the log says
                # something unexpected happened inside make_safe and nothing about where.
                #
                # Safe to carry a traceback because IndentedFormatter indents every line after
                # the first, so nothing inside one can begin like a log entry. Review raised
                # this as log forging and was right: a traceback's last line is the exception's
                # message at column zero, which %r elsewhere cannot reach. Fixed in the
                # formatter rather than here, because the exposure was never specific to this
                # call site.
                logging.exception("Could not make control %r safe, and the reason was unexpected: %r", name, exc)

    def _documents_for(self, name):
        """Return every document worth making this control's devices safe against.

        Two of them, where they differ. The document the process was started from names the
        devices *it* could have energised; the one on disk now names the devices the next
        process will own. They differ exactly when somebody has edited the control, which is
        the case this has to get right: reading the file alone strands a device that has
        just been removed from the document, and trusting the started-from copy alone misses
        one that has just been added. Where they are the same document, which is almost
        always, this returns one - commanding twice would be harmless but would also make
        every log of what the bridge was asked to do twice as long as what happened.

        Args:
            name (str): the control to describe

        Returns:
            list: the documents to act on, which is empty only where there is no description
            of this control left anywhere
        """
        documents = []
        child = self.children.get(name)
        if child is not None and child.document is not None:
            documents.append(child.document)
        try:
            path = control_path(name, self.settings_file)
            stored = os.path.exists(path)
        except ConfigError as exc:
            logging.error("Could not read control %r to make its devices safe: %r", name, exc)
            return documents
        if not stored:
            # Deleted, which is something an operator does on purpose rather than a fault to
            # report. The copy the process was started with is the only description of its
            # devices left anywhere, and is exactly what it is kept for.
            if not documents:
                logging.error(
                    "Control %r is not stored at %r and was not started from here, so there is nothing "
                    "left that says which devices it owns",
                    name,
                    path,
                )
            return documents
        try:
            current = _usable_control(name, self.settings_file)
        except ConfigError as exc:
            # The file is there and cannot be used, which is a fault. Said rather than
            # passed over: nothing is stranded, because the copy this process was started
            # with still names its devices, but the file an operator would go and look at is
            # not the one that just acted - and the next reload will refuse for the same
            # reason.
            level = logging.WARNING if documents else logging.ERROR
            logging.log(
                level,
                "Control %r could not be read, so its devices were made safe from %s: %r",
                name,
                "the document it was started with" if documents else "nothing at all",
                exc,
            )
            return documents
        if current not in documents:
            documents.append(current)
        return documents

    def stop_all(self) -> None:
        """Stop every running control and leave its devices safe, once.

        Once, explicitly. Two callers reach this in the ordinary shutdown of the collector -
        the supervisor's own loop when SHUTDOWN is set, and the atexit handler that covers a
        signal exiting through sys.exit - and a second pass closes an already-closed
        selector. That happens to be harmless on both selector implementations this runs on,
        which is not the same as being safe: it was idempotent by accident of somebody
        else's internals, while a comment at the call site asserted it as a property.
        """
        # Under a lock, not a bare check-then-set. Two threads can reach here: the
        # supervisor's own `finally` and the collector's exit handler. The handler joins the
        # thread first, so ordinarily only one arrives - but that join is bounded, and on a
        # wedged supervisor both would pass an unguarded `if not self._stopped` before either
        # set it, and then walk the same children terminating and releasing each one twice.
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
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
            self._release(child)
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
