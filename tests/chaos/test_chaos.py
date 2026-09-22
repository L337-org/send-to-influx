"""Many controls, seeded random faults, and the same invariants the static scenarios use.

The long seeded run is excluded from the default suite and scheduled instead, because a run
long enough to be worth anything is minutes rather than seconds.

Two things here are *not* excluded, and run with the ordinary suite. Any seed that has failed,
from `SEEDS_THAT_FAILED` - which is how a chaos failure becomes a permanent regression test
instead of an anecdote. And the driver's own guards below, which take seconds and check the
things a scheduled job would be the wrong place to find out about: that a fault never outlives
the run that injected it, and that the story says when something happened.
"""

__author__ = "Gavin Lucas"
__copyright__ = "Copyright (C) 2026 Gavin Lucas"
__license__ = "MIT"

import os
import random
import sys
import warnings

import pytest

from tests.harness import census, invariants
from tests.harness.bridge import plug
from tests.harness.chaos import SEEDS_THAT_FAILED, ChaosDriver
from tests.harness.installation import conservatory
from toinflux.supervision import Supervisor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: How many controls a chaos run drives. More than the static scenarios' two, because the
#: interactions worth finding are between controls sharing one bridge.
CONTROLS = 4


def _document(name, device):
    """Return a control with a short window and one device of its own.

    Args:
        name (str): the control's name
        device (str): the bridge device it owns

    Returns:
        dict: a control document
    """
    document = conservatory(name=name)
    document.pop("active_period", None)
    document["devices"] = {device: {"source": "hue", "device": device}}
    document["output"] = dict(
        document["output"],
        cycle_seconds=1,
        min_transition_seconds=1,
        stages=[{"level": 0, "set": {device: False}}, {"level": 1500, "set": {device: True}}],
    )
    return document


def _build(installation, count):
    """Write `count` controls, each owning its own plug on the shared bridge.

    Args:
        installation (Installation): where to write them
        count (int): how many

    Returns:
        dict: control name to its document
    """
    documents = {}
    for index in range(count):
        name, device = f"control{index}", f"heater{index}"
        installation.bridge.lights[str(90 + index)] = plug(device)
        documents[name] = _document(name, device)
        installation.write_control(documents[name])
    return documents


def _supervisor(installation, names):
    """Return a supervisor that starts controls from this checkout.

    Args:
        installation (Installation): the installation to run against
        names (iterable): the controls to supervise

    Returns:
        Supervisor: not yet started
    """

    def argv_for(name):
        """Return the command that runs one control.

        Args:
            name (str): the control to run

        Returns:
            list: the command
        """
        return [
            sys.executable,
            os.path.join(ROOT, "sendtoinflux.py"),
            "--control",
            name,
            "--settings",
            installation.settings_file,
        ]

    return Supervisor(
        names,
        settings_file=installation.settings_file,
        argv_for=argv_for,
        backoff=lambda failures: 0.1 * failures,
    )


def _chaos_run(installation, seed, ticks):
    """Drive one seeded run and check what must hold at the end of it.

    Args:
        installation (Installation): the installation to run against
        seed (int): the seed for every choice
        ticks (int): how many ticks to drive

    Raises:
        AssertionError: an invariant was violated, with the seed and the story
    """
    documents = _build(installation, CONTROLS)
    supervisor = _supervisor(installation, documents)
    driver = ChaosDriver(seed, supervisor, installation.bridge, installation.influx)
    try:
        supervisor.start_all()
        # The baseline is taken once every control is up and has talked to both far ends,
        # not before. A census of the system before it has done anything counts none of the
        # connection threads a working install has at rest, so every run would "leak" the
        # cost of starting - which is growth the invariant was never meant to report.
        for _ in range(5):
            supervisor.poll(timeout=0.2)
        before = census.take(os.getpid())
        for number in range(1, ticks + 1):
            driver.tick(number, documents)
        # Cleared and settled before judging what is left: a control cannot make its devices
        # safe through a bridge that is refusing connections, so asserting that mid-fault
        # would be asserting that an outage does not happen.
        driver.settle()
        supervisor.stop_all()
        reports = [
            invariants.devices_unenergised(
                installation.bridge,
                # The bridge's names, not the documents' keys for them. The two need not
                # agree, and passing keys used to check nothing at all rather than failing.
                [spec["device"] for doc in documents.values() for spec in doc["devices"].values()],
            ),
            invariants.nothing_leaked(before, census.quiet_after(before, os.getpid())),
            # Asserted outright rather than against the baseline. The baseline is taken with
            # every control up, so a run that leaked one child ends *below* it and the growth
            # check passes - blind to the one thing the census is here for.
            invariants.no_control_processes_left(os.getpid()),
        ]
        broken = [f"{report.name}: {v}" for report in reports for v in report.violations]
        assert not broken, (
            f"chaos run with seed {seed} left the system wrong:\n  "
            + "\n  ".join(broken)
            + f"\n\nWhat led to it:\n  {driver.run.story()}\n\n"
            f"Add {seed} to SEEDS_THAT_FAILED so this runs from now on."
        )
        for report in reports:
            for reason in report.skipped:
                # Warned rather than swallowed, the same as invariants.check does: a check
                # that was not made reads exactly like a check that passed, and a scheduled
                # job nobody watches is the worst place for that difference to be invisible.
                warnings.warn(f"chaos run with seed {seed} skipped a check: {reason}", stacklevel=2)
    finally:
        # Before stop_all, and whatever went wrong. A tick that raises leaves the fault it
        # injected still entered: the next scenario then fails for a reason that has nothing
        # to do with it, and - worse here - stop_all cannot make the devices safe through a
        # bridge this run is still holding unreachable. The harness's own rule, which the
        # driver was breaking.
        driver.clear_fault()
        supervisor.stop_all()


