"""What a control had done when it last ran: its devices, and its loop's own memory.

Named for the transitions because that is the bulk of it and the part with a rule attached.
The PID's integral lives in the same file for the same reason the device states do - it is
what this control knew, it is worthless to anybody else, and it must not outlive the document
that produced it.



``min_transition_seconds`` is a promise to the hardware: do not switch this more often
than that. It used to be kept only *inside* a cycle window, because the planner is a pure
function of one window and had nothing to remember a previous one by - so a minimum longer
than the window could not be honoured at all, and was refused by validation. That coupled
two settings that have nothing to do with each other: ``cycle_seconds`` is how often the
loop recomputes, and the minimum is a property of a relay or a compressor. Protecting a
slow device meant slowing the whole loop down.

This module is the memory that decouples them. One file per control, holding each device's
last commanded state and the moment it was commanded, so the planner can be told which
devices may not move yet and keep them where they are.

**Epoch seconds, on disk, rather than a monotonic clock in memory.** A control process is
restarted by the supervisor on every document reload as well as on failure, and a restart
asserting the safe state does not necessarily change anything - commanding ``unenergised``
on a heater that is already off is not a transition, so nothing would be recorded and an
in-memory clock would come back empty. A heater switched off a second before a reload could
then be switched on again immediately, which is precisely the thing the setting exists to
prevent.

Epoch seconds carry no time zone, so that part is free, but the wall clock can still step -
an NTP correction or somebody running ``date``. A backwards step would make the interval
negative and read as "no time has passed at all", freezing a device until the clock caught
up. A negative interval is therefore treated as zero elapsed, which errs towards holding the
device rather than switching it.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import contextlib
import json
import logging
import os
import stat
import tempfile
import time

from toinflux.controls import require_valid_control_name
from toinflux.exceptions import ConfigError
from toinflux.general import resolve_state_dir

#: Where the logs live. Beside the control documents rather than among them: ``list_controls``
#: filters by suffix so a stray file there would be ignored, but a directory holding one kind
#: of thing is easier to reason about than one holding two, and these are written every cycle
#: while the documents are written by hand.
TRANSITION_DIR_NAME = "transitions"

#: Nothing here brings a transition forward, deliberately.
#:
#: An earlier version allowed one to happen up to half a window early, so that a minimum
#: falling just after a window boundary was not rounded up to the next one. It was wrong in
#: both directions. It released devices early - measured at 899 seconds against a 900-second
#: minimum, and at 100 against 120 - which is the one direction that breaks the promise the
#: setting makes to the hardware. And it was unnecessary, because the freeze only ever decides
#: anything when the minimum is longer than the window: below that, `plan_window` already
#: keeps the spacing on its own, by requiring both dwells to be at least the minimum, which
#: also spaces the change at a window boundary from the one before it.
#:
#: So the error is now always in the safe direction and is bounded by one window, which is by
#: definition shorter than the minimum wherever this code decides anything at all.


def transition_dir(settings_file=None):
    """Return the directory transition logs are stored in.

    Args:
        settings_file (str or None): the settings path the process was started with

    Returns:
        str: the directory holding one log per control
    """
    return os.path.join(resolve_state_dir(settings_file), TRANSITION_DIR_NAME)


def transition_path(name, settings_file=None):
    """Return the path of one control's transition log.

    Args:
        name (str): the control's name
        settings_file (str or None): the settings path the process was started with

    Returns:
        str: the file the log is kept in

    Raises:
        ConfigError: the name is not one the store permits
    """
    require_valid_control_name(name)
    return os.path.join(transition_dir(settings_file), f"{name}.json")


class TransitionLog:
    """One control's record of when each device last changed, and what it was set to.

    Carries ``entries`` (device name to ``{"state": bool, "at": epoch seconds}``).

    Read once when the control starts and rewritten whenever a device actually changes.
    Every device command in this project goes through one function, so there is one place
    that writes this and it cannot be bypassed by a path somebody adds later.
    """

    def __init__(self, name, settings_file=None, clock=time.time):
        """Load a control's log, or start an empty one.

        Args:
            name (str): the control's name
            settings_file (str or None): the settings path the process was started with
            clock (callable): returns epoch seconds, injectable so a test is not a wait
        """
        self.name = name
        self.settings_file = settings_file
        self._clock = clock
        self.path = transition_path(name, settings_file)
        self.entries = self._read()
        self._pid = self._read_loop()

    def _read(self):
        """Return the stored entries, or an empty mapping.

        A log that cannot be read is not a reason to refuse to run: it is a cache of when
        things happened, and the worst an empty one costs is one transition sooner than the
        operator asked for. Refusing to start the control over it would trade a minor
        imprecision for no heating at all, which is the wrong way round. It is logged,
        because silently losing the record is how a device ends up being switched more often
        than its document says and nobody knows why.

        Returns:
            dict: device name to its last state and moment
        """
        try:
            with open(self.path, encoding="utf-8") as handle:
                # Parsed raw and checked here, before it is split into sections: the splitter
                # answers "which half is which" and turns anything unusable into two empty
                # ones, so a file holding a list would otherwise read as simply having nothing
                # in it rather than as the broken file it is.
                stored = json.load(handle)
        except FileNotFoundError:
            # Nothing has been commanded yet, which is the ordinary state of a control that
            # has never run. Not worth a line.
            return {}
        except (OSError, ValueError) as exc:
            logging.warning(
                "Control %r could not read its transition log at %r, so every device may change "
                "once sooner than its minimum asks: %r",
                self.name,
                self.path,
                exc,
            )
            return {}
        if not isinstance(stored, dict):
            logging.warning(
                "Control %r found a transition log at %r that is not a mapping, so it is being ignored",
                self.name,
                self.path,
            )
            return {}
        stored = self._sections(stored)["devices"]
        return {
            device: entry
            for device, entry in stored.items()
            if isinstance(device, str) and isinstance(entry, dict) and isinstance(entry.get("at"), (int, float))
        }

    def _read_loop(self):
        """Return the stored loop state, or an empty mapping.

        Read alongside the device entries rather than on demand: the file is opened once at
        construction, and a second read would let the two halves disagree about which version
        of the file they came from.

        Returns:
            dict: the loop's stored state, empty where there is none
        """
        try:
            with open(self.path, encoding="utf-8") as handle:
                return self._sections(json.load(handle))["pid"]
        except (OSError, ValueError):
            # Whatever went wrong has already been reported by the device half's own read,
            # which runs first and says so once. Saying it twice would describe one unreadable
            # file as two faults.
            return {}

    @staticmethod
    def _sections(stored):
        """Return a stored document as its two halves, reading the older flat shape too.

        The file held a bare mapping of device to entry before the loop's own state joined
        it. An installation upgrading in place has one of those on disk, and refusing to read
        it would cost every device one transition sooner than its minimum asks - a silly
        price for a format change nobody asked about.

        Args:
            stored (object): whatever was parsed out of the file

        Returns:
            dict: ``{"devices": mapping, "pid": mapping}``, either possibly empty
        """
        if not isinstance(stored, dict):
            return {"devices": {}, "pid": {}}
        if "devices" not in stored and "pid" not in stored:
            return {"devices": stored, "pid": {}}
        devices = stored.get("devices")
        loop = stored.get("pid")
        return {
            "devices": devices if isinstance(devices, dict) else {},
            "pid": loop if isinstance(loop, dict) else {},
        }

    @property
    def loop(self):
        """Return the loop's stored memory as written, without judging it.

        :meth:`loop_state` answers "may this be resumed from", which is the question the
        control asks. This answers "what is written down", which is the question somebody
        diagnosing it asks - including when the answer is that it no longer matches the
        document, because that is the finding rather than a reason to withhold it.

        Returns:
            dict: the stored state, empty where there is none
        """
        return dict(self._pid)

    def loop_state(self, fingerprint, max_age, now=None):
        """Return the PID state worth resuming from, or None to start afresh.

        **Two guards, both erring towards starting afresh**, because the failure they prevent
        is a control commanding output on the strength of something that is no longer true,
        and the cost of being wrong the other way is only the settling time it already has
        today.

        The fingerprint is the stricter of the two. An integral is in the output's units, so
        it means one thing under one set of gains and ladder and something else under
        another - and the commonest restart there is happens to be the one that changes them,
        because the supervisor restarts a control whenever its document is edited.

        Age covers the rest: a process that has been down long enough for the room to move on
        should look at the room rather than at what it remembered.

        Args:
            fingerprint (str): what the current document's tuning and ladder hash to
            max_age (float): how old the state may be and still describe the present
            now (float or None): epoch seconds; read from the clock when None

        Returns:
            dict or None: the state to resume from, or None
        """
        state = self._pid
        if not state or state.get("fingerprint") != fingerprint:
            return None
        at = state.get("at")
        if not isinstance(at, (int, float)):
            return None
        moment = self._clock() if now is None else now
        # Clamped like every other age here: a wall clock that stepped backwards must not
        # make a stale state look fresh.
        if not 0 <= float(moment) - float(at) <= float(max_age):
            return None
        return state

    def loop_age(self, now=None):
        """Return how long ago the loop state was written, or None where there is none.

        Separate from :meth:`loop_state` because the controller needs the age in seconds and
        must not read the timestamp itself: this log is written with epoch time so that it
        survives a restart, while a hold is measured with a monotonic clock so that it is not
        confused by one. Handing over the elapsed seconds is the only safe traffic between
        the two.

        Args:
            now (float or None): epoch seconds; read from the clock when None

        Returns:
            float or None: seconds since it was written, never negative, or None
        """
        at = (self._pid or {}).get("at")
        if not isinstance(at, (int, float)):
            return None
        # Clamped, matching `elapsed`: a clock that moved backwards makes a negative age,
        # which would otherwise read as state from the future.
        return max(0.0, float(self._clock() if now is None else now) - float(at))

    def record_loop(self, state, fingerprint, now=None) -> None:
        """Note the loop's own memory, so a restart does not rebuild it from nothing.

        Args:
            state (dict): what the controller wants back, already plain numbers
            fingerprint (str): what the current document's tuning and ladder hash to
            now (float or None): epoch seconds; read from the clock when None
        """
        self._pid = {
            **state,
            "fingerprint": fingerprint,
            "at": float(self._clock() if now is None else now),
        }
        self._write()

    def states(self):
        """Return what each device was last commanded to.

        The value as commanded, not coerced to a boolean: a switched device holds true or
        false and a driven one holds a number, and flattening the second into the first would
        make a lamp at 40% indistinguishable from the same lamp at 5%.

        Returns:
            dict: device name to the state it was last set to
        """
        return {device: entry.get("state") for device, entry in self.entries.items()}

    def parameters(self):
        """Return the parameter each device was driven by when it was last commanded.

        None for a device that was switched, and None for one recorded before this was kept -
        which reads the same way and is handled the same way, because "not the parameter it is
        driven by now" is the only question anybody asks of it.

        Returns:
            dict: device name to its parameter, or None
        """
        return {device: entry.get("parameter") for device, entry in self.entries.items()}

    def elapsed(self, device, now=None):
        """Return how long since a device last changed, or None where it never has.

        Args:
            device (str): the device name
            now (float or None): epoch seconds; read from the clock when None

        Returns:
            float or None: seconds, never negative, or None for a device with no record
        """
        entry = self.entries.get(device)
        if entry is None:
            return None
        moment = self._clock() if now is None else now
        # Clamped rather than returned as measured: a wall clock that stepped backwards would
        # otherwise read as a negative age, which passes no comparison and would hold the
        # device frozen until real time caught up with the jump.
        return max(0.0, float(moment) - float(entry["at"]))

    def released(self, device):
        """Whether a device's last move was one the minimum does not govern.

        A safe state overrides the minimum in both directions: it is applied whatever the
        clock says, and the control is not then made to wait out a full minimum before it
        may act again. Recording it as an ordinary transition would enforce the second half
        anyway - a heater forced off by a transient fault would sit there for the whole
        minimum after the fault cleared, which is the setting protecting the hardware from
        the safety mechanism.

        So the state is recorded, because the planner needs to know where the devices
        actually are, and the timing is marked as not binding.

        Args:
            device (str): the device name

        Returns:
            bool: True where the last command was a safe state
        """
        return bool((self.entries.get(device) or {}).get("forced"))

    def frozen(self, min_transition_for, devices, now=None):
        """Return the devices that may not change state yet.

        A device is free once its minimum has actually elapsed, and not a moment before.
        The question is asked once per window, so a minimum that expires part way through
        one is honoured at the next boundary rather than in the middle - late rather than
        early, which is the direction that keeps the promise rather than breaking it.

        Args:
            min_transition_for (callable): device name -> its minimum in seconds
            devices (iterable): the control's device names
            now (float or None): epoch seconds; read from the clock when None

        Returns:
            frozenset: the device names that must keep the state they are in
        """
        moment = self._clock() if now is None else now
        held = set()
        for device in devices:
            elapsed = self.elapsed(device, moment)
            if elapsed is None:
                # Never commanded, so there is nothing it is too soon after.
                continue
            if self.released(device):
                # Last moved by a safe state, which the minimum does not govern in either
                # direction. The next ordinary command restores the normal rule.
                continue
            if elapsed < float(min_transition_for(device)):
                held.add(device)
        return frozenset(held)

    def record(self, commands, now=None, forced=False, parameters=None) -> None:
        """Note the devices whose state this command actually changes.

        Only the ones that change: commanding a heater off when it is already off is not a
        transition, and restarting its clock would make the minimum mean "this long since
        anybody mentioned it" rather than "this long since it moved". That is the same rule
        the planner already applies when deciding which devices a rung change concerns.

        Args:
            commands (dict): device name to the state it has just been set to
            now (float or None): epoch seconds; read from the clock when None
            forced (bool): True where this is a safe state rather than a control decision,
                which the minimum governs in neither direction - see :meth:`released`
            parameters (dict or None): device name to the parameter it is driven by, or None
                for one that is switched. Kept with the state because a number means nothing
                without the scale it is on - 2700 is a colour temperature, 40 is a percentage
        """
        parameters = parameters or {}
        moment = float(self._clock() if now is None else now)
        changed = False
        for device, state in commands.items():
            entry = self.entries.get(device)
            # Compared as commanded rather than as booleans, so a dimmer moving from 40% to
            # 5% is a move. Under the old comparison both were true and nothing was timed,
            # which would have made `min_transition_seconds` mean nothing at all for the one
            # kind of device whose whole job is to change by degrees.
            if entry is not None and entry.get("state") == state:
                # No move, so nothing to time. The mark still has to go when an ordinary
                # command confirms a state a safe state put the device in, or the exemption
                # would outlive the safe state that earned it.
                if entry.get("forced") and not forced:
                    del entry["forced"]
                    changed = True
                continue
            self.entries[device] = {"state": state, "at": moment, "parameter": parameters.get(device)}
            if forced:
                self.entries[device]["forced"] = True
            changed = True
        if changed:
            self._write()

    def _write(self) -> None:
        """Replace the stored log, atomically.

        Written by rename so a process killed mid-write leaves the previous log rather than
        half of this one - a truncated file would parse as nothing and lose every device's
        history at once, which is the moment a heater gets switched twice in quick
        succession. A failure here is logged and not raised: the devices have already been
        commanded, and refusing to carry on because the note about it could not be filed
        would stop a working control over its own bookkeeping.

        Raises:
            BaseException: re-raised after the temporary file is cleaned up, where the
                failure was not an OSError this method is willing to tolerate - a
                KeyboardInterrupt or a MemoryError mid-write belongs to the caller
        """
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, prefix=f".{self.name}.", suffix=".tmp", delete=False
            )
            try:
                with handle:
                    json.dump({"devices": self.entries, "pid": self._pid}, handle)
                os.chmod(handle.name, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(handle.name, self.path)
            except BaseException:
                # The rename did not happen, so this temporary file is ours to clean up and
                # nothing else will. Left behind, a control restarting every few seconds
                # against a full disk would paper the state directory with them.
                with contextlib.suppress(OSError):
                    os.unlink(handle.name)
                raise
        except OSError as exc:
            logging.warning(
                "Control %r could not write its transition log at %r, so a device may change once "
                "sooner than its minimum asks after a restart: %r",
                self.name,
                self.path,
                exc,
            )


def forget_control(name, settings_file=None) -> None:
    """Remove a deleted control's transition log.

    Args:
        name (str): the control that has been deleted
        settings_file (str or None): the settings path the process was started with
    """
    try:
        os.unlink(transition_path(name, settings_file))
    except FileNotFoundError:
        # It never ran, or never changed a device. Not a fault.
        pass
    except (OSError, ConfigError) as exc:
        # The control is already gone; a log left behind costs nothing but a stale file, and
        # a name that reappears later reads it and finds states that no longer describe
        # anything. Worth saying so, not worth failing the delete over.
        logging.warning("Could not remove the transition log for deleted control %r: %r", name, exc)