@pytest.mark.chaos
def test_a_long_seeded_run(state_directory):
    """One long run from a fresh seed, printed so a failure can be repeated.

    The seed comes from the environment where CI passes one, so a scheduled job can be told
    to re-run yesterday's, and is otherwise random - a chaos run that always chose the same
    seed would only ever find the same thing.
    """
    seed = int(os.environ.get("CHAOS_SEED") or random.randrange(2**32))
    print(f"chaos seed: {seed}")
    _chaos_run(state_directory, seed, ticks=int(os.environ.get("CHAOS_TICKS") or 60))


@pytest.mark.parametrize("seed", SEEDS_THAT_FAILED)
def test_a_seed_that_failed_before(state_directory, seed):
    """Every seed that has ever failed, on every run of the ordinary suite.

    Short, because the point is the particular sequence rather than endurance. An empty list
    is the honest state before the first failure: nothing has been found yet, and a
    placeholder seed would only assert that the harness starts.
    """
    _chaos_run(state_directory, seed, ticks=25)


class TestAFaultNeverOutlivesTheRun:
    """The harness's own rule, which the driver was breaking. A tick that raises leaves the
    fault it injected still entered: the next scenario then fails for a reason that has
    nothing to do with it, and stop_all cannot make the devices safe through a bridge this
    run is still holding unreachable."""

    class _NoChildren:
        """A supervisor with nothing to kill, so the driver's choices stay to faults."""

        children: dict = {}

        def poll(self, timeout=0.2):
            """Do nothing, as a stand-in.

            Args:
                timeout (float): ignored

            Returns:
                list: no events
            """
            return []

    def _driver_with_a_fault(self, bridge, influx):
        """Return a driver that has entered a fault, and the endpoint it broke.

        Args:
            bridge (StubBridge): the devices' far end
            influx (StubInflux): the readings' far end

        Returns:
            ChaosDriver: with one fault active
        """
        driver = ChaosDriver(1, self._NoChildren(), bridge, influx)
        for tick in range(1, 40):
            driver._choose(tick)
            if driver._active is not None:
                return driver
        raise AssertionError("no seed choice produced a fault to clear")

    def test_clearing_puts_the_endpoints_back(self, bridge, influx):
        driver = self._driver_with_a_fault(bridge, influx)
        driver.clear_fault()
        for endpoint in (bridge, influx):
            assert endpoint.unreachable is False
            assert endpoint.hang_seconds == 0.0
            assert endpoint.status is None
        assert influx.frozen is False

    def test_clearing_twice_is_not_an_error(self, bridge, influx):
        """The unwind path runs it, and so does settle. Neither knows what the other did."""
        driver = self._driver_with_a_fault(bridge, influx)
        driver.clear_fault()
        driver.clear_fault()
        assert driver._active is None

    def test_the_unwind_records_the_clearing_it_did(self, bridge, influx):
        """The end-to-end version of this test cannot fail, and is not here.

        A fault is a `@contextmanager` generator, so when the driver is collected, closing
        the suspended generator runs its `finally` and puts the endpoint back. Measured: with
        the explicit clearing removed, a failed run still ended with every fault off. So
        CPython's refcounting was masking the omission, and an end-to-end assertion about
        endpoint state passes whether or not the unwind does its job.

        The explicit call stays, because correctness resting on when an object happens to be
        collected is not correctness - a reference cycle, or a traceback holding the frame,
        and the fault outlives the run. What is asserted here is the thing only the explicit
        call produces: an entry in the story saying it happened.
        """
        driver = self._driver_with_a_fault(bridge, influx)
        driver.clear_fault()
        assert any("cleared the fault" in tick.action for tick in driver.run.ticks)

    def test_the_story_stamps_when_a_fault_cleared_not_how_many_entries_it_had(self, bridge, influx):
        """Numbering by the length of the record makes tick numbers jump and run backwards,
        which is precisely what a story exists not to do.

        The two are forced apart rather than left to a seed. They coincide whenever the fault
        starts on the tick whose number equals the entry count, which is common enough that
        the first version of this test passed against the bug it was written for: quiet ticks
        are what makes the count lag the number, so the driver is held quiet deliberately.
        """
        driver = ChaosDriver(1, self._NoChildren(), bridge, influx)
        for number in range(1, 25):
            driver.tick(number, {})
            if driver._active is not None:
                break
        assert driver._active is not None, "no fault was injected, so this tested nothing"

        class _NeverChooses:
            """A generator that declines every probability, so nothing more is recorded."""

            @staticmethod
            def random():
                """Return a value above every threshold the driver tests.

                Returns:
                    float: 1.0
                """
                return 1.0

        driver.random = _NeverChooses()
        entries_before = len(driver.run.ticks)
        for number in range(driver._tick + 1, driver._tick + 9):
            driver.tick(number, {})
        assert len(driver.run.ticks) == entries_before, "the quiet ticks were not quiet"
        assert driver._tick > len(driver.run.ticks), "the count and the number did not diverge"

        driver.clear_fault()
        cleared = [tick for tick in driver.run.ticks if "cleared" in tick.action]
        assert cleared[-1].number == driver._tick
        assert [tick.number for tick in driver.run.ticks] == sorted(t.number for t in driver.run.ticks)

    def test_a_tick_that_changes_nothing_is_not_recorded(self, bridge, influx):
        """`story` is one line per change, and "nothing happened" is not one. An entry for it
        would contradict the format and make a three-hundred-tick run longer without making
        it more reproducible - the closing note already explains a gap."""
        driver = ChaosDriver(1, self._NoChildren(), bridge, influx)
        driver.random = type(
            "_AlwaysNothing",
            (),
            {
                "random": staticmethod(lambda: 1.0),
                "choice": staticmethod(lambda options: next(o for o in options if o[1] is None)),
            },
        )()
        for number in range(1, 6):
            driver.tick(number, {})
        assert driver.run.ticks == [], driver.run.story()
        assert driver.run.story() == "nothing was injected before it failed"


class TestWhatTheDriverCanReach:
    """A chaos driver that cannot reach a state never tests it, and says nothing about that
    in a passing run - which is the failure mode this class exists for."""

    class _Child:
        """A control that can be killed without a process behind it."""

        def __init__(self, name):
            self.name = name
            self.running = True
            self.process = type("_Process", (), {"kill": lambda self: None})()

    class _Supervisor:
        """Four killable controls and a poll that does nothing."""

        def __init__(self):
            self.children = {f"c{index}": TestWhatTheDriverCanReach._Child(f"c{index}") for index in range(4)}

        def poll(self, timeout=0.2):
            """Do nothing, as a stand-in.

            Args:
                timeout (float): ignored

            Returns:
                list: no events
            """
            return []

    class _Endpoint:
        """An endpoint whose fault switches can be set and read, and nothing else."""

        unreachable = False
        hang_seconds = 0.0
        status = None
        frozen = False

    def test_a_control_can_die_while_everything_else_is_healthy(self):
        """The commonest real failure, and one the driver could not reach: a kill used to be
        possible only while a fault was already running, because the fault branch returned
        first. Sixty seeds produce none of these against that version."""
        reached = 0
        for seed in range(60):
            driver = ChaosDriver(seed, self._Supervisor(), self._Endpoint(), self._Endpoint())
            for tick in range(1, 12):
                had_fault = driver._active is not None
                driver._choose(tick)
                killed = any(t.number == tick and t.action == "killed" for t in driver.run.ticks)
                if killed and not had_fault and driver._active is None:
                    reached += 1
        assert reached, "no seed killed a control while nothing was faulted"


def test_the_failed_seed_list_is_a_list_of_seeds():
    """Guards a constant designed to be edited by hand, in a hurry, by somebody who has just
    been handed a failing seed. A wrong shape here breaks collection for the whole suite
    rather than failing one test, so it is worth one assertion that says what is wrong."""
    assert isinstance(SEEDS_THAT_FAILED, list), f"SEEDS_THAT_FAILED must be a list, got {type(SEEDS_THAT_FAILED)}"
    wrong = [seed for seed in SEEDS_THAT_FAILED if not isinstance(seed, int) or isinstance(seed, bool)]
    assert not wrong, f"every entry must be an integer seed, got {wrong}"
